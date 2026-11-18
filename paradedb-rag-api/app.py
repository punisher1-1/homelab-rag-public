#!/usr/bin/env python3
"""
ParadeDB RAG Gateway — an OpenAI-compatible API that puts hybrid retrieval in
front of your generation backend, so Open WebUI can chat with your knowledge base.

Flow for every chat message:
    user question
      → embed (bge-m3)            → hybrid retrieve from ParadeDB (BM25 + vector, RRF)
      → build a grounded prompt   → forward to the backend (/v1/chat/completions)
      → stream the answer back, then append a Sources list
                                  (reconciled against what the model said about its
                                   own sourcing — see DISCLAIM_GUARD)

Open WebUI points at THIS service as if it were an OpenAI endpoint. It never talks
to the backend directly — every turn goes through retrieval first. The retrieval
logic is the same BM25 + vector + RRF fusion proved out in query.py, threshold and all.

BACKEND-AGNOSTIC: anything speaking the OpenAI API works here — vLLM (CT-served
Granite, no auth) or Unsloth Studio (gpu-node :8889, bearer-key auth). Swap with
GEN_URL / GEN_API_KEY in .env; the legacy VLLM_* names still work as fallbacks.

Endpoints:
    GET  /health               — checks DB, embeddings, and the generation backend
    GET  /v1/models            — advertises this RAG model to Open WebUI
    POST /v1/chat/completions  — the RAG turn (streaming + non-streaming)
    POST /v1/search            — retrieval only, for tool-calling clients (bearer token)
"""
import os
import re
import json
import hashlib
import hmac
import time
import uuid
import requests
import psycopg
from fastapi import FastAPI, Request, Header
from fastapi.responses import StreamingResponse, JSONResponse

# ---------------------------------------------------------------- config (.env)
DB = dict(
    host=os.environ.get("DB_HOST", "paradedb-host"),
    port=os.environ.get("DB_PORT", "5432"),
    dbname=os.environ.get("DB_NAME", "myragdatabase"),
    user=os.environ.get("DB_USER", "myuser"),
    password=os.environ["DB_PASSWORD"],
)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://embed-host:11434").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3:latest")

# --- Generation backend (any OpenAI-compatible server) -------------------------
# GEN_* are the current names. The old VLLM_* names are honoured as fallbacks so
# an existing .env keeps working; GEN_* wins when both are set.
#   vLLM (gpu-node :8000)           → no API key needed as we run it
#   Unsloth Studio (gpu-node :8889) → REQUIRES a bearer key (sk-unsloth-…)
GEN_URL = (os.environ.get("GEN_URL")
           or os.environ.get("VLLM_URL", "http://gen-host:8000/v1")).rstrip("/")
GEN_MODEL = (os.environ.get("GEN_MODEL")
             or os.environ.get("VLLM_MODEL", "")).strip()  # blank = auto-discover
GEN_API_KEY = (os.environ.get("GEN_API_KEY")
               or os.environ.get("VLLM_API_KEY", "")).strip()  # blank = no auth header
# Sent on every call to the backend. requests omits an empty dict, so an
# unauthenticated vLLM is unaffected by this being here.
GEN_HEADERS = {"Authorization": f"Bearer {GEN_API_KEY}"} if GEN_API_KEY else {}

MODEL_NAME = os.environ.get("MODEL_NAME", "paradedb-rag")  # id shown in Open WebUI

# --- Generation parameters: the GATEWAY is the authority ----------------------
# Clients (Open WebUI, the web frontend) send temperature/max_tokens with
# every request, and silently different client defaults have already confounded
# one A/B (0.57 vs 0.2, found 2026-07-28). So the values in .env win, every
# path generates under identical recorded settings, and eval runs are
# reproducible. Flip ALLOW_CLIENT_PARAMS=true only if you deliberately want
# per-request experimentation from the client side.
GEN_TEMPERATURE = float(os.environ.get("GEN_TEMPERATURE", "0.2"))
GEN_MAX_TOKENS = int(os.environ.get("GEN_MAX_TOKENS", "0"))      # 0 = no cap
ALLOW_CLIENT_PARAMS = os.environ.get("ALLOW_CLIENT_PARAMS", "false").lower() == "true"

# --- Collection scoping -------------------------------------------------------
# Drop-zone collections (the dropzone/ subfolder names). Each one listed here is
# advertised as a model variant ("paradedb-rag-atf") so Open WebUI can scope a
# chat to one corpus; a custom frontend can instead send "collection" (and
# optionally "source": vault|dropzone) in the request body, which wins.
# Unscoped questions otherwise retrieve across everything — the reason firearms
# questions were competing with NIST firewall chunks.
COLLECTIONS = [c.strip().lower()
               for c in os.environ.get("COLLECTIONS", "").split(",") if c.strip()]

TOP_K = int(os.environ.get("TOP_K", "6"))          # chunks fed to the model
MIN_SIM = float(os.environ.get("MIN_SIM", "0.0"))  # vector-leg cosine floor (yours!)
CANDIDATES = int(os.environ.get("CANDIDATES", "20"))
RRF_K = 60
SHOW_SOURCES = os.environ.get("SHOW_SOURCES", "true").lower() == "true"

# Provenance guard (added 2026-08-09). The Sources block is rendered from what
# RETRIEVAL returned and has no relationship to what the model actually wrote —
# so when the model says "this came from my training, not your passages", the
# gateway would still print real page-accurate citations above that sentence.
# Observed 2026-08-09 on a NIST AI 100-1 turn: topically perfect, provenance-false.
#   warn     — keep the list, but relabel it so it cannot be read as evidence (default)
#   suppress — drop the list, print one line saying nothing supported the answer
#   off      — pre-2026-08-09 behaviour (always append, unconditionally)
DISCLAIM_GUARD = os.environ.get("DISCLAIM_GUARD", "warn").strip().lower()

# Strip Sources blocks the MODEL writes for itself (added 2026-08-09, second pass).
# The guard above only governs the block the GATEWAY appends. Observed the same day:
# asked how long a PowerPoint should be — a question with no support anywhere in an
# ATF/NIST corpus — the model invented TWO citation lists, one of them attributing
# presentation-length advice to an ATF Paperwork Reduction Act notice, and printed
# them ABOVE the gateway's correctly-labelled block. It learned the format from
# an earlier SYSTEM_PROMPT that said "cite the note titles you used" and reproduced
# it whether or not it used anything.
# NOTE: enabling this makes streaming LINE-buffered instead of token-buffered — a
# citation block can only be recognised once its line is complete. Set false to get
# token-by-token streaming back.
STRIP_MODEL_SOURCES = os.environ.get("STRIP_MODEL_SOURCES", "true").lower() == "true"

# Abstention floor (added 2026-08-09, third pass). MIN_SIM=0.0 + RRF means the
# pipeline ALWAYS returns a top-k — there is no "nothing matched" state. Asked how
# long a PowerPoint should be, it returned ten ATF Paperwork Reduction Act notices
# and printed them as Sources. Three separate turns have now failed this way.
#
# This does NOT filter retrieval. MIN_SIM=0.0 stays exactly as it is — the model
# still receives every chunk it would have received. What is gated is the CLAIM OF
# GROUNDING: below the floor the Sources block is replaced with an honest note and
# the model is told its passages are weak. Retrieval is unchanged; only the
# assertion about it changes.
#
# BOTH DEFAULT TO OFF. Ship the instrument before the threshold: every turn logs
# its top scores, so after a few days of real use you can see the distribution for
# good vs bad queries and pick a floor from data instead of taste.
ABSTAIN_MIN_SIM = float(os.environ.get("ABSTAIN_MIN_SIM", "0.0"))
_amr = os.environ.get("ABSTAIN_MIN_RERANK", "").strip()
ABSTAIN_MIN_RERANK = float(_amr) if _amr else None

# Reranker (optional): retrieve wide with RRF, then a cross-encoder re-scores the pool.
RERANK_URL = os.environ.get("RERANK_URL", "").rstrip("/")        # blank = off
RERANK_BACKEND = os.environ.get("RERANK_BACKEND", "tei").lower()  # tei | llamacpp
RERANK_INPUT = int(os.environ.get("RERANK_CANDIDATES", "20"))    # candidates fed to reranker
RAG_LAB = os.environ.get("RAG_LAB", "").strip().lower()          # scope to one lab; blank = all

# Retrieval-only endpoint (added 2026-10-16) for the local model harness, which
# calls it as a tool and writes the answer itself. Same retrieval as a chat turn:
# hybrid search, dedup, rerank, and the abstention floor, reported as `weak`.
# It is OFF until SEARCH_TOKEN is set, and requires that bearer token: unlike chat,
# it returns raw passages to any caller, so it is not left open on the LAN.
SEARCH_TOKEN = os.environ.get("SEARCH_TOKEN", "").strip()
SEARCH_TEXT_CHARS = int(os.environ.get("SEARCH_TEXT_CHARS", "4000"))   # per passage
SEARCH_SOURCES = {"vault", "dropzone"}
# Valid collections for search come from the chunks themselves, not COLLECTIONS:
# ingestion names a collection after its approved/ folder (or its manifest), and new
# folders arrive from the NAS automation without anyone editing this .env.
# COLLECTIONS still controls the Open WebUI model variants. Cached briefly.
COLLECTION_CACHE_SECONDS = 300
_collection_cache = {"at": 0.0, "names": []}

SYSTEM_PROMPT = (
    "You are answering from a currated corpus of documents. Use ONLY the context "
    "passages provided; each is tagged with its source type/status, e.g. (entity/current).\n"
    "Rules:\n"
    "- If passages conflict on a CURRENT fact (hardware, IP addresses, what is running), "
    "trust entity/inventory pages and the most recent information over historical "
    "troubleshooting notes, benchmarks, or logs.\n"
    "- Treat clearly historical statements (past hardware, 'was', dated results) as history, "
    "not current state.\n"
    "- If the answer is not in the context, say you don't know. NEVER guess or emit "
    "placeholder values like '10.10.10.??' or '100.???' — omit what you don't have.\n"
    "- Be concise.\n"
    "- When a passage carries a section or page locator (e.g. '— §3.2 Firewall Policy, "
    "p. 17'), REPEAT IT VERBATIM in your answer so the reader can find the passage in "
    "the source document. Never invent a page, section number, or source title that is "
    "not printed in the context.\n"
    "- Do NOT end your answer with a 'Sources:' list. The system appends the "
    "authoritative source list automatically. Name a note only inline when its title "
    "appears in the context you used; never invent a bibliography.\n"
    "- Answer directly. Do NOT narrate your reasoning, restate the question, or "
    "describe what the user is asking. Deliberation is handled separately by the "
    "reasoning parser and must not appear in your answer."
)

app = FastAPI(title="ParadeDB RAG Gateway")


# ---------------------------------------------------------------- retrieval
def embed_query(text: str):
    r = requests.post(f"{OLLAMA_URL}/api/embed",
                      json={"model": EMBED_MODEL, "input": text}, timeout=60)
    r.raise_for_status()
    return r.json()["embeddings"][0]


def vec_literal(vec):
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


def rerank(query, texts):
    """Cross-encoder rerank. Returns [(orig_index, score), ...] best-first.
    tei      : POST /rerank    {query, texts}     -> [{index, score}]
    llamacpp : POST /v1/rerank {query, documents} -> {results:[{index, relevance_score}]}
    """
    if RERANK_BACKEND == "llamacpp":
        r = requests.post(f"{RERANK_URL}/v1/rerank",
                          json={"model": "reranker", "query": query, "documents": texts},
                          timeout=60)
        r.raise_for_status()
        results = r.json().get("results", [])
        return sorted(((it["index"], it.get("relevance_score", it.get("score", 0.0)))
                       for it in results), key=lambda x: x[1], reverse=True)
    r = requests.post(f"{RERANK_URL}/rerank",
                      json={"query": query, "texts": texts, "truncate": True}, timeout=60)
    r.raise_for_status()
    return [(item["index"], item["score"]) for item in r.json()]


def scope_clause(collection=None, source=None):
    """Shared ' AND metadata->>... = %s' filter for both legs — the SAME logic as
    query.py's scope_clause, so the gateway and the eval harness can never drift
    on what they're allowed to see. Returns (sql_fragment, params)."""
    sql, params = "", []
    if RAG_LAB and source != "dropzone":
        # drop-zone reference docs aren't lab-specific; a lab filter would hide them
        sql += " AND metadata->>'lab' = %s"
        params.append(RAG_LAB)
    if collection:
        sql += " AND metadata->>'collection' = %s"
        params.append(collection.lower())
    if source:
        sql += " AND coalesce(metadata->>'source','vault') = %s"
        params.append(source.lower())
    return sql, params


def hybrid_retrieve(question: str, collection=None, source=None):
    """BM25 + vector legs, fused with RRF. Returns (top TOP_K rows, score stats).

    The stats exist for the abstention floor, and WHICH score is reported matters:

      * `top_sim` — best cosine similarity in the vector candidate pool. Bounded
        [-1, 1] and comparable across queries, so it can carry a threshold.
      * `top_rerank` — best cross-encoder score, present only when RERANK_URL is
        set. Query-document specific and better calibrated than similarity; the
        preferred signal when available.

    The RRF fused score is deliberately NOT reported. RRF is 1/(60+rank) summed —
    it encodes POSITION, not relevance, so the top hit on a query with no good
    answer scores identically to the top hit on a perfect one. Thresholding it
    would look reasonable and measure nothing.
    """
    qvec = embed_query(question)
    lit = vec_literal(qvec)
    frag, sparams = scope_clause(collection, source)
    with psycopg.connect(**DB, connect_timeout=5) as conn, conn.cursor() as cur:
        # Use the paradedb.match() BUILDER, not the raw `@@@ 'string'` operator:
        # the string form is parsed as ParadeDB's query DSL (`:` = field:term, etc.),
        # so raw user text with punctuation breaks it. match() treats input as plain terms.
        cur.execute(
            "SELECT id, title, source_path, chunk_text, paradedb.score(id), metadata "
            f"FROM chunks WHERE id @@@ paradedb.match('chunk_text', %s){frag} "
            "ORDER BY paradedb.score(id) DESC LIMIT %s;",
            [question] + sparams + [CANDIDATES],
        )
        bm25 = cur.fetchall()
        cur.execute(
            "SELECT id, title, source_path, chunk_text, 1 - (embedding <=> %s::vector), metadata "
            f"FROM chunks WHERE 1 - (embedding <=> %s::vector) >= %s{frag} "
            "ORDER BY embedding <=> %s::vector LIMIT %s;",
            [lit, lit, MIN_SIM] + sparams + [lit, CANDIDATES],
        )
        vec = cur.fetchall()

    scores, rows = {}, {}
    for rank, row in enumerate(bm25, 1):
        scores[row[0]] = scores.get(row[0], 0) + 1 / (RRF_K + rank)
        rows[row[0]] = row
    for rank, row in enumerate(vec, 1):
        scores[row[0]] = scores.get(row[0], 0) + 1 / (RRF_K + rank)
        rows.setdefault(row[0], row)
    stats = {"top_sim": max((r[4] for r in vec), default=None),
             "top_rerank": None, "n": len(scores)}

    ranked = sorted(scores, key=lambda i: scores[i], reverse=True)
    # Dedup BEFORE slicing so the reranker gets a full complement of DISTINCT
    # candidates rather than spending its window on repeated boilerplate.
    pool = dedup_by_body([rows[i] for i in ranked])[:RERANK_INPUT]   # stage 1: wide recall (RRF)
    if RERANK_URL and pool:                              # stage 2: precision (cross-encoder)
        try:
            order = rerank(question, [r[3] for r in pool])
            pool = [pool[i] for i, _ in order]
            stats["top_rerank"] = max((s for _, s in order), default=None)
        except Exception as e:
            print(f"[rerank] skipped ({e}) — falling back to RRF order")
    return pool[:TOP_K], stats


def retrieval_is_weak(stats) -> bool:
    """True when nothing retrieved is a strong enough match to claim as a source.

    Reranker wins when present — a cross-encoder actually reads the query against
    the passage, where cosine similarity only compares two independent summaries
    of meaning. Logged on every turn regardless of whether a floor is set, because
    the log IS the calibration data.
    """
    if not stats:
        return False
    sim, rr = stats.get("top_sim"), stats.get("top_rerank")
    print(f"[floor] top_sim={sim if sim is None else round(sim, 4)} "
          f"top_rerank={rr if rr is None else round(rr, 4)} n={stats.get('n')}")
    if ABSTAIN_MIN_RERANK is not None and rr is not None:
        return rr < ABSTAIN_MIN_RERANK
    if ABSTAIN_MIN_SIM > 0 and sim is not None:
        return sim < ABSTAIN_MIN_SIM
    return False


def locator(meta: dict) -> str:
    """'§3.2 Firewall Policy, p. 17' — the part of a citation that tells you
    WHERE in the document to look. Empty for notes with no page/section.

    PAGE NUMBERING (2026-08-10). `meta["page"]` is the 1-based PDF page INDEX.
    Published standards carry covers, notices and roman-numeral front matter, so
    the number PRINTED on the page is lower — by 5 on nist.ai.100-1, where the
    Executive Summary is PDF 6-8 and prints as pages 1-3. A reader who checks the
    PDF index lands in the wrong section and concludes the citation was
    fabricated. That is worse than a silent error: it breaks the human
    verification loop the citations exist to support.

    So prefer the printed number and LABEL the fallback. `printed_page` is null
    for anything ingested before 2026-08-10, for the non-Docling paths, and for
    pages with no numbered footer (full-page figures — PDF 16 in nist.ai.100-1).
    "PDF p. 16" is honest there; a bare "p. 16" is not.

    This one function feeds BOTH build_context() (what the model reads) and
    sources_markdown() (what the reader sees), so fixing it here fixes both.
    """
    bits = []
    if meta.get("section"):
        bits.append(f"§{meta['section']}")
    if meta.get("printed_page"):
        bits.append(f"p. {meta['printed_page']}")
    elif meta.get("page"):
        bits.append(f"PDF p. {meta['page']}")
    return ", ".join(bits)


# ---------------------------------------------------------- chunk-text hygiene
# ingest.py prepends the citation label to chunk_text itself, e.g.
#   "[atf-p-5320-8-1 · §Section 2.1 Types of NFA firearms · p. 17]\n..."
# That string is baked at INGEST time from the raw PDF page index. locator() below
# was fixed on 2026-08-10 to prefer `printed_page`, but the model reads the chunk
# BODY and cites the stale label instead of the correct header — observed on two
# different models, both citing "p. 17" while the Sources block correctly said
# "p. 5". Same physical page, two numbering schemes, and the one the model repeats
# is the one a reader cannot verify. Strip it so locator() is the only locator.
#
# Requiring a "·" inside the brackets is the safety catch: it prevents this from
# eating a chunk that legitimately opens with "[1]" or "[Assignment: ...]".
CITE_PREFIX = re.compile(r"^\s*\[[^\]\n]*·[^\]\n]*\]\s*\n?")


def strip_cite_prefix(text: str) -> str:
    return CITE_PREFIX.sub("", text or "", count=1)


def dedup_by_body(rows):
    """Drop rows whose text is identical to one already kept.

    Reference documents repeat themselves — form instruction blocks and notices
    recur verbatim across appendices (10 duplicate bodies in atf-p-5320-8-1,
    found 2026-08-15). A duplicate that reaches the final top-k spends a slot on
    zero new information, and top-k is the binding constraint on multi-hop
    coverage: every wasted slot is one fewer chance for a required passage.

    Compared AFTER stripping the citation header, so two copies of the same text
    carrying different page labels are correctly treated as one passage.
    """
    seen, out = set(), []
    for r in rows:
        h = hashlib.md5(strip_cite_prefix(r[3]).strip().encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(r)
    if len(out) != len(rows):
        print(f"[dedup] {len(rows) - len(out)} duplicate passage(s) removed")
    return out


def build_context(chunks):
    blocks = []
    for i, row in enumerate(chunks, 1):
        title, src, text = row[1], row[2], strip_cite_prefix(row[3])
        meta = row[5] if len(row) > 5 and isinstance(row[5], dict) else {}
        tag = f"{meta.get('type') or 'note'}/{meta.get('status') or 'current'}"
        loc = locator(meta)
        head = f"{title or os.path.basename(src)}" + (f" — {loc}" if loc else "")
        blocks.append(f"[{i}] {head} ({tag}):\n{text}")
    return "\n\n".join(blocks)


def sources_markdown(chunks):
    """One line per document, with every page/section that contributed — so a
    reference PDF cites as 'NIST.SP.800-41r1 — §3.2 Firewall Policy, p. 17'
    instead of just the filename."""
    order, locs = [], {}
    for row in chunks:
        meta = row[5] if len(row) > 5 and isinstance(row[5], dict) else {}
        name = row[1] or os.path.basename(row[2])
        if name not in locs:
            order.append(name)
            locs[name] = []
        loc = locator(meta)
        if loc and loc not in locs[name]:
            locs[name].append(loc)
    lines = [f"- {n}" + (f" — {'; '.join(locs[n])}" if locs[n] else "") for n in order]
    return "\n\n**Sources:**\n" + "\n".join(lines) if lines else ""


# ------------------------------------------------------------ provenance guard
# The model has no privileged access to its own provenance — "this came from my
# training" is a generated sentence, not a report from a subsystem. So this is a
# guard, not a detector of truth: it catches the case where the model TELLS us the
# answer is ungrounded and stops the UI from contradicting it. It cannot catch
# ungrounded output that arrives with no disclaimer, and it is not trying to.
#
# Same signal as the `check: disclaim-then-deliver` type queued for eval.py —
# deliberately one implementation, so the thing that gates the UI is the thing
# that gets scored.
# Two tiers, because "ungrounded" and "incomplete" are different claims and only
# one of them makes the citation block WRONG.
#
#   EXTERNAL — the model says this answer came from OUTSIDE the corpus. The
#     citation block is then affirmatively false and may be suppressed.
#
#   ABSENCE — the model says something you asked for is missing from the passages.
#     Very often the rest of the answer IS grounded and correctly cited — the
#     2026-08-05 turn-3 refusal is the model at its best: it delivered GOVERN and
#     MANAGE with real citations and refused MEASURE. Suppressing that block would
#     punish the ideal behaviour. So ABSENCE only ever RELABELS, never suppresses.
#
# Getting this split wrong was caught by the test cases, not by reading the code:
# a single-tier matcher scored the best turn in the vault as a violation.
EXTERNAL_PATTERNS = [
    r"not\s+(?:be\s+)?(?:sourced|drawn|taken|derived)\s+from\s+the\s+(?:provided|retrieved|given)?\s*(?:context|passages|documents|sources)",
    r"(?:based\s+on|drawn\s+from|from)\s+(?:my\s+)?general\s+knowledge",
    r"from\s+(?:my\s+)?training\s*(?:data|weights|corpus)?",
    r"(?:isn'?t|is\s+not|was\s+not|wasn'?t)\s+(?:supported|backed)\s+by\s+the\s+(?:provided|retrieved)?\s*(?:context|passages)",
    r"outside\s+(?:of\s+)?the\s+(?:provided|retrieved)?\s*(?:context|passages|knowledge\s+base)",
    r"would\s+not\s+be\s+sourced",
    r"my\s+own\s+knowledge",
]
ABSENCE_PATTERNS = [
    r"not\s+(?:present\s+|contained\s+|found\s+|included\s+|mentioned\s+)?in\s+(?:the|any)\s+(?:provided|retrieved|given)?\s*(?:context|passage|passages|documents)",
    r"I\s+don'?t\s+have\s+.{0,60}?\s+in\s+the\s+(?:provided|retrieved)?\s*(?:context|passages)",
    r"cannot\s+(?:fabricate|invent)",
    r"no\s+(?:relevant\s+)?(?:passage|passages|context)\s+(?:covers|contains|mentions)",
]
EXTERNAL_RE = re.compile("|".join(EXTERNAL_PATTERNS), re.IGNORECASE)
ABSENCE_RE = re.compile("|".join(ABSENCE_PATTERNS), re.IGNORECASE)

GUARD_EXTERNAL_HEADER = (
    "\n\n> ⚠️ **Not grounded in the knowledge base.** The model stated that this answer "
    "was not drawn from the retrieved passages. The documents below were returned by "
    "retrieval for this query — they are **not** evidence for the answer above.\n"
)
GUARD_ABSENCE_HEADER = (
    "\n\n> ℹ️ **Partially grounded.** The model stated that some of what you asked for is "
    "not in the retrieved passages. Citations below cover only the part it answered.\n"
)
GUARD_SUPPRESS_LINE = (
    "\n\n> ⚠️ **No sources cited.** The model stated this answer was not drawn from the "
    "retrieved passages, so the citation list has been withheld.\n"
)
ABSTAIN_NOTE = (
    "\n\n> ⚠️ **No strongly matching passages.** Nothing in the knowledge base scored "
    "above the relevance floor for this question, so no sources are listed. Anything "
    "above is the model's own knowledge, not your documents.\n"
)
WEAK_CONTEXT_PROMPT = (
    "\n\nIMPORTANT: none of the context passages above scored above the relevance "
    "floor for this question. Treat them as probably irrelevant. Say plainly that the "
    "knowledge base does not cover this, and do not cite them."
)


THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def classify_disclaimer(answer: str):
    """Return ('external' | 'absence' | None, matched_text).

    Matches the ANSWER only, never the reasoning trace. Ornith is a reasoning
    model and opens every turn with <think>…</think>. With --reasoning-parser
    qwen3 active the trace arrives in `reasoning_content` and never reaches here;
    without it, it lands in `content` — and a model that weighs "is this in my
    passages?" out loud would trip every pattern above while still producing a
    fully grounded answer. Strip it either way.
    """
    if not answer:
        return None, ""
    text = THINK_RE.sub("", answer)
    m = EXTERNAL_RE.search(text)
    if m:
        return "external", m.group(0)
    m = ABSENCE_RE.search(text)
    if m:
        return "absence", m.group(0)
    return None, ""


def guard_sources(src_md: str, answer: str) -> str:
    """Reconcile the citation block with what the model said about its own sourcing.

    Returns the block to actually append. Logs every trigger so
    `docker logs paradedb-rag-api | grep '\\[guard\\]'` gives a countable rate —
    the point is to learn HOW OFTEN this happens, not just to hide it once. That
    count is the baseline the eval's `check: disclaim-then-deliver` needs.
    """
    if not src_md or DISCLAIM_GUARD == "off":
        return src_md
    if src_md == ABSTAIN_NOTE:
        return src_md      # abstention already says more than the guard could add
    kind, hit = classify_disclaimer(answer)
    if not kind:
        return src_md
    print(f"[guard] {kind} disclaimer ({DISCLAIM_GUARD}): {hit[:80]!r}")
    relabelled = src_md.replace("\n\n**Sources:**\n", "\n**Retrieved (not cited):**\n", 1)
    if kind == "absence":
        # Never suppress: the answered portion is usually correctly cited.
        return GUARD_ABSENCE_HEADER + src_md
    if DISCLAIM_GUARD == "suppress":
        return GUARD_SUPPRESS_LINE
    return GUARD_EXTERNAL_HEADER + relabelled


# --------------------------------------------- strip model-authored Sources blocks
# A heading line that is ONLY the word "sources" (any of **Sources:**, ## Sources,
# Sources:). Requiring the line to contain nothing else is what keeps a sentence
# like "check the sources: they disagree" from being treated as a heading.
SOURCES_HEADING_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__)?\s*sources\s*:?\s*(?:\*\*|__)?\s*:?\s*$",
    re.IGNORECASE)
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")


class SourcesStripper:
    """Line-oriented filter that drops the model's own Sources blocks.

    A block is a heading line, then the run of list items and blank lines that
    follows it. The run ENDS at the first non-list, non-blank line — which is
    deliberate and load-bearing: on the 2026-08-09 PowerPoint turn the model wrote
    a fake block, then the honest sentence "this is not sourced from the provided
    context", then a SECOND fake block. Swallowing everything after the first
    heading would have eaten the disclaimer — the one line worth keeping, and the
    line the provenance guard matches on.

    Streaming becomes line-buffered: at most one incomplete line is ever held.
    """

    def __init__(self):
        self.buf = ""
        self.in_block = False
        self.dropped_lines = 0

    def _keep(self, line):
        if self.in_block:
            if not line.strip() or LIST_ITEM_RE.match(line):
                self.dropped_lines += 1
                return False
            self.in_block = False          # block ended; this line is real content
        if SOURCES_HEADING_RE.match(line):
            self.in_block = True
            self.dropped_lines += 1
            return False
        return True

    def feed(self, text):
        self.buf += text
        out = []
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if self._keep(line):
                out.append(line + "\n")
        return "".join(out)

    def flush(self):
        if not self.buf:
            return ""
        line, self.buf = self.buf, ""
        return line if self._keep(line) else ""


def strip_model_sources(text: str) -> str:
    """Whole-string version for the non-streaming path."""
    if not STRIP_MODEL_SOURCES or not text:
        return text
    s = SourcesStripper()
    out = s.feed(text) + s.flush()
    if s.dropped_lines:
        print(f"[strip] removed {s.dropped_lines} line(s) of model-authored Sources")
    return out


def _flush_stripper(stripper):
    """Release the last held-back line at end of stream, and log the drop count."""
    if stripper is None:
        return []
    out = [t for t in (stripper.flush(),) if t]
    if stripper.dropped_lines:
        print(f"[strip] removed {stripper.dropped_lines} line(s) of model-authored Sources")
    return out


# ---------------------------------------------------------- generation plumbing
def resolve_model():
    """Use GEN_MODEL if pinned, else ask the backend what it's currently serving.

    Called per request on purpose: with GEN_MODEL blank and Unsloth Studio as the
    backend, swapping the loaded model in the Studio UI changes what this gateway
    uses on the very next message — no restart, no .env edit. (vLLM can't do that;
    its model is fixed at `docker run` time.)
    """
    if GEN_MODEL:
        return GEN_MODEL
    r = requests.get(f"{GEN_URL}/models", headers=GEN_HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data:
        raise RuntimeError(
            f"{GEN_URL}/models returned no models — load one in the backend UI "
            f"(Unsloth Studio) or pin GEN_MODEL in .env"
        )
    # Unsloth Studio advertises models it is NOT currently serving, flagged
    # `loaded: false` (observed 2026-07-28: a loaded 9B and an unloaded 35B).
    # Taking data[0] blindly can therefore request a model that isn't in VRAM,
    # which fails mid-stream with no clear cause. Prefer a loaded one.
    for m in data:
        if m.get("loaded"):
            return m["id"]
    return data[0]["id"]          # vLLM and others don't report `loaded` at all


def make_messages(incoming, context, weak=False):
    """Prepend our grounded system prompt + context; keep the user's turns."""
    extra = WEAK_CONTEXT_PROMPT if weak else ""
    sys = {"role": "system", "content": f"{SYSTEM_PROMPT}\n\nContext:\n{context}{extra}"}
    convo = [m for m in incoming if m.get("role") != "system"]
    return [sys] + convo


# ---------------------------------------------------------------- endpoints
@app.get("/health")
def health():
    out = {}
    try:
        with psycopg.connect(**DB, connect_timeout=5) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM chunks;")
            out["paradedb"] = f"ok ({cur.fetchone()[0]} chunks)"
    except Exception as e:
        out["paradedb"] = f"FAIL: {e}"
    try:
        embed_query("ping")
        out["embeddings"] = "ok"
    except Exception as e:
        out["embeddings"] = f"FAIL: {e}"
    try:
        out["generation"] = f"ok ({GEN_URL} — model {resolve_model()})"
    except Exception as e:
        out["generation"] = f"FAIL: {e}"
    ok = all(v.startswith("ok") for v in out.values())
    return JSONResponse(out, status_code=200 if ok else 503)


@app.get("/v1/models")
def list_models():
    """Advertise the unscoped model plus one variant per collection, so a chat
    in Open WebUI can be scoped to a single corpus by picking the model."""
    names = [MODEL_NAME] + [f"{MODEL_NAME}-{c}" for c in COLLECTIONS]
    return {"object": "list",
            "data": [{"id": n, "object": "model", "owned_by": "paradedb-rag"}
                     for n in names]}


def indexed_collections():
    """Distinct collection tags present in the index, refreshed every few minutes."""
    now = time.monotonic()
    if _collection_cache["names"] and now - _collection_cache["at"] < COLLECTION_CACHE_SECONDS:
        return _collection_cache["names"]
    with psycopg.connect(**DB, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT lower(metadata->>'collection') FROM chunks "
                    "WHERE metadata->>'collection' IS NOT NULL ORDER BY 1;")
        names = [row[0] for row in cur.fetchall() if row[0]]
    _collection_cache.update(at=now, names=names)
    return names


def search_error(status, code, **extra):
    return JSONResponse({"error": code, **extra}, status_code=status)


@app.post("/v1/search")
def search(body: dict, authorization: str = Header(default="")):
    """Hybrid retrieval with no generation: the passages a chat turn would see,
    each with its citation locator, plus whether the floor calls them weak.

    Body: {"query": str, "collection": str?, "source": "vault"|"dropzone"?, "top_k": int?}
    Logs carry counts and scores only, never the query or passage text.
    """
    if not SEARCH_TOKEN:
        return search_error(503, "search_disabled")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(supplied.encode(), SEARCH_TOKEN.encode()):
        return search_error(401, "unauthorized")
    query = body.get("query")
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000:
        return search_error(400, "invalid_query")
    try:
        known = indexed_collections()
    except Exception as e:
        print(f"[search] collection list failed ({type(e).__name__})")
        return search_error(502, "retrieval_failed")
    collection = body.get("collection") or None
    if collection is not None:
        if not isinstance(collection, str) or collection.strip().lower() not in known:
            return search_error(400, "unknown_collection", collections=known)
        collection = collection.strip().lower()
    source = body.get("source") or None
    if source is not None and source not in SEARCH_SOURCES:
        return search_error(400, "invalid_source")
    top_k = body.get("top_k", TOP_K)
    if type(top_k) is not int or not 1 <= top_k <= TOP_K:
        return search_error(400, "invalid_top_k", max_top_k=TOP_K)
    started = time.monotonic()
    try:
        chunks, stats = hybrid_retrieve(query.strip(), collection, source)
    except Exception as e:
        print(f"[search] retrieval failed ({type(e).__name__})")
        return search_error(502, "retrieval_failed")
    weak = retrieval_is_weak(stats)
    results = []
    for rank, row in enumerate(chunks[:top_k], 1):
        meta = row[5] if len(row) > 5 and isinstance(row[5], dict) else {}
        text = strip_cite_prefix(row[3]).strip()
        results.append({
            "rank": rank,
            "title": row[1] or os.path.basename(row[2] or ""),
            "source_path": row[2],
            "locator": locator(meta),
            "collection": meta.get("collection"),
            "type": meta.get("type") or "note",
            "status": meta.get("status") or "current",
            "text": text[:SEARCH_TEXT_CHARS],
            "truncated": len(text) > SEARCH_TEXT_CHARS,
        })
    print(f"[search] results={len(results)} weak={weak} collection={collection} "
          f"ms={round((time.monotonic() - started) * 1000)}")
    return {"results": results, "weak": weak,
            "top_similarity": stats.get("top_sim"), "top_rerank": stats.get("top_rerank"),
            "collections": known}


@app.post("/v1/chat/completions")
def chat_completions(body: dict):
    messages = body.get("messages", [])
    stream = bool(body.get("stream", False))
    question = next((m["content"] for m in reversed(messages)
                     if m.get("role") == "user"), "")

    # Open WebUI fires background "task" prompts (title/tag generation) at the
    # selected model. Those aren't real KB questions — pass them straight through
    # to the backend with no retrieval and no RAG wrapper.
    is_task = ("### Task:" in question) or ("### Chat History:" in question)
    # Retrieval scope: an explicit body field (custom frontend) wins; otherwise a
    # model-variant suffix ("paradedb-rag-atf") selected in Open WebUI applies.
    collection = (body.get("collection") or "").strip().lower() or None
    source = (body.get("source") or "").strip().lower() or None
    if not collection:
        requested = (body.get("model") or "")
        if requested.startswith(f"{MODEL_NAME}-"):
            suffix = requested[len(MODEL_NAME) + 1:].lower()
            if suffix in COLLECTIONS:
                collection = suffix

    if is_task:
        chunks, src_md, payload_messages = [], "", messages
    else:
        chunks, rstats = (hybrid_retrieve(question, collection, source)
                          if question else ([], {}))
        # Retrieval is NOT filtered — the model still sees every chunk. Only the
        # claim of grounding is gated.
        weak = retrieval_is_weak(rstats)
        if weak:
            src_md = ABSTAIN_NOTE if SHOW_SOURCES else ""
        else:
            src_md = sources_markdown(chunks) if SHOW_SOURCES else ""
        payload_messages = make_messages(messages, build_context(chunks), weak=weak)

    # Generation params: gateway is the authority (see GEN_TEMPERATURE above).
    if ALLOW_CLIENT_PARAMS:
        temperature = body.get("temperature", GEN_TEMPERATURE)
        max_tokens = body.get("max_tokens", GEN_MAX_TOKENS or None)
    else:
        temperature, max_tokens = GEN_TEMPERATURE, (GEN_MAX_TOKENS or None)

    payload = {
        "model": resolve_model(),
        "messages": payload_messages,
        "stream": stream,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens

    if not stream:
        r = requests.post(f"{GEN_URL}/chat/completions", json=payload,
                          headers=GEN_HEADERS, timeout=300)
        if r.status_code >= 400:
            # Pass the backend's status through instead of wrapping its error in a
            # 200 — otherwise a config fault looks like a successful empty answer.
            print(f"[chat] upstream HTTP {r.status_code}: {r.text[:300]}")
            return JSONResponse({"error": {"message": f"backend {GEN_URL} returned "
                                                      f"HTTP {r.status_code}: {r.text[:300]}",
                                           "type": "upstream_error"}},
                                status_code=r.status_code)
        data = r.json()
        if data.get("choices"):
            msg = data["choices"][0]["message"]
            raw = msg.get("content") or ""
            # Strip the model's own citation lists, then guard the gateway's.
            # The guard reads RAW on purpose: stripping removes list items, and the
            # disclaimer it matches on can sit between two fabricated blocks.
            msg["content"] = strip_model_sources(raw)
            if src_md:
                msg["content"] += guard_sources(src_md, raw)
        return JSONResponse(data)

    def sse(obj):
        return f"data: {json.dumps(obj)}\n\n"

    def delta(cid, content=None, finish=None):
        d = {"content": content} if content is not None else {}
        return {"id": cid, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": MODEL_NAME,
                "choices": [{"index": 0, "delta": d, "finish_reason": finish}]}

    def event_stream():
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        try:
            yield from _upstream_stream(cid, src_md)
        except Exception as e:
            # Once the SSE headers are out we cannot send an HTTP error status, so
            # an exception here just truncates the response and the client reports
            # something like "TransferEncodingError: Not enough data to satisfy
            # transfer length header" — which names nothing. Emit the real cause as
            # a message delta instead, then close the stream cleanly.
            print(f"[stream] upstream failed: {e}")
            yield sse(delta(cid, content=f"\n\n**Gateway error talking to {GEN_URL}:** `{e}`"))
            yield sse(delta(cid, finish="stop"))
            yield "data: [DONE]\n\n"

    def _upstream_stream(cid, src_md):
        with requests.post(f"{GEN_URL}/chat/completions", json=payload,
                           headers=GEN_HEADERS, stream=True, timeout=300) as r:
            if r.status_code >= 400:
                # Read the body before iterating — a 4xx/5xx carries a JSON error,
                # not an SSE stream, and it explains far more than the status alone.
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            # Accumulate the answer as it streams so the provenance guard can read
            # the finished text before deciding what the Sources block should say.
            # Sources are already appended at [DONE], so this costs no extra latency.
            answer_parts = []
            stripper = SourcesStripper() if STRIP_MODEL_SOURCES else None
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    # Upstream is finished. NOW append Sources as a delta, then send
                    # one clean stop + [DONE]. Appending after the model's own stop is
                    # what left Open WebUI's live view stuck — so we hold its stop back
                    # (below) and emit the only stop here, at the very end.
                    for tail in _flush_stripper(stripper):
                        yield sse(delta(cid, content=tail))
                    guarded = guard_sources(src_md, "".join(answer_parts))
                    if guarded:
                        yield sse(delta(cid, content=guarded))
                    yield sse(delta(cid, finish="stop"))
                    yield "data: [DONE]\n\n"
                    return
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                ch = obj.get("choices") or [{}]
                piece = (ch[0].get("delta") or {}).get("content")
                if piece:
                    answer_parts.append(piece)   # raw, so the guard sees everything
                    if stripper is not None:
                        kept = stripper.feed(piece)
                        if not kept:
                            # Line still incomplete, or it was a citation line we
                            # dropped. Either way there is nothing to send yet.
                            continue
                        ch[0]["delta"]["content"] = kept
                if ch[0].get("finish_reason"):   # hold back upstream's stop
                    ch[0]["finish_reason"] = None
                yield sse(obj)

            # Some backends close the stream without a final [DONE]. Before this
            # change that path silently dropped the Sources block; now it closes
            # cleanly and the guard still runs.
            for tail in _flush_stripper(stripper):
                yield sse(delta(cid, content=tail))
            guarded = guard_sources(src_md, "".join(answer_parts))
            if guarded:
                yield sse(delta(cid, content=guarded))
            yield sse(delta(cid, finish="stop"))
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
