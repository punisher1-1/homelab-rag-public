#!/usr/bin/env python3
"""
figure_index.py — build a figure/table index from a Docling JSON export.

WHY THIS EXISTS
---------------
`ingest.py`'s `read_docling_json()` skips `picture` items (DOCLING_SKIP_LABELS).
Docling attaches most captions as CHILDREN of the picture object, so skipping the
picture skips its caption too. Measured on `bsd.json` (Bridge & Structures Design
Manual, 309 pp): 73 caption items exist, only 7 are direct children of `body`.
The other 66 are unreachable from a body-only walk. For a figure-heavy manual the
caption is a large share of the retrievable text about that figure, and the figure
ID ("Figure 4.6.2-3") is how the document's own prose addresses it.

This script does not modify the corpus. It produces a SIDECAR index that
`ingest.py` can consume to enrich chunks, and a report that tells you whether the
document's cross-references actually resolve.

THREE THINGS IT ANSWERS
-----------------------
1. What figures exist?          -> id, caption, page, section, image path, bbox
2. Does the prose resolve?      -> "as shown in Figure 4.6-3" — is there a Figure 4.6-3?
3. What is unreachable?         -> uncaptioned pictures, orphan figures, external refs

THE THREE REFERENCE OUTCOMES (this is the useful part)
------------------------------------------------------
  resolved   the prose ID matches a caption ID in this document.
  external   the reference is to ANOTHER document. TxDOT marks these with angle
             brackets: "Supplement <Table 3.4.1-1> with the following". The table
             lives in the AASHTO LRFD spec, not here. A chunk like this retrieves
             well and answers nothing, which is worse than a miss because it looks
             complete. Flag it; do not try to resolve it.
  unresolved the prose and the caption disagree inside ONE document. Measured on
             bsd.json: prose says "Figure 4.6-3", the caption says
             "Figure 4.6.2-3". Same figure, two numbers. Nothing downstream
             reconciles that, and neither will BM25.

USAGE
-----
  python3 figure_index.py <docling.json> [--report] [--out <sidecar.json>]
  python3 figure_index.py dropzone/lad/lad.json --report

Stdlib only, no DB, no venv needed. Safe to run on anything.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from collections import Counter

# Soft-load .env so DROP_DIR matches ingest.py when a venv is active.
# Stdlib-only runs still work; they just see the process environment / default.
try:
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


def _drop_dir() -> pathlib.Path:
    """Same default as ingest.py — the configured approved-document store."""
    return pathlib.Path(
        os.environ.get("DROP_DIR", "./dropzone").strip() or "./dropzone"
    ).expanduser()


def _is_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False

# A "heading" longer than this is a sentence in any corpus. Same default and same
# reasoning as DOCLING_MAX_HEADING_CHARS in ingest.py; kept local so this script
# has no import dependency on the pipeline.
MAX_HEADING_CHARS = 80

# Labels Docling uses for the two things that can carry a numbered caption.
FIGURE_LABELS = {"picture", "chart"}
TABLE_LABELS = {"table"}
HEADING_LABELS = {"title", "section_header"}

# ---------------------------------------------------------------------------
# ID handling
# ---------------------------------------------------------------------------

# The ID grammar, which is messier than it looks. Real IDs from two TxDOT
# manuals: 4-14 · 4.6.2-3 · B.2-1 · A.1.1-1 · D3.2.4.3-1 · 6A.4.2.2-1 · 1.1
# So: an optional appendix letter (with or without a following dot), digits that
# may themselves be followed by a letter, dotted groups, optional -N suffix.
# Written too narrowly this silently under-counts: a first draft requiring
# [A-Z]?\d matched 21 of the 66 captions in bsd.json and read "Table 6A.4.2.2-1"
# as "Table 6".
_ID = r"[A-Za-z]{0,2}\.?\d+[A-Za-z]?(?:\.\d+[A-Za-z]?)*(?:-\d+[A-Za-z]?)?"

# Matches: Figure 4-14 · Figures 4-1 · Fig. 2-1 · Table B.2-1 · Exhibit 3
# The \s+ (not " ") is load-bearing: the raw PDF text layer contains
# "Figure \n4-14" with a hard line break between the word and the number.
REF_RE = re.compile(
    rf"\b(Figures?|Figs?\.?|Tables?|Exhibits?|Charts?)\s+({_ID})",
    re.IGNORECASE,
)

# A caption declares an ID: "Figure 4.6.2-3: Phasing for Slab Beams"
CAPTION_RE = re.compile(
    rf"^\s*(Figures?|Figs?\.?|Tables?|Exhibits?|Charts?)\s+({_ID})\s*[:.–—-]?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)

# External-reference convention: <Table 3.4.1-1>, <Article 3.6.1.3.1>.
# Anything inside angle brackets points OUT of this document.
EXTERNAL_RE = re.compile(r"<\s*(Figure|Fig\.?|Table|Article|Section|Exhibit)\s+([^>]+?)\s*>",
                         re.IGNORECASE)

KIND_OF = {"figure": "figure", "figures": "figure", "fig": "figure", "figs": "figure",
           "chart": "figure", "charts": "figure", "exhibit": "figure", "exhibits": "figure",
           "table": "table", "tables": "table"}


def norm_id(raw: str) -> str:
    """Canonical form of a figure/table ID.

    Collapses whitespace (including the line break the PDF text layer inserts),
    uppercases any appendix letter, and drops a trailing period so that
    'Table 2-2.' at the end of a sentence matches 'Table 2-2'.
    """
    t = re.sub(r"\s+", "", raw or "").upper().rstrip(".")
    return t


def kind_of(word: str) -> str:
    return KIND_OF.get((word or "").lower().rstrip("."), "figure")


# ---------------------------------------------------------------------------
# Docling document walking
# ---------------------------------------------------------------------------

def deref(doc: dict, ref: dict | None):
    """Resolve a {'$ref': '#/texts/12'} or {'cref': ...} pointer.

    Both spellings exist in the wild: docling-core >= 1.10 emits 'cref', older
    exports emit '$ref'. The owner's exports have used both.
    """
    if not isinstance(ref, dict):
        return None
    path = ref.get("$ref") or ref.get("cref")
    if not path:
        return None
    cur = doc
    for part in path.lstrip("#/").split("/"):
        try:
            cur = cur[int(part)] if part.isdigit() else cur[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return cur


def walk_body(doc: dict):
    """Yield every item under body in READING ORDER, depth first.

    Unlike ingest.py's walker this does NOT skip pictures — finding them is the
    whole point. Groups are traversed transparently.
    """
    def rec(node):
        for child in (node.get("children") or []):
            item = deref(doc, child)
            if item is None:
                continue
            yield item
            if item.get("children"):
                yield from rec(item)
    yield from rec(doc.get("body") or {})


def item_page(item: dict):
    prov = item.get("prov") or []
    return prov[0].get("page_no") if prov else None


def item_bbox(item: dict):
    prov = item.get("prov") or []
    if not prov:
        return None
    b = prov[0].get("bbox") or {}
    if not b:
        return None
    return [b.get("l"), b.get("t"), b.get("r"), b.get("b")]


def image_px(item: dict):
    """Pixel area of the exported image, or None in placeholder mode."""
    sz = ((item.get("image") or {}) or {}).get("size") or {}
    w, h = sz.get("width"), sz.get("height")
    return int(w * h) if w and h else None


def printed_page_map(doc: dict) -> dict:
    """PDF page index -> printed page number, read from page_footer/page_header
    items that are just a bare number.

    Manuals routinely number pages per chapter ('1-1', '4-7') or restart after
    front matter, so Docling's page_no is NOT what a reader sees printed on the
    page. ingest.py already builds this; duplicated here (cheaply) so the sidecar
    can carry both and citations can quote the printed number.
    """
    out = {}
    for item in walk_body(doc):
        if (item.get("label") or "") not in ("page_footer", "page_header"):
            continue
        txt = (item.get("text") or "").strip()
        # Two shapes, both present in bsd.json on adjacent pages: the number
        # alone ("56"), and the running title with the number glued on
        # ("Bridge and Structures Design Manual | 57"). Docling splits the
        # footer into one or two items depending on the PDF's text runs, so
        # handling only the bare-number case leaves half the map empty.
        # Kept in step with ingest.py's printed_page_map(). They diverged once,
        # on 2026-08-18: this copy read "A-1" as "1", because the hyphen in the
        # appendix label was consumed by the separator class in the trailing
        # pattern. corpus_profile.py is what surfaced it — the profile declared
        # chapter-relative pages and two values did not match. Duplicated logic
        # drifts; the check is what makes the drift visible.
        _PAGE = r"(?:[A-Za-z]-?)?\d{1,3}(?:-\d{1,3})?"
        m = (re.fullmatch(_PAGE, txt)
             or re.search(rf"[|·—]\s*({_PAGE})\s*$", txt)
             or re.search(rf"(?<!\d)({_PAGE})\s*$", txt))
        if m:
            pg = item_page(item)
            if pg is not None and pg not in out:
                out[pg] = (m.group(1) if m.re.groups else m.group(0)).strip()
    return out


def clean_heading(text: str) -> str:
    t = (text or "").strip().rstrip("#").strip()
    t = re.sub(r"[*_`]{1,3}", "", t)
    return re.sub(r"\s+", " ", t).strip(" :-")


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------

def caption_texts(doc: dict, item: dict) -> list[str]:
    """Captions attached as children of a picture/table object.

    This is the path ingest.py cannot see: the caption's parent is the picture,
    so a body-only walk that skips pictures never reaches it.
    """
    out = []
    for c in (item.get("captions") or []):
        t = deref(doc, c)
        if t and (t.get("text") or "").strip():
            out.append(t["text"].strip())
    return out


def build_index(doc: dict) -> dict:
    """Walk the document once and collect every figure and table.

    Handles BOTH caption attachment styles found in the same document:
      (a) caption is a child of the picture           -> 66 of 73 in bsd.json
      (b) caption is a loose item in body.children    ->  7 of 73 in bsd.json

    For (b), binding is GEOMETRIC, not by reading order. First draft bound a
    loose caption to the nearest PRECEDING picture and that is wrong here: in
    lad.json the caption is emitted before its picture, and Docling's reading
    order around figures is unreliable anyway (p74 emits caption 4-6, a page
    footer, two pictures, then caption 4-7). Every item carries a bbox, so
    "which figure is this caption physically touching" is answerable directly:
    same page, horizontal overlap, smallest vertical gap, either direction.
    """
    pmap = printed_page_map(doc)
    figures, loose_caps, section = [], [], ""
    seq = 0

    for item in walk_body(doc):
        label = (item.get("label") or "").lower()

        if label in HEADING_LABELS:
            head = clean_heading(item.get("text"))
            if head and len(head) <= MAX_HEADING_CHARS:
                section = head
            continue

        if label in FIGURE_LABELS or label in TABLE_LABELS:
            seq += 1
            caps = caption_texts(doc, item)
            page = item_page(item)
            rec = {
                "seq": seq,
                "kind": "table" if label in TABLE_LABELS else "figure",
                "id": None,
                "label": None,
                "caption": None,
                "page": page,
                "printed_page": pmap.get(page),
                "section": section,
                "self_ref": item.get("self_ref"),
                "bbox": item_bbox(item),
                "image_uri": ((item.get("image") or {}) or {}).get("uri"),
                "image_dpi": ((item.get("image") or {}) or {}).get("dpi"),
                "image_px": image_px(item),
                "annotations": len(item.get("annotations") or []),
                "ref_count": 0,
                "caption_source": None,
            }
            if item.get("data"):
                rec["table_shape"] = [item["data"].get("num_rows"),
                                      item["data"].get("num_cols")]
            if caps:
                apply_caption(rec, caps[0], "child")
            figures.append(rec)
            continue

        if label == "caption":
            txt = (item.get("text") or "").strip()
            if txt:
                loose_caps.append({"text": txt, "page": item_page(item),
                                   "bbox": item_bbox(item)})
            continue

    # ORPHAN SWEEP. Some items exist in the top-level pictures[]/tables[] arrays
    # and claim "parent": "#/body", yet are absent from body.children — so a
    # reading-order walk never reaches them. Measured on lad.json: tables 0, 1
    # and 2 (the table of contents, pages 4 to 6) are orphaned this way.
    #
    # This cuts both ways and it is worth being explicit. ingest.py walks body
    # too, so those items are already excluded from the corpus. Here that is
    # lucky (ToC tables are noise), but "lucky" is not "handled" — a real figure
    # orphaned the same way would vanish with no trace. Count them, flag them,
    # and let the reader decide.
    seen = {f["self_ref"] for f in figures}
    for coll, kind in (("pictures", "figure"), ("tables", "table")):
        for item in (doc.get(coll) or []):
            if item.get("self_ref") in seen:
                continue
            seq += 1
            page = item_page(item)
            rec = {
                "seq": seq, "kind": kind, "id": None, "label": None, "caption": None,
                "page": page, "printed_page": pmap.get(page), "section": "",
                "self_ref": item.get("self_ref"), "bbox": item_bbox(item),
                "image_uri": ((item.get("image") or {}) or {}).get("uri"),
                "image_dpi": ((item.get("image") or {}) or {}).get("dpi"),
                "image_px": image_px(item),
                "annotations": len(item.get("annotations") or []),
                "ref_count": 0, "caption_source": None,
                "orphan_from_body": True,
            }
            caps = caption_texts(doc, item)
            if caps:
                apply_caption(rec, caps[0], "child")
            figures.append(rec)

    bind_loose_captions(figures, loose_caps)
    return {"figures": figures}


# Max vertical gap, in PDF points, between a caption and the figure it belongs
# to. 60 pt is about four lines of body text: enough for a caption sitting
# under a figure with normal spacing, tight enough that it will not jump across
# a paragraph to a different figure.
CAPTION_MAX_GAP = 60.0


def bind_loose_captions(figures: list[dict], caps: list[dict]) -> None:
    """Attach loose caption items to uncaptioned figures by page geometry.

    bbox is (l, t, r, b) with coord_origin BOTTOMLEFT, so t > b and the vertical
    gap between a caption below a figure is fig['b'] - cap['t'].

    Greedy nearest-first over all candidate pairs, so on a page with several
    figures each caption goes to the one it is physically closest to rather than
    to whichever happened to be emitted first.
    """
    pairs = []
    for ci, cap in enumerate(caps):
        if cap["bbox"] is None or cap["page"] is None:
            continue
        cl, ct, cr, cb = cap["bbox"]
        for fi, fig in enumerate(figures):
            if fig["caption"] or fig["bbox"] is None or fig["page"] != cap["page"]:
                continue
            fl, ft, fr, fb = fig["bbox"]
            if min(cr, fr) - max(cl, fl) <= 0:      # no horizontal overlap
                continue
            gap = min(abs(fb - ct), abs(cb - ft))   # caption below | above
            if gap <= CAPTION_MAX_GAP:
                pairs.append((gap, ci, fi))

    pairs.sort()
    used_caps, used_figs = set(), set()
    for gap, ci, fi in pairs:
        if ci in used_caps or fi in used_figs or figures[fi]["caption"]:
            continue
        apply_caption(figures[fi], caps[ci]["text"], "geometric")
        figures[fi]["caption_gap"] = round(gap, 1)
        used_caps.add(ci)
        used_figs.add(fi)


def apply_caption(rec: dict, text: str, source: str) -> None:
    rec["caption"] = text
    rec["caption_source"] = source
    m = CAPTION_RE.match(text)
    if m:
        rec["label"] = f"{m.group(1).rstrip('.').title()} {m.group(2)}"
        rec["id"] = norm_id(m.group(2))
        rec["kind"] = kind_of(m.group(1))
        rec["title"] = (m.group(3) or "").strip()


# ---------------------------------------------------------------------------
# Cross-reference resolution
# ---------------------------------------------------------------------------

BODY_LABELS = {"text", "paragraph", "list_item", "footnote", "formula", "code"}


def collect_references(doc: dict, index: dict) -> list[dict]:
    """Find every 'see Figure X' in the prose and classify it.

    A reference is external if it appears inside angle brackets anywhere in the
    same text item — that is the document's own marker for 'this lives in the
    AASHTO spec, not here'.
    """
    by_id = {f["id"]: f for f in index["figures"] if f["id"]}
    refs = []
    for item in walk_body(doc):
        label = (item.get("label") or "").lower()
        if label not in BODY_LABELS:
            continue
        text = item.get("text") or ""
        if not text:
            continue
        ext_ids = {norm_id(m.group(2).split()[0]) for m in EXTERNAL_RE.finditer(text)}
        for m in REF_RE.finditer(text):
            rid = norm_id(m.group(2))
            if rid in ext_ids:
                status = "external"
            elif rid in by_id:
                status = "resolved"
                by_id[rid]["ref_count"] += 1
            else:
                status = "unresolved"
            start = max(0, m.start() - 60)
            refs.append({
                "id": rid,
                "kind": kind_of(m.group(1)),
                "status": status,
                "page": item_page(item),
                "context": re.sub(r"\s+", " ", text[start:m.end() + 60]).strip(),
            })
    return refs


# Below this, a "picture" is furniture: a bullet glyph, a logo, a back-to-top
# arrow. Measured on lad.json, where the two populations do not overlap at all:
#   captioned figures   median 226,968 px  (342 KB on disk)
#   decorative marks    median     684 px  (1.3 KB on disk, 27x17 to 39x18)
# 5,000 px sits in the empty space between them. Without this filter the report
# claims 34 figures need a VLM pass when the real number is 5, which would have
# turned a 5-image job into an 87-image one.
DECORATIVE_MAX_PX = 5000


def is_decorative(fig: dict) -> bool:
    px = fig.get("image_px")
    if px is not None:
        return px < DECORATIVE_MAX_PX
    bbox = fig.get("bbox")          # placeholder mode: no pixels, use the layout box
    if not bbox or None in bbox:
        return False
    l, t, r, b = bbox
    return abs(r - l) * abs(t - b) < 900   # 30x30 pt


def caption_line(fig: dict) -> str:
    """The enrichment string ingest.py should stamp into a chunk.

    Same argument as the citation prefix already used in the pipeline: put the
    literal text in front of the model and give the BM25 leg something to match,
    rather than hoping the embedding carries it.
    """
    bits = [fig.get("label") or f"{fig['kind'].title()} (unnumbered)"]
    if fig.get("title"):
        bits.append(fig["title"])
    elif fig.get("caption") and not fig.get("label"):
        bits.append(fig["caption"])
    loc = fig.get("printed_page") or fig.get("page")
    tail = f" (p. {loc})" if loc else ""
    return " — ".join(bits) + tail


def build_sidecar(path: pathlib.Path, doc: dict) -> dict:
    index = build_index(doc)
    refs = collect_references(doc, index)
    figs = index["figures"]

    status = Counter(r["status"] for r in refs)
    numbered = [f for f in figs if f["id"]]
    orphans = [f for f in numbered if f["ref_count"] == 0]
    uncaptioned = [f for f in figs if not f["caption"] and not is_decorative(f)]
    decorative = [f for f in figs if not f["caption"] and is_decorative(f)]

    # by_page lets ingest.py enrich a chunk without re-walking the document
    by_page = {}
    for f in figs:
        if f["page"] is None:
            continue
        by_page.setdefault(str(f["page"]), []).append(caption_line(f))

    return {
        "document": doc.get("name"),
        "origin": (doc.get("origin") or {}).get("filename"),
        "source_json": path.name,
        "pages": len(doc.get("pages") or {}),
        "figures": figs,
        "references": refs,
        "by_page": by_page,
        "stats": {
            "figures_total": len(figs),
            "figures_numbered": len(numbered),
            "figures_uncaptioned": len(uncaptioned),
            "figures_decorative": len(decorative),
            "captions_from_child": sum(1 for f in figs if f["caption_source"] == "child"),
            "captions_from_geometry": sum(1 for f in figs if f["caption_source"] == "geometric"),
            "with_image_uri": sum(1 for f in figs if f["image_uri"]),
            "with_vlm_annotation": sum(1 for f in figs if f["annotations"]),
            "refs_total": len(refs),
            "refs_resolved": status["resolved"],
            "refs_external": status["external"],
            "refs_unresolved": status["unresolved"],
            "orphan_figures": len(orphans),
            "orphaned_from_body": sum(1 for f in figs if f.get("orphan_from_body")),
        },
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(side: dict) -> None:
    s = side["stats"]
    print(f"\n{'=' * 72}")
    print(f"  {side['source_json']}  (origin: {side['origin']}, {side['pages']} pages)")
    print(f"{'=' * 72}")

    print(f"\nFIGURES & TABLES: {s['figures_total']} found, "
          f"{s['figures_numbered']} carry an ID")
    print(f"  captioned                             : "
          f"{s['figures_total'] - s['figures_uncaptioned'] - s['figures_decorative']:>4}")
    print(f"  decorative (icons, rules, logos)      : {s['figures_decorative']:>4}"
          f"   <-- filter, do not enrich")
    print(f"  REAL figures with no caption          : {s['figures_uncaptioned']:>4}"
          f"   <-- the actual gap")
    if s["orphaned_from_body"]:
        print(f"  {s['orphaned_from_body']} are NOT in body.children at all "
              f"-> already invisible to ingest.py's walk")
    print(f"  captions reached via picture children : {s['captions_from_child']:>4}"
          f"   <-- INVISIBLE to a body-only walk")
    print(f"  captions bound by page geometry       : {s['captions_from_geometry']:>4}")
    print(f"  with extracted image file (referenced): {s['with_image_uri']:>4}")
    print(f"  with VLM annotation                   : {s['with_vlm_annotation']:>4}")

    print(f"\nCROSS-REFERENCES: {s['refs_total']} mentions in prose")
    print(f"  resolved   {s['refs_resolved']:>4}   prose ID matches a caption in this document")
    print(f"  external   {s['refs_external']:>4}   <angle bracketed> — points at another document")
    print(f"  unresolved {s['refs_unresolved']:>4}   prose and captions disagree INSIDE this document")

    unres = [r for r in side["references"] if r["status"] == "unresolved"]
    if unres:
        print("\n  unresolved sample:")
        for r in unres[:6]:
            print(f"    p{r['page']:<4} {r['kind']} {r['id']:<14} …{r['context'][-88:]}")

    ext = [r for r in side["references"] if r["status"] == "external"]
    if ext:
        print("\n  external sample (these chunks answer nothing on their own):")
        for r in ext[:4]:
            print(f"    p{r['page']:<4} {r['id']:<14} …{r['context'][-88:]}")

    orph = [f for f in side["figures"] if f["id"] and f["ref_count"] == 0]
    if orph:
        print(f"\n  {len(orph)} figures are never referenced by the prose "
              f"(caption is their ONLY retrievable text):")
        for f in orph[:5]:
            print(f"    p{f['page']:<4} {caption_line(f)[:88]}")

    nocap = [f for f in side["figures"] if not f["caption"] and not is_decorative(f)]
    if nocap:
        print(f"\n  {len(nocap)} REAL figures have no caption "
              f"(nothing retrievable — VLM description or OCR is the only route):")
        for f in nocap:
            px = f.get("image_px")
            print(f"    p{f['page']:<4} {str(px) + ' px':>12}  section={f['section'][:44]!r}")
            print(f"          {(f['image_uri'] or '(no image exported)').split('/')[-1]}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json_path", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path,
                    help="sidecar path (default: ./figures/<stem>.figures.json)")
    ap.add_argument("--report", action="store_true", help="print a human-readable report")
    ap.add_argument("--no-write", action="store_true", help="report only, write nothing")
    args = ap.parse_args(argv)

    doc = json.loads(args.json_path.read_text(encoding="utf-8", errors="ignore"))
    if doc.get("schema_name") != "DoclingDocument":
        print(f"{args.json_path.name} is not a Docling export "
              f"(schema_name={doc.get('schema_name')!r})", file=sys.stderr)
        return 2

    side = build_sidecar(args.json_path, doc)
    if args.report:
        report(side)
    if not args.no_write:
        # NOT next to the input. ingest.py's approved-document walk is
        #   rglob("*") filtered by DROP_EXTS, which includes ".json"
        # so a sidecar written beside the source would be picked up as if it
        # were a Docling export and fail the schema check mid-ingest. Default
        # to ./figures/ in the working directory instead.
        # DROP_DIR is user-configurable (often a NAS path, not ./dropzone), so
        # warn against that root — not a hard-coded "dropzone" path segment.
        out = args.out or (pathlib.Path("figures") /
                           f"{args.json_path.stem}.figures.json")
        drop = _drop_dir()
        if _is_under(out, drop):
            print(f"WARNING: {out} is inside DROP_DIR ({drop}). "
                  f"ingest.py will try to read it as a document. "
                  f"Move it or pass --out.", file=sys.stderr)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(side, indent=2), encoding="utf-8")
        print(f"wrote {out}  ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
