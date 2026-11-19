#!/usr/bin/env python3
"""
ingest.py — load Obsidian markdown, PDFs, and DROP-ZONE documents into ParadeDB
for hybrid search, with CONTENT GOVERNANCE and PAGE/SECTION PROVENANCE.

Pipeline (one file at a time):
    read text  ->  split into chunks (tracking page + section)
    ->  embed each chunk (bge-m3)  ->  delete old rows for that file  ->  insert

--------------------------------------------------------------------------
THREE WAYS CONTENT GETS IN
--------------------------------------------------------------------------
1. VAULT      (OBSIDIAN_VAULT)  — the Obsidian notes, filtered by governance.
2. PDF_DIR    (PDF_DIR)         — the original flat PDF folder. Still works.
3. APPROVED DOCUMENT STORE (DROP_DIR) — Approved reference documents, grouped
                                        into collection subfolders, with page +
                                        section citations and without the vault
                                        governance denylist.

    approved/
      nist/       NIST SP 800-*.pdf     -> collection "nist"
      it286/      course readings       -> collection "it286"
      vendor/     product docs          -> collection "vendor"
      <file>.pdf  (loose, at the root)  -> collection "general"

  The SUBFOLDER NAME IS THE COLLECTION TAG. Query one collection at a time
  (`query.py "..." --collection nist`) so reference documents and home-lab
  notes stop competing for rank.

  Accepted: .json (a DOCLING export — best structure + real pages), .pdf, .docx,
  .md, .txt. For anything structured (NIST, standards, textbooks) prefer Docling:
      docling --to json --image-export-mode placeholder NIST.SP.800-41r1.pdf
  Convert to JSON, NOT markdown — markdown has no pages, so a markdown export
  silently throws away the page numbers this pipeline cites. A `.json` also
  SUPERSEDES the same stem's .pdf/.md (their old chunks are deleted).

--------------------------------------------------------------------------
CITATIONS — why chunks now carry page + section
--------------------------------------------------------------------------
A retrieved chunk is useless for schoolwork if you can't find it again in the
source. Every PDF chunk now records:

    page     the 1-based PDF page it came from
    section  the nearest heading above it — taken from the in-page markdown
             heading if there is one, else from the PDF's own bookmark
             outline (`get_toc()`), which NIST publications populate properly

and the chunk text itself is prefixed with a human-readable citation line:

    [NIST SP 800-41r1 · §3.2 Firewall Policy · p. 17]

That prefix is deliberate: it is indexed too, so BM25 can match on "800-41"
or a section number, and the LLM can quote the citation straight out of the
context instead of inventing one.

--------------------------------------------------------------------------
GOVERNANCE (vault only — the approved store is explicitly curated by you)
--------------------------------------------------------------------------
  1. PATH DENYLIST  — whole files/folders never ingested (history, drafts, courses).
  2. rag: false     — a note's frontmatter can opt it out individually.
  3. status: TAG    — frontmatter `status:`; chunks are tagged with it and
                      RAG_EXCLUDE_STATUSES decides which to drop.

--------------------------------------------------------------------------
INCREMENTAL — re-running is cheap
--------------------------------------------------------------------------
Each file's SHA-256 is stored on its chunks. On a re-run, a file whose hash is
unchanged is skipped without re-embedding. `--force` overrides.

Usage:
    python ingest.py                         # vault + PDF_DIR + approved documents
    python ingest.py --documents-only        # just approved documents  <-- common
    python ingest.py --vault-only
    python ingest.py --pdfs-only
    python ingest.py --path /path/to/approved/nist/SP800-53r5.pdf  # one file
    python ingest.py --path ~/Desktop/report.pdf --collection scratch
    python ingest.py --path /Volumes/.../approved/linux/pve-admin-guide.json --force \
        --source-path rag://approved/linux/pve-admin-guide.json   # keep reviewer identity
    python ingest.py --force --documents-only # re-embed even if unchanged
    python ingest.py --reset                 # wipe the table, then re-ingest
"""
import os
import re
import json
import hashlib
import argparse
import pathlib
import requests
import psycopg
from collections import Counter
from dotenv import load_dotenv

import code_lines
import corpus_manifest
from corpus_manifest import Manifest, ManifestError
from parse_check import is_web_chrome, repair_glyphs, repair_item_text

load_dotenv()

DB = dict(
    host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
)
OLLAMA_URL = os.environ["OLLAMA_URL"].rstrip("/")
EMBED_MODEL = os.environ["EMBED_MODEL"]
VAULT = os.environ.get("OBSIDIAN_VAULT", "").strip()
PDF_DIR = os.environ.get("PDF_DIR", "./pdfs").strip()
DROP_DIR = os.environ.get("DROP_DIR", "./dropzone").strip()

# Folders inside the vault we never even walk.
VAULT_EXCLUDES = {".obsidian", ".trash", ".git", "node_modules"}

# File types the approved document store accepts. `.json` = a Docling export.
DROP_EXTS = {".pdf", ".md", ".markdown", ".txt", ".docx", ".json"}

# ---------------------------------------------------------------------------
# CONTENT GOVERNANCE  (applies to the VAULT walk only)
# ---------------------------------------------------------------------------
# Layer 1 — path denylist. Case-insensitive substring match on the full path.
# These categories pollute a factual KB, so they never get ingested:
RAG_DENYLIST = [
    "/log.md",          # append-only history — full of superseded facts (old IPs, retired VMs)
    "/raw/",            # unprocessed source transcripts (the wiki is the synthesized version)
    "/templates/",      # note templates
    "homelab-roadmap",  # speculative roadmap (or tag it `status: plan` instead — see Layer 3)
    "it286", "it299",   # course material — not knowledge-base facts
    "discussion-",      # discussion posts / peer replies
    "-draft", "essay",  # drafts and essays
]

# Layer 3 — status EXCLUSION (denylist, not allowlist). Ingest everything by
# default; drop a note only if its frontmatter `status:` CONTAINS one of these
# keywords (speculative or dead). Substring match, so "Fully retired",
# "working-draft", and "planning" all catch. Denylisting fails safe — a status
# value you didn't anticipate ("active", "deployed", "published") is kept, not lost.
EXCLUDE_STATUS_KEYWORDS = {s.strip().lower() for s in os.environ.get(
    "RAG_EXCLUDE_STATUSES",
    "draft,outline,planning,plan,propos,idea,placeholder,in-progress,wip,"
    "retire,deactiv,deprecat,archiv,obsolet,supersed,historical,shelved"
).split(",") if s.strip()}


def status_excluded(status: str) -> bool:
    s = (status or "").lower()
    return any(k in s for k in EXCLUDE_STATUS_KEYWORDS)


CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
EMBED_BATCH = 32


def is_denied(path: pathlib.Path) -> bool:
    p = str(path).lower()
    return any(pat.lower() in p for pat in RAG_DENYLIST)


def parse_lab_folders(raw: str) -> list[tuple[str, str]]:
    """RAG_LAB_FOLDERS: comma-separated `folder name=lab` pairs, for example
    `Work Project=work,Network Team=netops`. A note under a folder with that name
    (any depth, case-insensitive) gets that lab tag. Kept in .env so the code
    carries no deployment names."""
    rules = []
    for item in raw.split(","):
        folder, sep, lab = item.partition("=")
        folder, lab = folder.strip().strip("/").lower(), lab.strip().lower()
        if sep and folder and lab:
            rules.append((f"/{folder}/", lab))
    return rules


RAG_LAB_FOLDERS = parse_lab_folders(os.environ.get("RAG_LAB_FOLDERS", ""))


def lab_for(path: pathlib.Path, meta: dict, rules=None) -> str:
    """Provenance/scope tag (the production 'stamp source at ingestion' step).
    Explicit frontmatter `lab:` wins; otherwise inferred from the folder rules in
    RAG_LAB_FOLDERS so you never hand-tag files; anything else is `home`."""
    if meta.get("lab"):
        return meta["lab"].strip().lower()
    p = str(path).lower()
    for fragment, lab in (RAG_LAB_FOLDERS if rules is None else rules):
        if fragment in p:
            return lab
    return "home"


def file_sha(path: pathlib.Path) -> str:
    """Full SHA-256 content hash used as the incremental-ingest identity."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
def parse_frontmatter(raw: str):
    """Return (meta: dict, body: str). Handles a leading --- YAML block."""
    meta = {}
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            for line in raw[3:end].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip().lower()] = v.strip().strip('"').strip("'")
            return meta, raw[end + 4:].strip()
    return meta, raw.strip()


def read_markdown(path: pathlib.Path):
    raw = path.read_text(encoding="utf-8", errors="ignore")
    meta, body = parse_frontmatter(raw)
    title = meta.get("title") or path.stem
    return title, body, meta


def read_text(path: pathlib.Path):
    return path.stem, path.read_text(encoding="utf-8", errors="ignore").strip(), {}


def read_docx(path: pathlib.Path):
    """Word -> markdown-ish text. Heading STYLES become '#' headings so the
    section tracker can see them. Requires python-docx (optional dependency)."""
    from docx import Document
    doc = Document(str(path))
    lines = []
    for para in doc.paragraphs:
        txt = para.text.strip()
        if not txt:
            lines.append("")
            continue
        style = (para.style.name or "").lower()
        m = re.match(r"heading (\d)", style)
        if m:
            lines.append("#" * min(int(m.group(1)), 6) + " " + txt)
        elif style.startswith("title"):
            lines.append("# " + txt)
        else:
            lines.append(txt)
    # tables -> markdown tables so the table-aware chunker can linearize them
    for tbl in doc.tables:
        rows = [[c.text.strip().replace("|", "/") for c in r.cells] for r in tbl.rows]
        if len(rows) >= 2:
            lines.append("")
            lines.append("| " + " | ".join(rows[0]) + " |")
            lines.append("|" + "---|" * len(rows[0]))
            for r in rows[1:]:
                lines.append("| " + " | ".join(r) + " |")
            lines.append("")
    return path.stem, "\n".join(lines).strip(), {}


def read_pdf(path: pathlib.Path):
    """Whole-document markdown — used by the legacy PDF_DIR path (no paging)."""
    import pymupdf4llm
    return path.stem, pymupdf4llm.to_markdown(str(path)).strip(), {}


def read_pdf_pages(path: pathlib.Path):
    """
    PAGE-AWARE extraction (the citation feature).

    Returns (title, pages, section_map, doc_title) where
        pages       = [(page_no_1based, markdown_text), ...]
        section_map = {page_no: "1.2 Section Title"} built from the PDF's
                      bookmark outline, used as the fallback when a page has
                      no visible markdown heading of its own.
        doc_title   = the title embedded in the PDF's own metadata (kept for
                      reference; often blank or wrong on scanned documents)

    TITLE POLICY: the FILENAME is the citation title, because you control it and
    it is what you would actually cite ("NIST.SP.800-41r1"). Embedded PDF titles
    are frequently missing or generic ("User Manual"), so they are stored as
    metadata only. Name the file the way you want it cited.
    """
    import pymupdf4llm
    import pymupdf

    doc = pymupdf.open(str(path))
    toc = doc.get_toc() or []            # [[level, title, start_page_1based], ...]
    n_pages = doc.page_count
    doc_title = (doc.metadata or {}).get("title") or ""
    doc.close()

    # Roll the outline forward: every page inherits the last section that started
    # on or before it. Deeper (more specific) entries win on the same page.
    section_map, current = {}, ""
    starts = {}
    for level, title, page in toc:
        if page and page >= 1:
            starts.setdefault(page, []).append((level, clean_heading(title or "")))
    for p in range(1, n_pages + 1):
        if p in starts:
            current = sorted(starts[p], key=lambda x: x[0])[-1][1] or current
        section_map[p] = current

    pages = []
    for seq, pg in enumerate(pymupdf4llm.to_markdown(str(path), page_chunks=True), 1):
        text = (pg.get("text") or "").strip()
        if not text:
            continue
        # pymupdf4llm puts the 1-based page number in metadata["page_number"].
        # (Older builds used "page"; fall back to document order if neither exists.)
        pmeta = pg.get("metadata") or {}
        page_no = pmeta.get("page_number") or pmeta.get("page") or seq
        pages.append((int(page_no), text))

    return path.stem, pages, section_map, doc_title.strip()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def chunk_prose(text: str):
    text = re.sub(r"\n{3,}", "\n\n", text)
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, current = [], ""
    for para in paras:
        if len(current) + len(para) + 2 <= CHUNK_CHARS:
            current = f"{current}\n\n{para}" if current else para
        else:
            if current:
                chunks.append(current)
            if len(para) > CHUNK_CHARS:
                for i in range(0, len(para), CHUNK_CHARS - CHUNK_OVERLAP):
                    chunks.append(para[i:i + CHUNK_CHARS])
                current = ""
            else:
                current = para
    if current:
        chunks.append(current)
    if CHUNK_OVERLAP > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            overlapped.append(chunks[i - 1][-CHUNK_OVERLAP:] + "\n\n" + chunks[i])
        chunks = overlapped
    return [c for c in chunks if c.strip()]


def _is_table_sep(line: str) -> bool:
    """A markdown table separator row, e.g. |---|:--:|---|."""
    s = line.strip()
    return "|" in s and "-" in s and re.fullmatch(r"[\s|:\-]+", s) is not None


def chunk_markdown(title: str, text: str):
    """
    Table-aware chunking. Prose is chunked normally; markdown TABLES are
    'linearized' — each row becomes a self-contained line using the header names
    as field labels, prefixed with the doc title + nearest heading. A raw split
    table row is a wall of pipes with no semantic signal and embeds/reranks
    terribly; linearized, "IP: paradedb-host; Host: ParadeDB" matches a
    query like "ParadeDB IP" directly.
    """
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = text.split("\n")
    in_code = fence_mask(lines)
    chunks, prose = [], []
    heading = ""

    def flush_prose():
        joined = "\n".join(prose).strip()
        # skip a lone heading (happens when a heading sits right above a table)
        if joined and not (joined.startswith("#") and "\n" not in joined):
            chunks.extend(chunk_prose(joined))
        prose.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        # a table = a header row immediately followed by a separator row
        if ("|" in line and i + 1 < len(lines)
                and _is_table_sep(lines[i + 1]) and not _is_table_sep(line)):
            flush_prose()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            ctx = title + (f" — {heading}" if heading else "")
            i += 2
            row_lines = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                parts = [f"{h}: {c}" for h, c in zip(header, cells) if c and h]
                if not parts:
                    parts = [c for c in cells if c]
                if parts:
                    row_lines.append("; ".join(parts))
                i += 1
            if row_lines:
                # (a) whole-table chunk for ENUMERATION ("list all X"), if it fits
                whole = f"[{ctx}] full list —\n" + "\n".join(row_lines)
                if len(whole) <= 2800:
                    chunks.append(whole)
                # (b) one chunk per row for PRECISE single-fact lookups
                chunks.extend(f"[{ctx}] " + rl for rl in row_lines)
            continue
        if line.startswith("#") and not in_code[i]:
            heading = line.lstrip("#").strip()
        prose.append(line)
        i += 1
    flush_prose()
    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# DOCLING JSON  (the high-fidelity path for structured PDFs — NIST et al.)
# ---------------------------------------------------------------------------
# WHY JSON AND NOT MARKDOWN:
#   Docling reads a PDF far better than pymupdf4llm does — real heading levels,
#   real table objects, correct reading order on two-column layouts. But its
#   MARKDOWN export is a lossy final render: markdown has no pages, so exporting
#   to .md throws away the page numbers this pipeline cites. Its JSON export is
#   the actual document model — every item keeps `prov[].page_no`. So: convert
#   to JSON, and let this adapter walk the model.
#
#   Convert with (placeholder image mode keeps base64 blobs OUT of the text —
#   an embedded-image export of NIST SP 800-41 measured 79% base64 by size):
#       docling --to json --image-export-mode placeholder NIST.SP.800-41r1.pdf
#
# Items are re-packed into the SAME chunker used everywhere else, so the
# table-aware behaviour (whole-table chunk + linearized rows) still applies.

# Structural labels that are navigation furniture, not content.
DOCLING_SKIP_LABELS = {"page_header", "page_footer", "picture", "furniture"}
# Labels that carry prose we want to index.
DOCLING_TEXT_LABELS = {"text", "paragraph", "list_item", "caption", "footnote",
                       "code", "formula", "checkbox_selected", "checkbox_unselected"}
DOCLING_HEADING_LABELS = {"title", "section_header"}

# --- Heading levels (Docling release of 2026-08-07) -------------------------
# Docling now infers section-header levels 1-6 from PDF bookmarks, numbering and
# font style. Before that release EVERY section_header came back as level 1, so
# this parser could only ever take "the nearest heading above the chunk".
#
# Measured on atf-p-5320-8.pdf, old parse vs new parse of the same PDF
# (2026-08-07, both schema 1.10.0):
#
#   section_header items   371 -> 593      list_item  559 -> 342
#   chunks                 777 -> 825      indexed body chars  624,383 -> 605,222
#   sub-200-char chunks   12.0% -> 15.6%   section labels >80 chars   23 -> 85
#
# Both halves of that matter, and they pull in opposite directions:
#
#   1. The level is USABLE. A breadcrumb ("CHAPTER 2. CLASSIFICATION > 2.1.5 Any
#      Other Weapon") tells a reader WHERE a chunk sits, which is the documented
#      2026-07-24 defect: a chunk's nearest heading is not always a description
#      of the chunk. Flat level-1 headings cannot express that.
#
#   2. The level is also a JUNK FILTER, and this is the load-bearing part. The
#      same release promoted ~220 list items to section_header - form fields and
#      sentence fragments like "Number of firearms covered by the application;".
#      A heading here does two things: it forces a chunk boundary, and its text
#      leaves the body buffer to become a LABEL. The label is not lost - it is
#      stamped back into chunk_text by citation_label() - but only onto chunks
#      that follow it. So a promoted heading survives if body text follows it
#      before the next heading, and is ERASED if the next item is another
#      heading. A run of consecutive form fields is exactly that case, and the
#      fields annihilate each other. Verified on the 2026-08-07 parse of
#      atf-p-5320-8: "Export the firearm(s) within four years of importation."
#      survives once (as a label); "Number of firearms covered by the
#      application;" reaches the index ZERO times. Raw body text dropped 19,161
#      chars (-3.1%) and sub-200-char fragments rose 12.0% -> 15.6%.
#
# A heading is therefore only honoured when it is shallow enough AND short
# enough to plausibly be a heading; anything else is treated as ordinary body
# text, which restores it to the index unconditionally.
#
# BACKWARD COMPATIBILITY, precisely: the LEVEL cap is a no-op on pre-release
# parses, because everything in them is level 1. The LENGTH filter is not - it
# also rejects the sentence-length "headings" those older parses contained
# (measured: +5,315 chars restored on the old atf-p-5320-8 parse, and the count
# of over-long section labels drops 23 -> 0). That is a deliberate improvement,
# not an accident, but it does mean re-ingesting an old JSON is NOT a no-op.
#
# ⛔ CORRECTED 2026-08-07, SAME DAY, BY MEASUREMENT — the level cap now DEFAULTS
# OFF (99). It shipped at 4 for a few hours on reasoning that the eval disproved.
#
# The reasoning was: "deep headings are junk." That is wrong. Docling assigns a
# level from VISUAL PROMINENCE — font size, weight, numbering — so the level
# says how big the type is, NOT how important the content is. Those are the same
# thing in some documents and not in others:
#
#   atf-p-5320-8   levels {1:14, 2:34, 3:206, 4:182, 5:54, 6:103}
#   atf_p_5300.4   levels {1:51, 2:135, 3:241, 4:68, 5:2,  6:660}   <- 57% at 5-6
#
# In 5300.4 the ENTIRE (N1)-(N27) NFA question-and-answer section renders at
# level 6, because it is set in smaller type than the chapter titles. A cap of 4
# rejected 662 of 1,157 headings and destroyed every "(N1)"-style section
# identifier — the exact string `evalset.yaml` matches on, and the exact string a
# citation needs to be useful.
#
# Caught by the 2026-08-07 three-arm A/B: on the multi-hop benchmark question the
# clause {file: atf_p_5300.4, section: "(N1)"} scored MISS in the filtered arms
# and RANK in the unfiltered one. The filter, not the corpus, was the cause.
# Measured across both ATF documents:
#
#   cap=4  -> 0 chunks carry an "(Nx)" label
#   cap=99 -> 16 chunks carry one
#
# So a single global level cap cannot be right: it is a per-document property
# masquerading as a setting. Left tunable for a corpus where levels are known to
# be trustworthy, defaulted off because they are not trustworthy in general.
#
# DOCLING_MAX_HEADING_CHARS survives as the default filter, because it does NOT
# depend on the document: a "heading" longer than 80 characters is a sentence in
# any corpus. It is also what removes the genuine damage — a 768-character
# statutory paragraph was classified as a section_header in the handbook and
# became the citation string on a rank-1 result in the unfiltered arm.
DOCLING_MAX_HEADING_LEVEL = int(os.environ.get("DOCLING_MAX_HEADING_LEVEL", "99"))
# How many ancestor levels to keep in the section label. 1 = leaf only, which is
# the pre-2026-08-07 behaviour and the DEFAULT.
#
# Depth > 1 renders a breadcrumb ("CHAPTER 2. ... > 2.1.5 Any Other Weapon") and
# is OFF by default because it is measurably wrong on this corpus today: a PDF
# table of contents emits every chapter heading before any content, so the last
# ToC entry stays on the outline stack and mislabels the first real chapter.
# Observed on atf-p-5320-8: "CHAPTER 5. QUALIFYING TO DO BUSINESS IN NFA
# FIREARMS > Section 1.1 History of the National Firearms Act". Turning this on
# needs ToC detection first. Left in place so it can be A/B'd, not assumed.
DOCLING_SECTION_DEPTH = int(os.environ.get("DOCLING_SECTION_DEPTH", "1"))
# Prose-as-heading rejection. Real headings are short; 85 of the new parse's 334
# distinct labels are over 80 chars and are sentences. Note the label is stamped
# into chunk_text, so a sentence-length label is also BM25 dilution on every
# chunk in its section.
DOCLING_MAX_HEADING_CHARS = int(os.environ.get("DOCLING_MAX_HEADING_CHARS", "80"))


def _heading_level(item) -> int:
    """Docling's inferred level, defaulting to 1 for pre-2026-08-07 exports
    (and for `title`, which sits above the numbered hierarchy)."""
    try:
        return max(1, int(item.get("level") or 1))
    except (TypeError, ValueError):
        return 1


def _is_real_heading(text: str, level: int) -> bool:
    """Shallow enough and short enough to be a heading rather than promoted
    body text. Returning False sends the item to the index as prose."""
    return bool(text) and level <= DOCLING_MAX_HEADING_LEVEL \
        and len(text) <= DOCLING_MAX_HEADING_CHARS


def _section_path(stack: dict, level: int, text: str) -> str:
    """Maintain an outline stack and render the trailing breadcrumb.

    Mutates `stack` in place: a level-2 heading clears every deeper level, so a
    stale subsection cannot leak into the next chapter's labels."""
    for deeper in [lv for lv in stack if lv > level]:
        del stack[deeper]
    stack[level] = text
    chain = [stack[lv] for lv in sorted(stack)]
    return " > ".join(chain[-DOCLING_SECTION_DEPTH:])


def _deref(ref, doc):
    """Resolve '#/texts/3' against the document. Handles both the modern
    {'cref': ...} and the older {'$ref': ...} spellings."""
    if isinstance(ref, dict):
        ref = ref.get("cref") or ref.get("$ref") or ""
    parts = str(ref).lstrip("#/").split("/")
    if len(parts) != 2:
        return None
    bucket, idx = parts
    try:
        return doc.get(bucket, [])[int(idx)]
    except (ValueError, IndexError, TypeError):
        return None


def _walk_docling(node, doc, seen=None):
    """Yield items in READING ORDER by following body.children depth-first.
    Reading order matters: it's what Docling gets right and a naive text dump
    gets wrong on multi-column pages."""
    seen = seen if seen is not None else set()
    for child in (node.get("children") or []):
        item = _deref(child, doc)
        if item is None:
            continue
        ref = item.get("self_ref")
        if ref in seen:
            continue
        seen.add(ref)
        yield item
        if item.get("children"):
            yield from _walk_docling(item, doc, seen)


def _item_page(item):
    prov = item.get("prov") or []
    return prov[0].get("page_no") if prov else None


def printed_page_map(doc) -> dict:
    """PDF page index -> the page number PRINTED on that page, from the document's
    own footers. Added 2026-08-10.

    WHY: `page` above is the 1-based PDF index. Published standards carry covers,
    notices and roman-numeral front matter, so the printed number is lower — by 5
    on nist.ai.100-1 (PDF 6 = "Page 1", PDF 12 = "Page 7"). Citing the PDF index
    sends a reader who checks it to the wrong section, which makes a CORRECT answer
    look fabricated. That is worse than a silent error: it breaks the human
    verification loop the citations exist to support.

    Reads doc["texts"] FLAT rather than walking body: footers carry
    content_layer="furniture" and are double-filtered in read_docling_json, and a
    flat read needs no state and no reading order.

    Returns a per-page MAP, not one offset, so documents that restart numbering or
    carry unnumbered plates come out right with no special-casing. Front matter
    ("i", "Page ii") fails the \\d+$ match and is skipped — correctly, those pages
    have no arabic number to cite. Missing keys mean printed_page=None downstream.

    > [!important] 2026-08-18 — CHAPTER-RELATIVE PAGE NUMBERS WERE BEING TRUNCATED.
    > The old pattern was `(\\d+)\\s*$` wrapped in `int()`, which keeps only the
    > digits after the hyphen. Measured on the TxDOT Landscape & Aesthetics manual:
    >
    >     PDF page 8  footer "1-2"   ->  stored 2
    >     PDF page 36 footer "2-20"  ->  stored 20
    >     PDF page 73 footer "4-4"   ->  stored 4
    >
    > Manuals that number per chapter are common (both TxDOT manuals in the corpus
    > do it), and the damage is worse than a wrong label: chapter 2 page 20 and
    > chapter 3 page 20 both collapse to "20", so the citation is not merely
    > imprecise, it is AMBIGUOUS across the document. A reader sent to "p. 20"
    > cannot find the passage, which is the exact failure this function was written
    > to prevent.
    >
    > Now returns a STRING and preserves the chapter prefix ("4-4", "A-3", "7").
    > Safe: `printed_page` is only ever rendered, never compared numerically —
    > app.py builds `f"p. {meta['printed_page']}"` and nothing else reads it.
    > Segments are capped at 3 digits AND preceded by a non-digit, so a footer
    > ending in a revision date does not parse as a page number. This also fixes
    > a PRE-EXISTING defect the cap first made worse: page 217 of the NFA handbook
    > carries two footers, `"205"` and
    > `"ATF E-Form 5013.3 Revised February 2004"`, and last-write-wins stored the
    > YEAR as the printed page. Now the year fails to match at all, and the first
    > valid match on a page wins rather than the last.
    """
    # rank 0 = the whole footer IS a page number ("4-4"); rank 1 = a number at the
    # end of a running footer ("… Design Manual | 4-4"). A standalone number is the
    # stronger signal, so it wins even if it appears later in the document order.
    # Shapes seen in this corpus: "7" · "4-4" · "2-20" · "A-1" · "A2".
    # The hyphen after an appendix letter is NOT optional-by-accident: written as
    # [A-Za-z]?\d{1,3} the letter can only be followed by a digit, so "A-1" fell
    # through to the trailing-match branch and stored "1" — losing the appendix
    # and making every Appendix A page collide with chapter numbering.
    PAGE_NUM = r"(?:[A-Za-z]-?)?\d{1,3}(?:-\d{1,3})?"
    exact = re.compile(rf"^{PAGE_NUM}$")
    trailing = re.compile(rf"(?<!\d){PAGE_NUM}\s*$")

    best = {}
    for t in doc.get("texts", []):
        if t.get("label") != "page_footer":
            continue
        prov = t.get("prov") or []
        if not prov:
            continue
        txt = (t.get("text") or "").strip()
        if exact.match(txt):
            rank, val = 0, txt
        else:
            mm = trailing.search(txt)
            if not mm:
                continue
            rank, val = 1, mm.group(0).strip()
        page = prov[0]["page_no"]
        if page not in best or rank < best[page][0]:
            best[page] = (rank, val)
    return {p: v for p, (_, v) in best.items()}


def _docling_table_md(item):
    """DoclingDocument table -> a markdown table, so chunk_markdown can apply
    the whole-table + linearized-row treatment already validated on the vault."""
    data = item.get("data") or {}
    grid = data.get("grid")
    rows = []
    if grid:
        for row in grid:
            rows.append([repair_glyphs((c or {}).get("text", "")).strip().replace("|", "/")
                         for c in row])
    else:
        cells = data.get("table_cells") or []
        if not cells:
            return ""
        nrows = data.get("num_rows") or max((c.get("end_row_offset_idx", 0) for c in cells), default=0)
        ncols = data.get("num_cols") or max((c.get("end_col_offset_idx", 0) for c in cells), default=0)
        rows = [["" for _ in range(ncols)] for _ in range(nrows)]
        for c in cells:
            r, col = c.get("start_row_offset_idx", 0), c.get("start_col_offset_idx", 0)
            if r < nrows and col < ncols:
                rows[r][col] = repair_glyphs(c.get("text") or "").strip().replace("|", "/")
    rows = [r for r in rows if any(x for x in r)]
    if len(rows) < 2:
        return ""
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def split_oversized(text):
    """
    Split one item longer than CHUNK_CHARS into pieces of at most CHUNK_CHARS
    that overlap by about CHUNK_OVERLAP. A code item has one line per line, so
    each piece ends at the last line break before the limit and the next one
    starts at a line start, keeping every line of code whole. Text with no
    line break in reach (prose, one very long line) is cut at the character
    limit, overlapping by exactly CHUNK_OVERLAP, as before.
    """
    pieces, start = [], 0
    while True:
        end = start + CHUNK_CHARS
        if end >= len(text):
            pieces.append(text[start:])
            return pieces
        # A line break in the back half of the window ends the piece there;
        # one nearer the start would leave a stub, so cut by character.
        nl = text.rfind("\n", start + CHUNK_CHARS // 2, end + 1)
        if nl != -1:
            end = nl
        pieces.append(text[start:end])
        # Overlap: back up CHUNK_OVERLAP, then to the start of that line if it
        # is close, else forward to the next line start. With no line break
        # either way, start mid-line exactly CHUNK_OVERLAP back.
        nxt = end - CHUNK_OVERLAP
        back = text.rfind("\n", start, nxt) + 1
        fwd = text.find("\n", nxt, end + 1)
        if back > start and end - back <= 2 * CHUNK_OVERLAP:
            nxt = back
        elif fwd != -1:
            nxt = fwd + 1
        start = nxt


def overlap_tail(text):
    """The last CHUNK_OVERLAP characters of text, carried into the next chunk.
    If that would start mid-line and a line break is in reach, start after it,
    so a code line is never carried over in part."""
    tail = text[-CHUNK_OVERLAP:]
    if len(text) > CHUNK_OVERLAP and text[-CHUNK_OVERLAP - 1] != "\n":
        nl = tail.find("\n")
        if nl != -1 and tail[nl + 1:].strip():
            tail = tail[nl + 1:].lstrip("\n")
    return tail


def pack_items(items, section):
    """
    Group paragraph-level (text, page) pieces into chunks, carrying the page of
    the FIRST piece in each chunk. Docling hands us real paragraphs, which is
    exactly the granularity chunk_prose works at — so packing them directly is
    both simpler and gives an exact page per chunk, rather than inferring one
    from a character offset into a re-joined blob.
    """
    out, cur, curlen, curpage, prev = [], [], 0, None, ""

    def flush():
        nonlocal cur, curlen, curpage, prev
        if not cur:
            return
        body = "\n\n".join(cur)
        if CHUNK_OVERLAP and prev:
            body = overlap_tail(prev) + "\n\n" + body
        out.append({"text": body, "page": curpage, "section": section})
        prev = "\n\n".join(cur)
        cur, curlen, curpage = [], 0, None

    for text, page in items:
        # Keep a code item's first-line indentation; only blank lines go.
        text = (text or "").strip("\n").rstrip()
        if not text.strip():
            continue
        if len(text) > CHUNK_CHARS:            # oversized paragraph: hard-split it
            flush()
            for piece in split_oversized(text):
                out.append({"text": piece, "page": page, "section": section})
            prev = text[-CHUNK_CHARS:]
            continue
        if curlen + len(text) + 2 > CHUNK_CHARS:
            flush()
        if curpage is None:
            curpage = page
        cur.append(text)
        curlen += len(text) + 2
    flush()
    return out


# Export filenames that carry no citation value — fall back to the name of the
# PDF Docling converted (kept in `origin.filename`) rather than citing "file".
GENERIC_NAMES = {"file", "document", "doc", "output", "export", "converted",
                 "untitled", "result", "out", "tmp", "temp"}


def docling_title(path: pathlib.Path, doc: dict) -> str:
    """Citation title for a Docling export. The filename still wins — you named
    it, so you control the citation — unless it's a placeholder like
    `file.json`, in which case the original PDF's name is far more useful."""
    stem = path.stem
    if stem.lower() in GENERIC_NAMES:
        origin = ((doc.get("origin") or {}).get("filename") or "").strip()
        return pathlib.Path(origin).stem or doc.get("name") or stem
    return stem


def read_docling_json(path: pathlib.Path):
    """A Docling JSON export -> (title, records). Records carry real page numbers."""
    doc = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    if doc.get("schema_name") != "DoclingDocument":
        raise ValueError(
            f"{path.name} is not a Docling export (schema_name="
            f"{doc.get('schema_name')!r}). Convert with: "
            "docling --to json --image-export-mode placeholder <file>")

    title = docling_title(path, doc)
    pmap = printed_page_map(doc)   # PDF page index -> printed page number
    # Docling joins a code item's lines with spaces. When the raw PDF it was
    # converted from is in the raw store (hash-verified), code items are read
    # back from its text layer instead, one line per line. See code_lines.py.
    reader = code_lines.reader_for(path, doc)
    try:
        return _read_docling_items(doc, title, pmap, reader)
    finally:
        if reader is not None:
            reader.close()


def _read_docling_items(doc, title, pmap, reader):
    from_pdf = n_code = 0
    records, buf, section = [], [], ""
    outline = {}          # level -> heading text, for the breadcrumb
    for item in _walk_docling(doc.get("body") or {}, doc):
        label = (item.get("label") or "").lower()
        if label in DOCLING_SKIP_LABELS or item.get("content_layer") == "furniture":
            continue
        # Microsoft Learn "Expand table" / "HOW-TO GUIDE" button chrome.
        if is_web_chrome(item.get("text") or ""):
            continue
        page = _item_page(item)
        # Mis-mapped glyphs (ˆ for ^) in every label, damaged dunders in code
        # items. See parse_check.repair_item_text for the measured table.
        text = repair_item_text(item.get("text") or "", label)
        if label == "code":
            n_code += 1
            lines = reader.code_text(item) if reader is not None else None
            if lines is not None:
                # The text layer is the book's own text: no dunder guessing.
                text = repair_glyphs(lines)
                from_pdf += 1

        # Curated derivatives must not merge across excluded-page gaps or give
        # later retained text the citation of an earlier retained page.
        if doc.get("rag_curation") and buf and page != buf[-1][1]:
            records.extend(pack_items(buf, section))
            buf = []

        if label in DOCLING_HEADING_LABELS:
            head = clean_heading(text)
            level = _heading_level(item)
            if _is_real_heading(head, level):
                records.extend(pack_items(buf, section))   # close out the old section
                buf = []
                section = _section_path(outline, level, head)
                continue
            # Docling called it a heading; we don't. Deep or sentence-length
            # "headings" are promoted body text — index them instead of letting
            # them become a label and vanish from the corpus.
            if head:
                buf.append((text, page))
            continue

        if label == "table":
            records.extend(pack_items(buf, section))
            buf = []
            md = _docling_table_md(item)
            if md:
                for c in chunk_markdown(title, md):
                    records.append({"text": c, "page": page, "section": section})
            continue

        if label in DOCLING_TEXT_LABELS or item.get("text"):
            buf.append((text, page))

    records.extend(pack_items(buf, section))
    records = [dict(r, printed_page=pmap.get(r.get("page"))) for r in records]   # <-- add
    if reader is not None:
        print(f"  code lines: {from_pdf}/{n_code} code items from the PDF text layer"
              f" ({pathlib.Path(reader.pdf.name).name})")
    return title, [r for r in records if r["text"].strip()]


HEADING_RE = re.compile(r"^#{1,6}\s+(\S.*)$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def fence_mask(lines):
    """One bool per line: True if the line is inside, or delimits, a fenced
    code block (``` or ~~~).

    Without this a '# comment' line in a code block is read as a heading: the
    chunk is cited under the comment ("§compose.yaml") and the comment itself
    is dropped from the code. Measured on the Docker docs, 2026-11-14: 599
    chunks in 110 of 605 files. A fence closes on the same character, at least
    as long, with nothing after it; an unclosed fence runs to the end.
    """
    mask, marker = [], None
    for line in lines:
        m = FENCE_RE.match(line)
        if marker is None:
            if m:
                marker = m.group(1)
            mask.append(m is not None)
        else:
            mask.append(True)
            if (m and m.group(1)[0] == marker[0] and len(m.group(1)) >= len(marker)
                    and line.strip() == m.group(1)):
                marker = None
    return mask


def clean_heading(text: str) -> str:
    """Strip markdown emphasis/trailing hashes. pymupdf4llm emits headings as
    '# **3.2 Firewall Policy**' — the asterisks would end up in every citation."""
    t = text.strip().rstrip("#").strip()
    t = re.sub(r"[*_`]{1,3}", "", t)
    return re.sub(r"\s+", " ", t).strip(" :-—")


def split_by_heading(text: str, carried: str):
    """
    Break a block of markdown into (heading, body) segments at each '#' heading.
    `carried` is the heading still in force from the previous page, so a section
    that spans a page break keeps its name on the continuation pages.
    Returns (segments, last_heading).
    """
    segments, buf, head = [], [], carried
    lines = text.split("\n")
    for line, in_code in zip(lines, fence_mask(lines)):
        # '# comment' in a shell/Dockerfile/YAML block is code, not a heading.
        m = None if in_code else HEADING_RE.match(line.strip())
        if m:
            if buf:
                segments.append((head, "\n".join(buf)))
                buf = []
            head = clean_heading(m.group(1)) or head
        else:
            buf.append(line)
    if buf:
        segments.append((head, "\n".join(buf)))
    return segments, head


def citation_label(title: str, section: str, page, jurisdiction=None, jurisdiction_name=None):
    """The human-readable provenance line stamped onto each chunk.

    When the manifest supplies a jurisdiction it goes FIRST, in both its code and
    its spelled-out form: `[Texas (TX) · Texas Payroll Laws · §1.1 · p. 1]`.

    That is not cosmetic. The citation prefix is indexed with the chunk text, so
    every Texas chunk now literally contains the token "Texas" — which is the
    only way the BM25 leg can find a passage whose substance is an ABSENCE
    ("no state mandate", "preempted"). Measured 2026-08-02: Texas sections lost
    4:1 to Colorado on semantic similarity alone, because you cannot retrieve
    the absence of a thing by searching for the thing. Jurisdiction filtering
    (query.py --jurisdiction) is the primary fix; this is the cheap second one,
    and it also gives the model a string to copy rather than infer.
    """
    parts = []
    if jurisdiction_name and jurisdiction:
        parts.append(f"{jurisdiction_name} ({jurisdiction})")
    elif jurisdiction_name or jurisdiction:
        parts.append(str(jurisdiction_name or jurisdiction))
    parts.append(title)
    if section:
        parts.append(f"§{section}")
    if page:
        parts.append(f"p. {page}")
    return "[" + " · ".join(parts) + "]"


def chunk_pdf_with_pages(title: str, pages, section_map):
    """
    Page-aware chunking. Returns [{"text", "page", "section"}, ...].

    Section for a chunk = the nearest markdown heading above it (headings carry
    across page breaks), falling back to the PDF outline entry for that page.
    """
    out, carried = [], ""
    for page_no, ptext in pages:
        segments, carried = split_by_heading(ptext, carried)
        for seg_head, seg_text in segments:
            if not seg_text.strip():
                continue
            section = (seg_head or section_map.get(page_no, "")).strip()
            for c in chunk_markdown(title, seg_text):
                out.append({"text": c, "page": page_no, "section": section})
    return out


def chunk_plain_with_sections(title: str, body: str):
    """Same shape as chunk_pdf_with_pages, but for page-less formats
    (markdown, txt, docx): section tracked, page left None."""
    out = []
    segments, _ = split_by_heading(body, "")
    for seg_head, seg_text in segments:
        if not seg_text.strip():
            continue
        for c in chunk_markdown(title, seg_text):
            out.append({"text": c, "page": None, "section": (seg_head or "").strip()})
    return out


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
def embed_batch(texts):
    r = requests.post(f"{OLLAMA_URL}/api/embed",
                      json={"model": EMBED_MODEL, "input": texts}, timeout=180)
    r.raise_for_status()
    return r.json()["embeddings"]


def vec_to_pg(vec):
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def already_current(conn, src: str, sha: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT metadata->>'sha' FROM chunks WHERE source_path = %s LIMIT 1;", (src,))
        row = cur.fetchone()
    return bool(row) and row[0] == sha


def curation_summary(curation) -> dict | None:
    """Small stand-in for a document's rag_curation history on each chunk.

    The full history (every repair's before/after text and page geometry) can
    be tens of KB, and it was copied onto every chunk: ~571 MB across the home
    corpus on 2026-11-15, fetched with every search candidate and read by
    nothing. The approved JSON keeps the full history; chunks get counts, the
    excluded pages, and the hash of the source the latest curation started from.
    """
    if not isinstance(curation, dict) or not curation:
        return None
    repairs = [r for r in curation.get("repairs") or [] if isinstance(r, dict)]
    exclusions = [e for e in curation.get("page_exclusions") or [] if isinstance(e, dict)]
    steps = sorted(repairs + exclusions, key=lambda s: str(s.get("recorded_at") or ""))
    actions: dict[str, int] = {}
    for repair in repairs:
        for change in repair.get("changes") or []:
            if isinstance(change, dict):
                action = str(change.get("action") or "unknown")
                actions[action] = actions.get(action, 0) + 1
    pages = sorted({p for e in exclusions for p in e.get("pdf_pages") or [] if isinstance(p, int)})
    return {
        "repair_batches": len(repairs),
        "repair_actions": dict(sorted(actions.items())),
        "excluded_pdf_pages": pages,
        "latest_source_sha256": steps[-1].get("source_sha256") if steps else None,
        "full_history": "rag_curation in the approved document JSON",
    }


def write_chunks(conn, src, title, doc_type, records, doc_meta):
    """records = [{"text","page","section"}]. Embeds, replaces the file's rows."""
    texts = [r["text"] for r in records]
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH):
        vectors.extend(embed_batch(texts[i:i + EMBED_BATCH]))

    # Stamp WHICH MODEL produced each vector. Per the work-lab embedder bake-off
    # decision (2026-06-20): "store model name + dimension as metadata on every
    # vector so future-you knows what generated it." Without this, a corpus
    # half-embedded with bge-m3 and half with Qwen3 is silently broken and
    # indistinguishable — cosine distances between two models' vectors are
    # meaningless, and you cannot find the stale rows to re-embed.
    embed_stamp = {"embed_model": EMBED_MODEL,
                   "embed_dim": len(vectors[0]) if vectors else None}

    # Jurisdiction comes from the collection's manifest.yaml (may be absent —
    # the vault, NIST and ATF corpora have none, and citation_label degrades to
    # its original form).
    jur = doc_meta.get("jurisdiction")
    jur_name = doc_meta.get("jurisdiction_name")

    rows = []
    for idx, (rec, vec) in enumerate(zip(records, vectors)):
        meta = dict(doc_meta)
        meta.update(embed_stamp)
        meta["page"] = rec["page"]
        meta["printed_page"] = rec.get("printed_page")     # <-- add
        meta["section"] = rec["section"] or None
        meta["citation"] = citation_label(title, rec["section"], rec["page"],
                                          jur, jur_name)
        rows.append((src, title, doc_type, idx, rec["text"],
                     json.dumps(meta), vec_to_pg(vec)))

    with conn.cursor() as cur:
        cur.execute("DELETE FROM chunks WHERE source_path = %s;", (src,))
        cur.executemany(
            "INSERT INTO chunks "
            "(source_path, title, doc_type, chunk_index, chunk_text, metadata, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::vector);",
            rows,
        )
    conn.commit()
    return len(rows)


def ingest_vault_file(conn, path: pathlib.Path, doc_type: str, force=False):
    """The original vault/PDF_DIR path — governance applied. Returns (n, reason, status)."""
    try:
        if doc_type == "pdf":
            title, body, meta = read_pdf(path)
        else:
            title, body, meta = read_markdown(path)
    except Exception as e:
        print(f"  ! error {path.name}: {e}")
        return 0, "error", None

    # Layer 2 — per-file opt-out
    if str(meta.get("rag", "true")).lower() in ("false", "no", "0"):
        return 0, "optout", None

    # Layer 3 — status policy
    status = (meta.get("status") or "current").lower()
    if status_excluded(status):
        return 0, "status", status

    if not body:
        return 0, "empty", status

    src, sha = str(path), file_sha(path)
    if not force and already_current(conn, src, sha):
        return 0, "unchanged", status

    records = chunk_plain_with_sections(title, body)
    if not records:
        return 0, "empty", status

    doc_meta = {"file": path.name, "status": status, "type": meta.get("type", ""),
                "lab": lab_for(path, meta), "source": "vault",
                "collection": "vault", "sha": sha}
    n = write_chunks(conn, src, title, doc_type, records, doc_meta)
    return n, None, status


def ingest_drop_file(conn, path: pathlib.Path, collection: str, force=False,
                     manifest: "Manifest" = None, source_path: str | None = None,
                     content_sha256: str | None = None):
    """
    The DROP-ZONE path: no governance denylist (you chose to put it there),
    collection tag from the subfolder, page + section citations for PDFs.

    If the collection folder has a `manifest.yaml`, its entry for this file adds
    jurisdiction / authority / provenance metadata and VERIFIES the file's
    sha256. See corpus_manifest.py. A ManifestError propagates — a mislabelled
    document must stop the run, not warn.

    Returns (n, reason, collection).
    """
    ext = path.suffix.lower()
    src, sha = source_path or str(path), content_sha256 or file_sha(path)

    # Manifest first: a sha mismatch must be caught even on an unchanged file,
    # and the manifest's `collection:` overrides the folder name (a folder
    # renamed in Finder otherwise silently re-tags the whole collection).
    extra = manifest.metadata_for(path, sha) if manifest else {}
    collection = str(extra.get("collection", collection)).strip().lower()

    if not force and already_current(conn, src, sha):
        return 0, "unchanged", collection

    doc_title = ""
    try:
        if ext == ".json":
            # Docling export — best structure AND real page numbers
            title, records = read_docling_json(path)
            doc_type = "docling"
            npages = max((r["page"] for r in records if r["page"]), default=0)
        elif ext == ".pdf":
            title, pages, section_map, doc_title = read_pdf_pages(path)
            records = chunk_pdf_with_pages(title, pages, section_map)
            doc_type, npages = "pdf", (max(p for p, _ in pages) if pages else 0)
        else:
            if ext == ".docx":
                title, body, meta = read_docx(path)
            elif ext in (".md", ".markdown"):
                title, body, meta = read_markdown(path)
            else:
                title, body, meta = read_text(path)
            records = chunk_plain_with_sections(title, body)
            doc_type, npages = ext.lstrip("."), 0
    except Exception as e:
        print(f"  ! error {path.name}: {e}")
        return 0, "error", collection

    if not records:
        return 0, "empty", collection

    doc_meta = {"file": path.name, "status": "current", "type": "reference",
                "lab": os.environ.get("RAG_LAB", "home").strip().lower() or "home",
                "source": "dropzone", "collection": collection,
                "pages_total": npages, "doc_title": doc_title or None, "sha": sha}
    if ext == ".json":
        # Informational provenance only: curation physically removes excluded
        # content BEFORE review, so arbitrary incoming flags cannot bypass QA.
        # Only a summary goes on the chunks; the full history stays in the
        # approved JSON (see curation_summary).
        curated = json.loads(path.read_text(encoding="utf-8"))
        summary = curation_summary(curated.get("rag_curation"))
        if summary:
            doc_meta["rag_curation"] = summary

    # Manifest fields (jurisdiction, authority, source_url, retrieved, …) are
    # merged in, but they may not overwrite the fields that establish the
    # chunk's identity — those are computed from the bytes on disk and the
    # manifest is a claim ABOUT those bytes, not a substitute for them.
    _RESERVED = {"file", "sha", "source", "collection", "pages_total", "doc_title", "rag_curation"}
    doc_meta.update({k: v for k, v in extra.items() if k not in _RESERVED})

    # Stamp the citation onto the indexed text itself, so BM25 can match a
    # section/doc number (or a state name) and the model can quote the
    # reference verbatim rather than inventing one.
    jur = doc_meta.get("jurisdiction")
    jur_name = doc_meta.get("jurisdiction_name")
    for rec in records:
        rec["text"] = (citation_label(title, rec["section"], rec["page"], jur, jur_name)
                       + "\n" + rec["text"])
    n = write_chunks(conn, src, title, doc_type, records, doc_meta)
    return n, None, collection


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------
def gather_markdown(vault: str):
    root = pathlib.Path(vault).expanduser()
    if not root.is_dir():
        print(f"  (vault not found: {root})")
        return []
    files = []
    for p in root.rglob("*.md"):
        if any(part in VAULT_EXCLUDES for part in p.parts):
            continue
        if is_denied(p):          # Layer 1 — path denylist
            continue
        files.append(p)
    return files


def gather_pdfs(pdf_dir: str):
    root = pathlib.Path(pdf_dir).expanduser()
    if not root.is_dir():
        print(f"  (pdf dir not found: {root})")
        return []
    return [p for p in root.rglob("*.pdf") if not is_denied(p)]


def gather_drop(drop_dir: str):
    """Walk the approved document store.

    Returns ([(path, collection), ...], superseded_paths).

    First subfolder under the drop root is the collection; loose files at the
    root land in 'general'. NO denylist here — approval is the curation gate.

    SUPERSEDE RULE: if `NIST.SP.800-41r1.json` (a Docling export) sits next to
    `NIST.SP.800-41r1.pdf` or `.md`, the JSON wins and the others are skipped —
    otherwise you'd index the same document twice and its own duplicate chunks
    would compete for rank. Any chunks already written from the superseded file
    are deleted, so upgrading a document to Docling is a one-step operation.
    """
    root = pathlib.Path(drop_dir).expanduser()
    if not root.is_dir():
        print(f"  (approved document store not found: {root} — set DROP_DIR)")
        return [], []
    files = [p for p in sorted(root.rglob("*"))
             if p.is_file() and not p.name.startswith(".")
             and p.suffix.lower() in DROP_EXTS
             and p.name.lower() != "readme.md"]   # document-store instructions

    json_stems = {(p.parent, p.stem) for p in files if p.suffix.lower() == ".json"}
    out, superseded = [], []
    for p in files:
        if p.suffix.lower() != ".json" and (p.parent, p.stem) in json_stems:
            superseded.append(str(p))
            continue
        rel = p.relative_to(root)
        collection = rel.parts[0].lower() if len(rel.parts) > 1 else "general"
        out.append((p, collection))
    return out, superseded


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="TRUNCATE the table first")
    ap.add_argument("--vault-only", action="store_true")
    ap.add_argument("--pdfs-only", action="store_true")
    ap.add_argument(
        "--documents-only", "--drop-only", dest="documents_only",
        action="store_true",
        help="only the approved document store (--drop-only is a legacy alias)",
    )
    ap.add_argument("--path", help="ingest a single approved file or folder")
    ap.add_argument("--collection", help="collection tag for --path (default: inferred)")
    ap.add_argument("--source-path",
                    help="with --path to ONE file: the source_path its chunks are stored "
                         "under. Use the rag://approved/<collection>/<file> identity for a "
                         "document the reviewer ingested, so a Mac-side re-ingest replaces "
                         "its rows instead of adding a /Volumes/... duplicate")
    ap.add_argument("--force", action="store_true", help="re-embed even if unchanged")
    ap.add_argument("--require-manifest", action="store_true",
                    help="fail if an approved document has no manifest.yaml entry "
                         "(recommended for the payroll corpus — an unlisted file "
                         "gets no jurisdiction, so a scoped query can never find it)")
    args = ap.parse_args()

    if args.source_path and not (args.path and pathlib.Path(args.path).expanduser().is_file()):
        ap.error("--source-path needs --path to a single file")

    restricted = args.vault_only or args.pdfs_only or args.documents_only or args.path

    with psycopg.connect(**DB) as conn:
        if args.reset:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE chunks RESTART IDENTITY;")
            conn.commit()
            print("Table reset.\n")

        # ---------- build the job list ----------
        vault_jobs, drop_jobs, superseded = [], [], []
        if args.path:
            target = pathlib.Path(args.path).expanduser()
            if target.is_dir():
                # Same rules as the document store; --collection overrides the inferred tag,
                # and a folder with no subfolders is named after itself
                found, superseded = gather_drop(str(target))
                drop_jobs = [(p, (args.collection or
                                  (c if c != "general" else target.name)).lower())
                             for p, c in found]
            elif target.is_file():
                drop_jobs = [(target, (args.collection or target.parent.name).lower())]
            else:
                print(f"--path not found: {target}")
                return
        else:
            if args.vault_only or not restricted:
                vault_jobs += [(p, "obsidian") for p in gather_markdown(VAULT)]
            if args.pdfs_only or not restricted:
                vault_jobs += [(p, "pdf") for p in gather_pdfs(PDF_DIR)]
            if args.documents_only or not restricted:
                drop_jobs, superseded = gather_drop(DROP_DIR)

        # A Docling JSON replaces the same document's .pdf/.md — drop its old rows
        # so the two versions can't compete for rank.
        if superseded:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chunks WHERE source_path = ANY(%s);", (superseded,))
                removed = cur.rowcount
            conn.commit()
            for s in superseded:
                print(f"  ~ {os.path.basename(s)} superseded by its Docling .json"
                      + (" (old chunks removed)" if removed else ""))

        if not vault_jobs and not drop_jobs:
            print("Nothing to ingest. Check OBSIDIAN_VAULT / PDF_DIR / DROP_DIR / denylist.")
            return

        if vault_jobs:
            print(f"Governance: excluding statuses containing {sorted(EXCLUDE_STATUS_KEYWORDS)} · "
                  f"{len(RAG_DENYLIST)} path-denylist patterns")
        print(f"Queued: {len(vault_jobs)} vault/pdf file(s), "
              f"{len(drop_jobs)} approved document file(s)\n")

        total_chunks = 0
        skips = Counter()
        by_collection = Counter()

        for path, doc_type in vault_jobs:
            n, reason, status = ingest_vault_file(conn, path, doc_type, force=args.force)
            total_chunks += n
            if reason:
                skips[reason] += 1
            else:
                print(f"  + {path.name}  ({status}, {n} chunks)")

        # Collection manifests: jurisdiction + provenance metadata, and sha
        # verification of every listed document. See corpus_manifest.py.
        manifest = Manifest(require=args.require_manifest)

        if drop_jobs:
            print("\nApproved document store:")
        for path, collection in drop_jobs:
            try:
                n, reason, coll = ingest_drop_file(conn, path, collection,
                                                   force=args.force, manifest=manifest,
                                                   source_path=args.source_path)
            except ManifestError as e:
                # STOP GATE. A document that is not what the manifest says it is
                # poisons every metric computed downstream and presents as a
                # model failure — see the 2026-08-01 baseline. Refuse the run
                # rather than ingest a corpus you cannot describe.
                print(f"\n  ✗ MANIFEST ERROR — {e}")
                print("\nAborted before writing. Nothing in this run was ingested "
                      "after this point; fix the manifest or the file and re-run.")
                return
            total_chunks += n
            if reason:
                skips[reason] += 1
                if reason == "unchanged":
                    print(f"  = {path.name}  [{coll}] unchanged, skipped")
            else:
                by_collection[coll] += n
                print(f"  + {path.name}  [{coll}] {n} chunks")

        # Files ingested with no manifest entry got no jurisdiction, which makes
        # them invisible to any scoped query — silent, and indistinguishable
        # from a retrieval failure. Say so loudly.
        for w in manifest.warnings:
            print(f"  ! unlisted: {w}")

        # Summary — what actually made it in.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(DISTINCT source_path) FROM chunks;")
            grand, ndocs = cur.fetchone()
            cur.execute("SELECT coalesce(metadata->>'collection','vault'), count(*) "
                        "FROM chunks GROUP BY 1 ORDER BY 2 DESC;")
            collections = cur.fetchall()
            # Preflight §2 in one line: if a state you just ingested isn't here,
            # `query.py --jurisdiction XX` will return nothing and it will look
            # like a retrieval problem.
            cur.execute("SELECT metadata->>'jurisdiction', count(*) FROM chunks "
                        "WHERE metadata ? 'jurisdiction' GROUP BY 1 ORDER BY 1;")
            jurisdictions = cur.fetchall()

    done = len(vault_jobs) + len(drop_jobs) - sum(skips.values())
    print(f"\nIngested {total_chunks} chunks from {done} file(s).")
    if skips:
        print("Skipped: " + ", ".join(f"{k}={v}" for k, v in skips.items())
              + "  (unchanged=hash match · optout=rag:false · status=excluded status)")
    print(f"Table now holds {grand} chunks across {ndocs} docs.")
    print("By collection: " + ", ".join(f"{c}={n}" for c, n in collections))
    if jurisdictions:
        print("By jurisdiction: " + ", ".join(f"{j}={n}" for j, n in jurisdictions))


if __name__ == "__main__":
    main()
