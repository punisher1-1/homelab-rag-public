#!/usr/bin/env python3
"""
eval.py — measure retrieval quality against a golden question set.

WHY THIS EXISTS
---------------
Every tuning decision in this pipeline so far (chunk size, RRF vs rerank, the
cosine floor, bge-m3 vs Qwen3, Docling vs pymupdf4llm) has been argued from a
handful of spot-checks. That is fine for a PoC and useless for a recommendation.
To say "this replaces AnythingLLM" you need numbers on YOUR corpus, from YOUR
questions — the same method already written down in the AnythingLLM embedder
bake-off: 20-30 real questions, each with the passage that SHOULD be retrieved,
scored on whether it landed in the top-k.

WHAT IT MEASURES
----------------
  hit@k    did an expected passage make the final top-k?  (the headline number)
  MRR      1/rank of the first expected passage — rewards ranking it #1, not #5
  pool     was it anywhere in the candidate pool before the final cut?
  coverage for `require:` questions — how many of the ALL-required passages landed

That pool number is the diagnostic that saves you time. Split the failures:
  - NOT in pool  -> a RETRIEVAL failure. The chunk was never a candidate:
                    wrong chunking, wrong embedder, missing from the corpus,
                    or a scope filter excluded it. Reranking cannot save it.
  - in pool, NOT in top-k -> a RANKING failure. The chunk was found and then
                    ordered badly. This is what a reranker exists to fix.
Chasing a ranking fix for a retrieval failure is the most common way to waste a
week on RAG.

It also reports which LEG found each hit (BM25 / vector / both), so you can see
whether hybrid search is actually earning its keep on your corpus.

SCOPE NOTE (recorded so a future session doesn't "fix" it): this harness
measures the BARE RETRIEVER — the same stage-1 + rerank path production runs,
nothing else. It has never included any prompt scaffolding, and the gateway's
decision-tree scaffold system was removed entirely on 2026-08-01. Keep this
harness the sensitivity control for chunking, cosine, and reranker work.

USAGE
-----
    python eval.py evalset.yaml                    # score the current config
    python eval.py evalset.yaml --no-rerank        # stage-1 only
    python eval.py evalset.yaml --compare          # A/B rerank on vs off (one shared pool)
    python eval.py evalset.yaml --k 5 --min-sim 0.7
    python eval.py evalset.yaml --save runs/baseline.json
    python eval.py evalset.yaml --baseline runs/baseline.json   # show the delta
    python eval.py evalset.yaml --verbose          # per-question detail

Defaults for --k and --min-sim come from .env (TOP_K / MIN_SIM) so an unflagged
run scores the DEPLOYED config, not a literal that drifted from it.

Pre-flight, before any long run:
    python eval.py evalset.yaml --verbose --save runs/preflight.json
confirm the printed config line matches the API's .env, the NIST sanity
question passes, and at least one negative control returns no confident top-1.

Write the question set with `evalset.example.yaml` as the template.
"""
import os
import sys
import json
import time
import hashlib
import argparse
import pathlib

import psycopg

# Reuse the REAL retrieval code — an eval that reimplements retrieval measures
# the eval, not the system.
import query as q

try:
    import yaml
except ImportError:
    sys.exit("eval.py needs PyYAML:  pip install pyyaml")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def row_meta(row):
    return row[5] if len(row) > 5 and isinstance(row[5], dict) else {}


def _page_eq(got, want):
    """Coerce page comparisons: a parser path that stores '17' instead of 17
    must not silently produce a false MISS — the most expensive kind of bug in
    a harness whose whole job is telling MISS from RANK."""
    try:
        return int(got) == int(want)
    except (TypeError, ValueError):
        return got == want


def matches(row, expect: dict) -> bool:
    """Does this retrieved chunk satisfy one 'expect' clause?

    Deliberately matched on CONTENT and LOCATION, never on chunk id — ids change
    on every re-ingest, so an id-based eval set silently rots the moment you
    change the chunker, which is exactly when you need it most.
    """
    meta = row_meta(row)
    title, src, text = (row[1] or ""), (row[2] or ""), (row[3] or "")

    if "file" in expect:
        want = str(expect["file"]).lower()
        if want not in os.path.basename(src).lower() and want not in title.lower():
            return False

    if "page" in expect and not _page_eq(meta.get("page"), expect["page"]):
        return False

    if "pages" in expect:
        lo, hi = expect["pages"]
        try:
            pg = int(meta.get("page"))
        except (TypeError, ValueError):
            return False
        if not (lo <= pg <= hi):
            return False

    if "section" in expect:
        if str(expect["section"]).lower() not in str(meta.get("section") or "").lower():
            return False

    if "contains" in expect:
        needles = expect["contains"]
        if isinstance(needles, str):
            needles = [needles]
        low = text.lower()
        if not all(str(n).lower() in low for n in needles):
            return False

    return True


def first_hit(rows, expects):
    """Index of the first row satisfying ANY expect clause, else None."""
    for i, row in enumerate(rows):
        if any(matches(row, e) for e in expects):
            return i
    return None


def clause_status(topk, pool, clause):
    """PASS = in final top-k · RANK = in pool but ordered out · MISS = never a candidate.

    Per-clause, not per-question. A multi-hop question can be PASS on the statute
    and MISS on the determination letter, and those need different fixes.
    """
    if any(matches(r, clause) for r in topk):
        return "PASS"
    if any(matches(r, clause) for r in pool):
        return "RANK"
    return "MISS"


def score_required(topk, pool, requires):
    """Returns (statuses, n_pass, total). statuses is [(clause, status), ...].

    `expect:` means any-of (either passage will do). `require:` means ALL-of —
    a multi-hop question is only complete when every named passage landed,
    because answering from a partial set is exactly the state that produced the
    wrong answers in the probe series.

    ⚠️ CLAUSES MUST BE SATISFIED BY **DISTINCT** ROWS (added 2026-08-02).
    Previously each clause was scored independently, so ONE chunk could satisfy
    several clauses and the question still reported COMPLETE. Found on the first
    payroll run: `§6.1 Metadata Anchors` — a RAG implementation-notes section
    listing the document's own keywords — contained both "preempt" and "40" and
    single-handedly satisfied both Texas clauses. The eval said COMPLETE 4/4 on a
    retrieval with no substantive Texas coverage, while the real answer omitted
    preemption entirely. Coverage has to mean distinct EVIDENCE, not keyword hits.

    New status `DUP` = the clause matched, but only on a row already claimed by an
    earlier clause. Treated as a failure, and it usually means the clause is too
    loose or an index/metadata section is polluting the corpus.
    """
    statuses, claimed = [], set()
    for c in requires:
        # greedy distinct assignment: first matching row not already spoken for
        idx = next((i for i, r in enumerate(topk)
                    if matches(r, c) and i not in claimed), None)
        if idx is not None:
            claimed.add(idx)
            statuses.append((c, "PASS"))
        elif any(matches(r, c) for r in topk):
            statuses.append((c, "DUP"))          # matched, but shares a row
        else:
            statuses.append((c, clause_status(topk, pool, c)))   # RANK or MISS
    return statuses, sum(1 for _, s in statuses if s == "PASS"), len(requires)


# ---------------------------------------------------------------------------
# Retrieval (mirrors query.py / app.py exactly, so results are comparable)
# ---------------------------------------------------------------------------
def stage1(cur, question, collection, source, min_sim):
    """Embed + both legs + RRF fusion. Returns (pool, legs)."""
    qvec = q.embed_query(question)
    scope = q.scope_clause(collection, source)
    bm25 = q.bm25_search(cur, question, q.CANDIDATES, scope)
    vec = q.vector_search(cur, qvec, q.CANDIDATES, min_sim, scope)
    legs = {}
    for r in bm25:
        legs[r[0]] = "bm25"
    for r in vec:
        legs[r[0]] = "both" if r[0] in legs else "vec"
    # Dedup exactly as app.py does (2026-08-15) — otherwise the eval measures a
    # retrieval stack the gateway does not run.
    pool = q.dedup_by_body([d["row"] for _, _, d in q.rrf_fuse(bm25, vec)])
    return pool, legs


def apply_rerank(question, pool):
    """Rerank the head and DISCARD the tail — matches app.py and query.py.
    (An earlier version kept pool[RERANK_INPUT:], which production never sees.)
    Returns (ordered_rows, rerank_seconds)."""
    if not (q.RERANK_URL and pool):
        return pool, 0.0
    head = pool[:q.RERANK_INPUT]
    t0 = time.time()
    try:
        order = q.rerank(question, [r[3] for r in head])
    except Exception as e:
        print(f"    [rerank skipped: {e}]")
        return pool, time.time() - t0
    return [head[i] for i, _ in order], time.time() - t0


def collect_stage1(questions, args):
    """One retrieval pass, reused by every arm of a --compare run — the two arms
    of a rerank A/B must see the SAME candidate pool, and this guarantees it."""
    cache = {}
    with psycopg.connect(**q.DB) as conn, conn.cursor() as cur:
        for i, item in enumerate(questions):
            t0 = time.time()
            pool, legs = stage1(
                cur, item["q"],
                item.get("collection", args.collection),
                item.get("source", args.source),
                args.min_sim)
            cache[i] = (pool, legs, time.time() - t0)
    return cache


def run_set(questions, args, use_rerank, cache):
    """Score every question against the cached stage-1 pools."""
    results = []
    for i, item in enumerate(questions):
        question = item["q"]
        expects = item.get("expect") or []
        if isinstance(expects, dict):
            expects = [expects]
        requires = item.get("require") or []
        if isinstance(requires, dict):
            requires = [requires]

        pool, legs, s1_secs = cache[i]
        if use_rerank:
            ordered, rr_secs = apply_rerank(question, pool)
        else:
            ordered, rr_secs = pool, 0.0

        topk = ordered[:args.k]
        rank = first_hit(topk, expects) if expects else None
        pool_rank = first_hit(pool, expects) if expects else None
        hit_row = topk[rank] if rank is not None else None

        req_status, req_pass, req_total = score_required(topk, pool, requires)

        results.append({
            "q": question,
            "note": item.get("note", ""),
            "check": (item.get("check") or "").strip(),
            "hit": rank is not None,
            "rank": (rank + 1) if rank is not None else None,
            "in_pool": pool_rank is not None,
            "pool_rank": (pool_rank + 1) if pool_rank is not None else None,
            "leg": legs.get(hit_row[0]) if hit_row is not None else None,
            "cite": q.cite(hit_row) if hit_row is not None else None,
            "top1": q.cite(topk[0]) if topk else None,
            "stage1_secs": round(s1_secs, 2),
            "rerank_secs": round(rr_secs, 2),
            "secs": round(s1_secs + rr_secs, 2),
            "scored": bool(expects),
            # require: (all-of) coverage for multi-hop questions
            "req_total": req_total,
            "req_pass": req_pass,
            "coverage": (req_pass / req_total) if req_total else None,
            "complete": (req_total > 0 and req_pass == req_total),
            "req_detail": [
                {"clause": c, "status": s} for c, s in req_status if s != "PASS"
            ],
        })

    scored = [r for r in results if r["scored"]]
    n = len(scored) or 1
    hits = [r for r in scored if r["hit"]]
    summary = {
        "questions": len(scored),
        "hit_at_k": len(hits) / n,
        "mrr": sum(1 / r["rank"] for r in hits) / n,
        "recall_pool": sum(1 for r in scored if r["in_pool"]) / n,
        # the diagnostic split
        "ranking_failures": sum(1 for r in scored if r["in_pool"] and not r["hit"]),
        "retrieval_failures": sum(1 for r in scored if not r["in_pool"]),
        "leg_bm25": sum(1 for r in hits if r["leg"] == "bm25"),
        "leg_vec": sum(1 for r in hits if r["leg"] == "vec"),
        "leg_both": sum(1 for r in hits if r["leg"] == "both"),
        "avg_secs": round(sum(r["secs"] for r in scored) / n, 2),
        # Questions whose ANSWER must be judged by a human. Retrieval metrics
        # cannot see a model that retrieved the right document and then reasoned
        # wrong, or that flipped its answer under pushback — both observed
        # 2026-07-24 on the ATF corpus, where hit@k would have reported PASS.
        "needs_review": sum(1 for r in results if r["check"]),
    }
    cov = [r for r in results if r["req_total"]]
    m = len(cov) or 1
    summary.update({
        "coverage_questions": len(cov),
        "mean_coverage": sum(r["coverage"] for r in cov) / m,
        "complete_at_k": sum(1 for r in cov if r["complete"]) / m,
        # per-clause failure split across ALL required clauses — the actionable number
        "clause_rank_failures": sum(
            1 for r in cov for d in r["req_detail"] if d["status"] == "RANK"),
        "clause_miss_failures": sum(
            1 for r in cov for d in r["req_detail"] if d["status"] == "MISS"),
    })
    return results, summary


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_report(results, summary, label, verbose):
    print(f"\n{'=' * 74}\n{label}\n{'=' * 74}")
    for r in results:
        if not r["scored"] and not r["req_total"]:
            mark, detail = "?", "(no `expect:` or `require:` — runs but not scored)"
        elif not r["scored"]:
            # A `require:`-only question IS scored — on COVERAGE, not hit@k.
            # This branch previously printed "?  (no expect: — not scored)" and
            # then a "[PARTIAL] coverage 2/3" line directly underneath it, which
            # contradicted itself and buried the multi-hop result — the single
            # most informative question in the set. (Found 2026-07-30.)
            mark = "COMP" if r["complete"] else "PART"
            detail = (f"scored on COVERAGE (all-of), not hit@k — "
                      f"{r['req_pass']}/{r['req_total']} required passages in top-{ARGS.k}")
        elif r["hit"]:
            mark = "PASS"
            detail = f"rank {r['rank']} via {r['leg']}  ->  {r['cite']}"
        elif r["in_pool"]:
            mark = "RANK"          # found, ordered badly
            detail = f"in pool at {r['pool_rank']} but not in top-{ARGS.k}  (ranking failure)"
        else:
            mark = "MISS"          # never a candidate
            detail = "not in candidate pool at all  (retrieval failure)"
        print(f"  [{mark:4}] {r['q'][:62]}")
        if verbose or mark != "PASS" or r["check"] or r["req_detail"]:
            print(f"         {detail}")
            if verbose and r["top1"]:
                print(f"         top-1 was: {r['top1']}")
            if r["req_total"]:
                tag = "COMPLETE" if r["complete"] else "PARTIAL"
                print(f"         [{tag}] coverage {r['req_pass']}/{r['req_total']}")
                for d in r["req_detail"]:
                    print(f"           {d['status']:4} {d['clause']}")
            if r["note"]:
                print(f"         note: {r['note']}")
            if r["check"]:
                # Retrieval passing tells you nothing here — read the answer.
                for line in r["check"].splitlines():
                    if line.strip():
                        print(f"         JUDGE: {line.strip()}")

    s = summary
    print(f"\n  hit@{ARGS.k}: {s['hit_at_k']:.0%}   MRR: {s['mrr']:.3f}   "
          f"in-pool recall: {s['recall_pool']:.0%}   avg {s['avg_secs']}s/q")
    print(f"  failures: {s['retrieval_failures']} retrieval (fix chunking/embedding/corpus) · "
          f"{s['ranking_failures']} ranking (fix the reranker/fusion)")
    print(f"  hits found by: BM25 {s['leg_bm25']} · vector {s['leg_vec']} · both {s['leg_both']}")
    if s.get("coverage_questions"):
        print(f"  coverage: mean {s['mean_coverage']:.0%} · "
              f"complete@{ARGS.k}: {s['complete_at_k']:.0%} "
              f"({s['coverage_questions']} multi-hop q)")
        print(f"  clause failures: {s['clause_miss_failures']} MISS · "
              f"{s['clause_rank_failures']} RANK")
    if s.get("needs_review"):
        print(f"  ** {s['needs_review']} question(s) carry a `check:` — retrieval score is NOT the")
        print(f"     result. Read the actual answers against the JUDGE criteria above. **")


def print_delta(base, new):
    print(f"\n{'=' * 74}\nDELTA vs baseline\n{'=' * 74}")
    for key, fmt in (("hit_at_k", "{:.0%}"), ("mrr", "{:.3f}"), ("recall_pool", "{:.0%}"),
                     ("retrieval_failures", "{}"), ("ranking_failures", "{}"),
                     ("mean_coverage", "{:.0%}"), ("complete_at_k", "{:.0%}")):
        b, n = base.get(key), new.get(key)
        if b is None or n is None:
            continue
        arrow = "=" if n == b else ("+" if n > b else "-")
        print(f"  {key:20} {fmt.format(b):>8}  ->  {fmt.format(n):>8}   [{arrow}]")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("evalset", help="YAML question set (see evalset.example.yaml)")
    # Defaults come from the DEPLOYED config (.env), not literals. query.py calls
    # load_dotenv() at import, and `import query` above runs before this parser
    # is built, so TOP_K / MIN_SIM are already in the environment.
    ap.add_argument("--k", type=int,
                    default=int(os.environ.get("TOP_K", "6")),
                    help="top-k the answer sees (default: app.py's TOP_K)")
    ap.add_argument("--min-sim", type=float,
                    default=float(os.environ.get("MIN_SIM", "0.0")),
                    help="vector-leg cosine floor (default: app.py's MIN_SIM)")
    ap.add_argument("--collection", help="default collection scope for all questions")
    ap.add_argument("--source", choices=["vault", "dropzone"])
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--compare", action="store_true", help="A/B: rerank on vs off")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--save", help="write results JSON here")
    ap.add_argument("--baseline", help="compare the summary against a saved run")
    ARGS = ap.parse_args()

    doc = yaml.safe_load(pathlib.Path(ARGS.evalset).read_text(encoding="utf-8")) or {}
    questions = doc.get("questions") or []
    if not questions:
        sys.exit(f"No questions in {ARGS.evalset}")
    # A question with `require:` but no `expect:` IS scored — on coverage. Counting
    # it as unscored here understated the set and made the multi-hop question look
    # like a throwaway. (Fixed 2026-07-30.)
    unscored = sum(1 for q_ in questions
                   if not q_.get("expect") and not q_.get("require"))
    cov_only = sum(1 for q_ in questions
                   if q_.get("require") and not q_.get("expect"))
    print(f"Loaded {len(questions)} question(s) from {ARGS.evalset}"
          + (f"  ({cov_only} scored on coverage only)" if cov_only else "")
          + (f"  ({unscored} with neither `expect:` nor `require:` — run but not scored)"
             if unscored else ""))
    # Provenance in the log, not just the JSON.
    print(f"config: k={ARGS.k} min_sim={ARGS.min_sim} "
          f"candidates={q.CANDIDATES} rerank_input={q.RERANK_INPUT} "
          f"rerank={'on' if q.RERANK_URL and not ARGS.no_rerank else 'off'}")

    # One retrieval pass; every arm below reads from this cache.
    cache = collect_stage1(questions, ARGS)

    if ARGS.compare:
        r_off, s_off = run_set(questions, ARGS, use_rerank=False, cache=cache)
        print_report(r_off, s_off, "STAGE 1 ONLY (RRF fusion, no reranker)", ARGS.verbose)
        r_on, s_on = run_set(questions, ARGS, use_rerank=True, cache=cache)
        print_report(r_on, s_on, "RERANKED (cross-encoder)", ARGS.verbose)
        print_delta(s_off, s_on)
        results, summary = r_on, s_on
    else:
        use_rerank = not ARGS.no_rerank
        label = "RERANKED" if (use_rerank and q.RERANK_URL) else "STAGE 1 ONLY (RRF)"
        results, summary = run_set(questions, ARGS, use_rerank, cache)
        print_report(results, summary, label, ARGS.verbose)

    if ARGS.baseline:
        base = json.loads(pathlib.Path(ARGS.baseline).read_text())
        print_delta(base["summary"], summary)

    if ARGS.save:
        out = pathlib.Path(ARGS.save)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Record EVERY knob that was tuned — two runs at different candidate
        # depths must not produce indistinguishable JSON. The evalset hash
        # enforces "freeze the question set before the bake-off": if the hash
        # moves between runs, the comparison is void and the JSON says so.
        out.write_text(json.dumps({
            "when": time.strftime("%Y-%m-%d %H:%M"),
            "config": {
                "k": ARGS.k,
                "min_sim": ARGS.min_sim,
                "candidates": q.CANDIDATES,
                "rerank": bool(q.RERANK_URL and not ARGS.no_rerank),
                "rerank_backend": q.RERANK_BACKEND if q.RERANK_URL else None,
                "rerank_url": q.RERANK_URL or None,
                "rerank_input": q.RERANK_INPUT,
                "rrf_k": q.RRF_K,
                "embed_model": q.EMBED_MODEL,
                "collection": ARGS.collection,
                "source": ARGS.source,
                "rag_lab": q.RAG_LAB or None,
                "evalset": str(ARGS.evalset),
                "evalset_sha1": hashlib.sha1(
                    pathlib.Path(ARGS.evalset).read_bytes()).hexdigest()[:12],
            },
            "summary": summary, "results": results,
        }, indent=1))
        print(f"\nSaved -> {out}")
    print()


if __name__ == "__main__":
    main()
