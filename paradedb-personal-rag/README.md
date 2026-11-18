# ParadeDB Personal RAG

A self-hosted hybrid-search knowledge base over your Obsidian vault and PDFs,
backed by **ParadeDB** (Postgres + `pgvector` + `pg_search`) on the database host.

Unlike Omni, the retrieval logic lives in these scripts — you own the similarity
threshold and the fusion, nothing is hidden in a compiled binary.

## What each piece does


| File             | Role                                                                                            |
| ---------------- | ----------------------------------------------------------------------------------------------- |
| `check_setup.py` | Verifies the DB, both extensions, and the embedding endpoint before you ingest. Run this first. |
| `ingest.py`      | Reads vault notes and approved documents → chunks → embeds → writes to ParadeDB.               |
| `query.py`       | Hybrid search: runs BM25 + vector legs, fuses with RRF, prints ranked results.                  |
| `.env`           | Your connection details and secrets (copy from `.env.example`).                                 |




## The pipeline in one picture

```
Obsidian .md / PDFs
      │  read + strip frontmatter / extract PDF text
      ▼
   chunk (~1200 chars, 150 overlap)
      │  POST /api/embed  (bge-m3 on embed-host)
      ▼
 1024-dim vectors ──► ParadeDB chunks table
                         ├─ chunk_text  → BM25 index   (exact words)
                         └─ embedding   → HNSW index   (meaning)
                                   ▲
   query.py ── your question ──────┘  runs BOTH, fuses with RRF
```



## One-time setup (on the Mac where your vault lives)



# NOTE: this project folder's name contains a ":" (Homelab:AI). The colon is the



# PATH separator on macOS/Linux, so Python REFUSES to create a venv inside it.



# Create the venv in your home dir instead — its location doesn't matter, only



# that it's activated. Run the scripts from the project folder so they find .env.

```bash
# 1. Create the venv somewhere colon-free
python3 -m venv ~/paradedb-rag-venv
source ~/paradedb-rag-venv/bin/activate

# 2. cd into the project folder (note the escaped spaces)
cd paradedb-personal-rag   # from the repo root

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your .env from the template and fill in the DB password
cp .env.example .env
#   then edit .env  ->  set DB_PASSWORD, confirm OBSIDIAN_VAULT and PDF_DIR
```



## Run it

```bash
source ~/paradedb-rag-venv/bin/activate    # each new terminal

python check_setup.py               # confirm all plumbing is green

# Set DROP_DIR to the NAS `rag-store/documents/approved` folder, then:
python ingest.py --documents-only           # approved reference documents only
python ingest.py --reset --documents-only   # wipe and rebuild from approved documents
python ingest.py --vault-only               # Obsidian markdown only

python query.py "NIST access control families"
python query.py "reset the OPNsense firewall" --k 8
```

On the n8n/Docling deployment, prefer the reviewer-managed reset so rebuilt rows
retain canonical `rag://approved/...` source identities and ingestion receipts are
archived consistently:

```bash
docker exec rag-reviewer python request_rebuild.py \
  --confirm "TRUNCATE AND REBUILD CHUNKS"
```



## Seeing the Omni problem for yourself

Run the same query twice:

```bash
python query.py "your paraphrased question" --min-sim 0.0     # our default: no floor
python query.py "your paraphrased question" --min-sim 0.70    # Omni's hidden gate
```

With `--min-sim 0.70` you'll watch the vector leg drop the ~0.67-similarity
chunks — exactly what Omni was silently doing. With `0.0`, they come back and RRF
ranks them properly. That difference is the whole reason this project exists.

## Notes

- **PDF quality:** `pymupdf4llm` is fast and fine for most docs. For table-heavy
NIST publications, run them through **Docling** first and drop the
resulting markdown into the vault instead — better structure preservation.
- **Re-running is safe:** each file's old chunks are deleted before its new ones
are inserted, so editing a note and re-ingesting just updates it.
- **Hashes are full SHA-256:** manifests and ingested chunk metadata use the full
64-character digest. The first ingest after upgrading from the older 16-character
incremental key will re-embed existing files once; subsequent unchanged runs skip
them normally. A legacy short hash in `manifest.yaml` is rejected and must be
replaced with the document's complete SHA-256.
- **Code keeps its line breaks:** Docling joins every code item's lines with
spaces. For a Docling `.json`, ingest.py finds the raw PDF it was converted from
(`origin.filename` under `documents/raw/*/`, used only if its sha256 matches
`origin.binary_hash`) and reads each code item's lines and indentation from
the PDF text layer (`code_lines.py`). If a code font has no ToUnicode map (its
text reads as a shifted cipher, like IN254's FreeMono), glyphs are decoded by
exact outline match against a font of the same name in
`documents/reference-fonts/` (for IN254: GNU FreeFont 20120503 `FreeMono.ttf`
and `FreeMonoBold.ttf`). Anything that can't be read exactly keeps Docling's
text. The run prints `code lines: N/M code items from the PDF text layer`. The
approved JSON is not changed, so re-index an existing book with `--force`.
- **Embedding dims must match:** the column is `vector(1024)` because bge-m3
outputs 1024 dims. If you ever change the embed model, the dimension must match
or retrieval breaks silently.

```

```
