#!/usr/bin/env python3
"""
query.py — hybrid search (BM25 + vector) → RRF fusion → optional cross-encoder RERANK.

Two-stage retrieval, the production pattern:
  1. RETRIEVE WIDE (recall): BM25 + vector legs, fused with RRF, gives a candidate pool.
  2. RERANK PRECISE (precision): a cross-encoder (bge-reranker via TEI) re-reads the query
     WITH each candidate and re-scores them, so the single most on-point chunk lands on top.

The reranker is optional — set RERANK_URL in .env to turn it on. Without it you get
the RRF ordering (stage 1 only).

SCOPING: approved documents are tagged with a `collection` (the subfolder they were
dropped into) and a `source` (vault | dropzone). Filtering by collection is how you
stop home-lab notes from competing with a reference PDF for rank — and vice versa.

CITATIONS: chunks from PDFs carry `page` and `section`, printed with every hit so you
can jump straight to the passage in the source document.

Usage:
    python query.py "list all my VMs and IPs"
    python query.py "what does 800-53 say about least privilege" --collection nist
    python query.py "..." --source dropzone     # reference docs only
    python query.py "..." --source vault        # my own notes only
    python query.py "..." --k 8                 # final results to show
    python query.py "..." --full                # print whole chunks, not previews
    python query.py "..." --no-rerank           # stage-1-only, for an A/B comparison
"""
import os
import re
import hashlib
import argparse
import textwrap
import requests
import psycopg
from dotenv import load_dotenv

load_dotenv()

DB = dict(
    host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
)
OLLAMA_URL = os.environ["OLLAMA_URL"].rstrip("/")
EMBED_MODEL = os.environ["EMBED_MODEL"]

RRF_K = 60
# Pulled from EACH leg before RRF fusion. Read from the environment so it matches
# app.py on the gateway host — this was a hardcoded 20 until 2026-08-01, which meant the
# gateway honoured CANDIDATES from .env and eval.py silently ignored it. An eval
# that cannot see a knob the deployed service uses is measuring a different system.
CANDIDATES = int(os.environ.get("CANDIDATES", "20"))
RERANK_URL = os.environ.get("RERANK_URL", "").rstrip("/")   # rerank endpoint; blank = off
RERANK_BACKEND = os.environ.get("RERANK_BACKEND", "tei").lower()  # tei | llamacpp
RERANK_INPUT = int(os.environ.get("RERANK_CANDIDATES", "20"))  # fused candidates to rerank
RAG_LAB = os.environ.get("RAG_LAB", "").strip().lower()   # scope to one lab; blank = all


def embed_query(text):
    r = requests.post(f"{OLLAMA_URL}/api/embed",
                      json={"model": EMBED_MODEL, "input": text}, timeout=60)
    r.raise_for_status()
    return r.json()["embeddings"][0]


def vec_literal(vec):
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


def normalize_jurisdictions(values, include_federal=True):
    """['me'] -> ['ME', 'US'].  Accepts repeated flags or comma-separated lists.

    `US` (federal) is added automatically, because almost no state payroll
    question is answerable from state law alone — the FLSA baseline is the thing
    a state either exceeds, matches, or is silent about. Suppress with
    --no-federal when you specifically want state-only text.
    """
    out = []
    for v in values or []:
        for part in str(v).replace(",", " ").split():
            code = part.strip().upper()
            if code and code not in out:
                out.append(code)
    if out and include_federal and "US" not in out:
        out.append("US")
    return out


def scope_clause(collection, source, use_lab=True, jurisdictions=None):
    """Build the shared ' AND metadata->>... = %s' filter both legs need.
    Returns (sql_fragment, params). Keeping this in one place means the BM25 and
    vector legs can never drift out of sync on what they're allowed to see.

    JURISDICTION IS A FILTER, NOT A RANKING SIGNAL — and that distinction is the
    whole point. Measured 2026-08-02 on a two-state corpus: a Colorado-vs-Texas
    question returned 8 CO chunks and 2 TX chunks from documents with 33
    sections each, because Texas law is written as absence and preemption
    ("no state mandate", "follows the federal baseline") and absence is
    semantically thin. Scale that to 50 states and a Maine question retrieves
    41 states of noise. A reranker cannot repair it: a cross-encoder only
    reorders candidates, and the Maine chunks were never candidates. Cutting the
    candidate pool to Maine + federal BEFORE retrieval is the structural fix.

    Chunks with no `jurisdiction` (vault notes, NIST, ATF) are excluded whenever
    this filter is active. That is intended — if you are asking a state-law
    question, a NIST firewall guide is not a near miss.
    """
    sql, params = "", []
    if use_lab and RAG_LAB and source != "dropzone":
        # Approved reference docs aren't lab-specific, so a lab filter would hide them
        sql += " AND metadata->>'lab' = %s"
        params.append(RAG_LAB)
    if collection:
        sql += " AND metadata->>'collection' = %s"
        params.append(collection.lower())
    if source:
        sql += " AND coalesce(metadata->>'source','vault') = %s"
        params.append(source.lower())
    if jurisdictions:
        sql += " AND upper(metadata->>'jurisdiction') = ANY(%s)"
        params.append(list(jurisdictions))
    return sql, params


def bm25_search(cur, query, limit, scope):
    frag, sparams = scope
    cur.execute(
        "SELECT id, title, source_path, chunk_text, paradedb.score(id), metadata "
        f"FROM chunks WHERE id @@@ paradedb.match('chunk_text', %s){frag} "
        "ORDER BY paradedb.score(id) DESC LIMIT %s;",
        [query] + sparams + [limit],
    )
    return cur.fetchall()


def vector_search(cur, qvec, limit, min_sim, scope):
    lit = vec_literal(qvec)
    frag, sparams = scope
    cur.execute(
        "SELECT id, title, source_path, chunk_text, 1 - (embedding <=> %s::vector), metadata "
        f"FROM chunks WHERE 1 - (embedding <=> %s::vector) >= %s{frag} "
        "ORDER BY embedding <=> %s::vector LIMIT %s;",
        [lit, lit, min_sim] + sparams + [lit, limit],
    )
    return cur.fetchall()


def cite(row):
    """'NIST.SP.800-41r1 · §3.2 Firewall Policy · p. 17' — from chunk metadata."""
    meta = row[5] if len(row) > 5 and isinstance(row[5], dict) else {}
    parts = [row[1] or os.path.basename(row[2])]
    if meta.get("section"):
        parts.append(f"§{meta['section']}")
    if meta.get("page"):
        parts.append(f"p. {meta['page']}")
    coll = meta.get("collection")
    tail = f"   [{coll}]" if coll and coll != "vault" else ""
    return " · ".join(parts) + tail


# ---------------------------------------------------------- chunk-text hygiene
# Mirrors app.py (2026-08-15). ingest.py prepends the citation label to
# chunk_text; that label is baked from the raw PDF page index and goes stale the
# moment printed_page is introduced. Strip it so there is one locator, and dedup
# on the stripped body so repeated boilerplate cannot spend a top-k slot.
# KEEP IN SYNC WITH app.py — an eval that retrieves differently from the gateway
# is measuring a different system.
CITE_PREFIX = re.compile(r"^\s*\[[^\]\n]*·[^\]\n]*\]\s*\n?")


def strip_cite_prefix(text: str) -> str:
    return CITE_PREFIX.sub("", text or "", count=1)


def dedup_by_body(rows):
    """Drop rows whose text duplicates one already kept (after header strip)."""
    seen, out = set(), []
    for r in rows:
        h = hashlib.md5(strip_cite_prefix(r[3]).strip().encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(r)
    return out


def rrf_fuse(bm25_rows, vec_rows):
    scores, info = {}, {}
    for rank, row in enumerate(bm25_rows, 1):
        scores[row[0]] = scores.get(row[0], 0) + 1 / (RRF_K + rank)
        info[row[0]] = {"row": row, "bm25_rank": rank, "vec_rank": None,
                        "bm25_score": row[4], "similarity": None}
    for rank, row in enumerate(vec_rows, 1):
        scores[row[0]] = scores.get(row[0], 0) + 1 / (RRF_K + rank)
        d = info.setdefault(row[0], {"row": row, "bm25_rank": None, "vec_rank": None,
                                     "bm25_score": None, "similarity": None})
        d["vec_rank"] = rank
        d["similarity"] = row[4]
    ranked = sorted(scores, key=lambda i: scores[i], reverse=True)
    return [(cid, scores[cid], info[cid]) for cid in ranked]


def rerank(query, texts):
    """Cross-encoder rerank. Returns [(orig_index, score), ...] best-first.
    Supports two servers via RERANK_BACKEND:
      - tei      : POST /rerank      {query, texts}      -> [{index, score}]
      - llamacpp : POST /v1/rerank   {query, documents}  -> {results:[{index, relevance_score}]}
    """
    if RERANK_BACKEND == "llamacpp":
        r = requests.post(f"{RERANK_URL}/v1/rerank",
                          json={"model": "reranker", "query": query, "documents": texts},
                          timeout=60)
        r.raise_for_status()
        results = r.json().get("results", [])
        return sorted(((it["index"], it.get("relevance_score", it.get("score", 0.0)))
                       for it in results), key=lambda x: x[1], reverse=True)
    # default: TEI
    r = requests.post(f"{RERANK_URL}/rerank",
                      json={"query": query, "texts": texts, "truncate": True}, timeout=60)
    r.raise_for_status()
    return [(item["index"], item["score"]) for item in r.json()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--min-sim", type=float, default=0.0)
    ap.add_argument("--no-rerank", action="store_true", help="stage-1 (RRF) only, for A/B")
    ap.add_argument("--collection", help="restrict to one document collection (e.g. nist)")
    ap.add_argument("--source", choices=["vault", "dropzone"],
                    help="restrict to your own notes or to dropped reference docs")
    ap.add_argument("--jurisdiction", "-j", action="append", metavar="CODE",
                    help="restrict to one or more jurisdictions (ME, TX, US). "
                         "Repeatable or comma-separated. Federal (US) is included "
                         "automatically — see --no-federal")
    ap.add_argument("--no-federal", action="store_true",
                    help="with --jurisdiction, do NOT add US (state text only)")
    ap.add_argument("--full", action="store_true", help="print whole chunks, not previews")
    args = ap.parse_args()

    jurs = normalize_jurisdictions(args.jurisdiction, include_federal=not args.no_federal)

    qvec = embed_query(args.query)
    scope = scope_clause(args.collection, args.source, jurisdictions=jurs)
    with psycopg.connect(**DB) as conn, conn.cursor() as cur:
        bm25_rows = bm25_search(cur, args.query, CANDIDATES, scope)
        vec_rows = vector_search(cur, qvec, CANDIDATES, args.min_sim, scope)

    fused = rrf_fuse(bm25_rows, vec_rows)
    use_rerank = RERANK_URL and not args.no_rerank
    stage = "RRF only"

    if use_rerank:
        pool = fused[:RERANK_INPUT]
        order = rerank(args.query, [d["row"][3] for _, _, d in pool])
        # rebuild the list in reranked order, carrying the rerank score
        fused = [(pool[i][0], score, pool[i][2]) for i, score in order]
        stage = f"reranked (bge-reranker over top {len(pool)})"

    scope_note = ""
    if args.collection or args.source or jurs:
        scope_note = " · scope: " + " ".join(
            filter(None, [f"collection={args.collection}" if args.collection else "",
                          f"source={args.source}" if args.source else "",
                          f"jurisdiction={'+'.join(jurs)}" if jurs else ""]))

    # An empty result under a jurisdiction filter is nearly always a MISSING or
    # MISTAGGED document, not a hard question. Say which, so the next ten minutes
    # aren't spent tuning retrieval against a corpus that has nothing to find.
    if jurs and not bm25_rows and not vec_rows:
        print(f"\n⚠  No chunks at all for jurisdiction {'+'.join(jurs)}"
              + (f" in collection '{args.collection}'" if args.collection else "")
              + ".\n   Check what is actually indexed before tuning anything:\n"
              "     SELECT metadata->>'jurisdiction', count(*) FROM chunks\n"
              "     WHERE metadata ? 'jurisdiction' GROUP BY 1 ORDER BY 1;\n"
              "   A state with no rows was never ingested, or its manifest.yaml "
              "entry is missing.\n")
    print(f"\nBM25 {len(bm25_rows)} · vector {len(vec_rows)} · {stage}{scope_note}\n" + "=" * 70)
    for i, (cid, score, d) in enumerate(fused[:args.k], 1):
        row = d["row"]
        legs = []
        if d["bm25_rank"]:
            legs.append(f"BM25 #{d['bm25_rank']}")
        if d["vec_rank"]:
            legs.append(f"vec #{d['vec_rank']} ({d['similarity']:.3f})")
        label = "rerank" if use_rerank else "RRF"
        body = (" ".join(row[3].split()) if not args.full
                else textwrap.indent(row[3].strip(), "   "))
        print(f"\n{i}. {cite(row)}   [{label} {score:.4f}]   <- {' + '.join(legs)}")
        print(f"   {os.path.basename(row[2])}")
        print(body if args.full else "   " + textwrap.shorten(body, width=200))
    print()


if __name__ == "__main__":
    main()
