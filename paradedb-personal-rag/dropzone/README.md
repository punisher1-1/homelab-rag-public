# Legacy local document-store template

The active document store is the directory configured by `DROP_DIR`. For the NAS-backed
deployment, point it at `rag-store/documents/approved`. This tracked folder remains as a
local collection-layout template.

```
approved/
  nist/     ← NIST SP 800-* PDFs        collection = "nist"
  it286/    ← course readings           collection = "it286"
  vendor/   ← product/vendor docs       collection = "vendor"
  general/  ← anything else             collection = "general"
  <file>    ← loose at the root         collection = "general"
```

## Use it

```bash
source ~/paradedb-rag-venv/bin/activate
cd "…/Homelab:AI/homelab-rag/paradedb-personal-rag"

python ingest.py --documents-only               # ingest approved documents
python query.py "what does 800-41 say about default deny" --collection nist
```

**The subfolder name is the collection tag.** That tag is what lets you ask a question
of *only* the NIST documents, so your home-lab notes don't compete for rank — and
vice versa. To add a collection, just make a new folder.

## Two rules worth knowing

1. **The filename is the citation title.** `NIST.SP.800-41r1.pdf` cites as
   `NIST.SP.800-41r1`. PDFs' own embedded titles are often missing or generic
   ("User Manual"), so the filename — which you control — wins. Name accordingly.
   (Exception: a Docling export literally named `file.json`, `output.json`, etc.
   falls back to the original PDF's name from `origin.filename`.)

2. **Re-running is cheap.** Each file's SHA-256 is stored with its chunks; a file
   that hasn't changed is skipped without re-embedding. Edit a file and re-run to
   update just that one. Use `--force` to re-embed anyway (e.g. after changing the
   chunking rules).

## What gets indexed

| Format | Structure | Page citations |
|--------|-----------|----------------|
| `.json` — **Docling export (best)** | real heading levels, real tables, correct reading order, running headers/footers stripped | ✅ authoritative, from `prov.page_no` |
| `.pdf` — pymupdf4llm | headings inferred from font size; two-column layouts can interleave | ✅ from the page extractor |
| `.docx` | heading styles + tables | ❌ no pages in the format |
| `.md` / `.txt` | headings only | ❌ |

### The Docling path — for anything structured (NIST, standards, textbooks)

```bash
# wherever docling is installed:
docling --to json --image-export-mode placeholder NIST.SP.800-41r1.pdf
# then place the resulting .json into approved/nist/
```

**Convert to JSON, not markdown.** Markdown is a lossy final render — it has no pages, so
a markdown export throws away the page numbers this pipeline cites. JSON is Docling's
actual document model and keeps `prov.page_no` on every item.

`--image-export-mode placeholder` matters too: an *embedded*-image export of NIST SP
800-41 measured **79% base64 by file size**. In JSON that bloat is at least quarantined in
the `pictures` array (the adapter skips it); in markdown it lands inline and gets chunked
and embedded as garbage.

**Same stem = automatic upgrade.** If `NIST.SP.800-41r1.json` sits next to
`NIST.SP.800-41r1.pdf` or `.md`, the JSON wins, the others are skipped, and any chunks
previously written from them are deleted. So converting a document to Docling later is a
one-step operation — drop the JSON in and re-run.

### The plain-PDF path

Fine for simple, single-column, text-based PDFs. Section comes from the nearest heading,
falling back to the PDF's bookmark outline. A **scanned** PDF with no text layer yields
nothing — OCR it or run it through Docling first.

## Housekeeping

Files here are **not** committed to git (see `.gitignore`) — the folder structure is,
the documents aren't. The source files stay put; ParadeDB only holds derived text and
vectors. Deleting a file from this folder does **not** remove its chunks — re-run with
`--reset` (full rebuild) or delete its rows by `source_path`.
