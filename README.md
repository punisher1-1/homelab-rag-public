# Homelab RAG — Self-Hosted Hybrid Retrieval Stack

A self-hosted, curated, hybrid + reranked Retrieval-Augmented Generation stack over a
personal Obsidian knowledge base, built on consumer hardware as a prototype for
company-scale local AI.

## Why it exists

Off-the-shelf RAG platforms hid their retrieval logic (a hardcoded cosine threshold that
silently dropped correct results). This stack puts every knob — fusion, threshold,
chunking, content governance, provenance scoping — in code you control.

## Architecture

```
Obsidian vault  +  NAS approved/<collection>/
   │  ingest.py (Mac): curate → chunk (page + section) → embed
   ▼
bge-m3 embeddings (Z2 Mini T1000, embed-host:11434)
   │
   ▼
ParadeDB  (paradedb-host)   Postgres 18 + pgvector + pg_search (BM25)
   ▲                                  one `chunks` table: text + vector(1024) + metadata
   │  hybrid retrieve (BM25 + vector, RRF fused)
   │
Parade-API gateway (gateway-host:8100)  OpenAI-compatible
   │  → rerank (bge-reranker-v2-m3 via TEI, MSI node reranker-host:8080)
   │  → generate (vLLM Granite-4.1-8B-FP8, gpu-node vllm-host:8000)
   ▼
Open WebUI (docker-host openwebui-host:3000)  — chat frontend
```

One model per GPU: **embeddings** on the Z2 Mini T1000, **generation** on the gpu-node
RTX 5060 Ti, **reranking** on the MSI node.

## Components (folders in this repo)

| Folder | Runs on | What it is |
|--------|---------|-----------|
| `paradedb-personal-rag/` | Mac | The pipeline: `check_setup.py`, `ingest.py` (curation + chunking + embed), `query.py` (retrieval + rerank inspector), `eval.py` (golden-set scoring) |
| `paradedb-rag-api/` | gateway host | The gateway: OpenAI-compatible FastAPI service (retrieve → rerank → generate), Dockerized |
| `paradedb-schema.sql` | database host | The ParadeDB `chunks` table + BM25/HNSW indexes |

## Two ways in

| | Obsidian vault | Approved document store |
|---|---|---|
| How | write a note; governance rules decide if it's indexed | approve a file into `<DROP_DIR>/<collection>/` |
| For | your own knowledge — lab facts, runbooks, decisions | reference documents you didn't write — NIST publications, course readings, vendor docs |
| Citations | note title + section | title + **section + page** |
| Command | `python ingest.py --vault-only` | `python ingest.py --documents-only` |

See `paradedb-personal-rag/dropzone/README.md`.

## Key capabilities

- **Approved-document ingestion** — no tagging ceremony: subfolder = `collection` tag, so
  `query.py "…" --collection nist` searches the NIST corpus without your lab notes
  competing for rank. SHA-256 per file makes re-runs incremental.
- **Page + section citations** — PDF chunks record the page and the nearest heading
  (falling back to the PDF's bookmark outline), so an answer points at
  `NIST.SP.800-41r1 — §3.2 Firewall Policy, p. 17` instead of just a filename.
- **Hybrid search** — BM25 (`pg_search`) + dense vector (`pgvector`), fused with Reciprocal Rank Fusion.
- **Cross-encoder reranking** — retrieve wide, rerank precise (bge-reranker via TEI).
- **Content governance** — path denylist + `rag:false` opt-out + status denylist; keeps history, drafts, and speculation out of a factual KB.
- **Table-aware chunking** — whole-table chunks (enumeration) + linearized rows (pinpoint lookup).
- **Provenance scoping** — every chunk tagged with its `lab`/source; queries filter by scope (multi-tenant pattern).
- **Grounded generation** — prompt trusts current/authoritative sources over historical; never fabricates.
- **Measured, not guessed** — `eval.py` scores a golden question set (hit@k, MRR) and
  splits failures into **retrieval** (never a candidate — fix chunking/embedding/corpus)
  vs **ranking** (found but ordered badly — fix the reranker). Every vector is stamped
  with the `embed_model` and `embed_dim` that produced it.

## Measuring a change

```bash
python eval.py evalset.yaml --save runs/baseline.json   # before
# ...change chunk size / embedder / reranker...
python ingest.py --documents-only --force
python eval.py evalset.yaml --baseline runs/baseline.json   # after, with the delta
python eval.py evalset.yaml --compare                  # A/B the reranker on vs off
```

## Setup

Each component folder's README covers standing it up (plus `DEPLOY.md` where present). Every component reads its
secrets from a local `.env` (copy from the provided `.env.example`) — **secrets are never
committed**.

## Local model harness (without retrieval)

[Local Model Harness](https://github.com/punisher1-1/local-model-harness) is a separate FastAPI template
for Open WebUI → policy-controlled local inference. It provides named model profiles,
server-owned prompts and generation settings, context budgeting, authentication,
streaming, and metadata logging. It runs on port 8200 alongside the RAG gateway.
