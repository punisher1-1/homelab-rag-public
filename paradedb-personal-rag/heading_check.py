#!/usr/bin/env python3
"""
heading_check.py — decide whether ingest.py's heading filters help a document,
BEFORE ingesting it.

    python heading_check.py dropzone/Payroll/*.json
    python heading_check.py /path/to/one.json --verbose

WHAT IT ANSWERS
---------------
Docling's 2026-08-07 release began inferring section-header levels (1-6) from
PDF bookmarks, numbering and font style. On a clean, well-bookmarked document
that is an improvement. On a scanned document full of forms it promotes body
text to headings — measured on atf-p-5320-8.pdf: section_header 371 -> 593,
~220 of them former list items, costing 3.1% of indexed body text and pushing
sub-200-char fragments from 12.0% to 15.6%.

Which of those two you get is a property of THE DOCUMENT, not of Docling. So it
has to be checked per corpus rather than decided once. This script runs
ingest.py's real chunker twice over the same parse — filters off, then on — and
prints the difference.

WHAT IT DOES NOT DO
-------------------
No database. No embedder. No GPU. It never writes anything. Run it on a parsed
JSON before that JSON goes anywhere near `ingest.py --documents-only`.

WHY IT IMPORTS ingest.py INSTEAD OF REIMPLEMENTING
--------------------------------------------------
So it cannot drift from what actually runs. If read_docling_json() changes, this
report changes with it. A QA tool that reimplements the thing it checks is
measuring a second, untested implementation.

READING THE OUTPUT
------------------
  heading levels        all `1` -> the parse predates the release, or Docling
                        found no structure to infer from. The filters will do
                        almost nothing; that is fine and expected.
                        A spread across 1-6 -> inference fired.

  body chars    ON > OFF means the filters RESTORED text that would otherwise
                have become a label. That text is recoverable no other way.

  frag < 200    the fragmentation tax. Chunks this small carry too little
                context to embed usefully.

  labels > 80ch sentence-length "headings". These are stamped into chunk_text
                by citation_label(), so each one is BM25 dilution on every chunk
                in its section.

The filters can only ever move text OUT of labels and back INTO the body. They
never delete content and never add a chunk boundary, so a "no change" verdict
means shipping them is harmless, not that they are wrong.
"""
import argparse
import pathlib
import re
import statistics
import sys
from collections import Counter

import ingest as I

# The wiki's ingest-QA table (rag-eval-harness). Included because the same
# release that changed headings also changed text extraction, and you want both
# numbers in front of you at the same time — attributing a text-quality win to a
# heading feature is exactly the confound this project keeps re-learning.
QA_CHECKS = {
    "space collapse (word >=19ch)": re.compile(r"\b\w{19,}\b"),
    "digit-letter fusion":          re.compile(r"\d[a-z]{3,}|[a-z]\d{2}"),
    "digit/letter confusion":       re.compile(r"\d\.[OolI]"),
    "mangled section symbol":       re.compile(r"\b[s$]\s\d{3}"),
}

# Reproduces the pre-patch chunker exactly: caps so high that _is_real_heading()
# is always true for a non-empty heading.
FILTERS_OFF = {"DOCLING_MAX_HEADING_LEVEL": 99, "DOCLING_MAX_HEADING_CHARS": 10 ** 9}


def parse_with(path: pathlib.Path, overrides: dict):
    """Run ingest.py's own reader with module constants temporarily overridden."""
    saved = {k: getattr(I, k) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(I, k, v)
        return I.read_docling_json(path)
    finally:
        for k, v in saved.items():
            setattr(I, k, v)


def profile(records):
    lens = [len(r["text"]) for r in records] or [0]
    labels = [r["section"] or "" for r in records]
    return {
        "chunks": len(records),
        "chars": sum(lens),
        "mean": statistics.mean(lens),
        "frag": 100.0 * sum(1 for l in lens if l < 200) / max(len(lens), 1),
        "labels": len(set(labels)),
        "long_labels": sum(1 for s in set(labels) if len(s) > 80),
    }


def heading_levels(path: pathlib.Path):
    """Level histogram straight from the Docling export, before any filtering."""
    import json
    doc = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    return Counter(
        (t.get("level") or 1)
        for t in (doc.get("texts") or [])
        if (t.get("label") or "").lower() in I.DOCLING_HEADING_LABELS
    )


def report(path: pathlib.Path, verbose=False):
    print(f"\n{'=' * 78}\n{path.name}\n{'=' * 78}")

    lv = heading_levels(path)
    total = sum(lv.values())
    spread = sorted(lv.items())
    print(f"  headings in parse : {total}")
    print(f"  level histogram   : {dict(spread)}"
          + ("   <- all level 1: no inference, filters will be near no-ops"
             if set(lv) <= {1} else "   <- levels inferred"))

    _, off = parse_with(path, FILTERS_OFF)
    _, on = parse_with(path, {})          # current ingest.py defaults
    a, b = profile(off), profile(on)

    print(f"\n  {'':16}{'filters OFF':>14}{'filters ON':>14}{'delta':>14}")
    rows = [("chunks", "chunks", "{:,.0f}"), ("body chars", "chars", "{:,.0f}"),
            ("mean chunk ch", "mean", "{:,.0f}"), ("frag < 200", "frag", "{:.1f}%"),
            ("section labels", "labels", "{:,.0f}"), ("labels > 80ch", "long_labels", "{:,.0f}")]
    for name, key, fmt in rows:
        d = b[key] - a[key]
        print(f"  {name:16}{fmt.format(a[key]):>14}{fmt.format(b[key]):>14}"
              f"{('+' if d > 0 else '') + fmt.format(d):>14}")

    text = " ".join(r["text"] for r in on)
    print(f"\n  ingest-QA (on the filtered text, {len(text):,} chars):")
    for name, rx in QA_CHECKS.items():
        hits = rx.findall(text)
        print(f"    {name:30}{len(hits):>6}"
              + (f"   e.g. {hits[0][:40]!r}" if hits and verbose else ""))

    gained, defrag = b["chars"] - a["chars"], a["frag"] - b["frag"]
    print()
    if gained > 0.01 * a["chars"] or defrag > 1.0:
        print(f"  VERDICT: KEEP THE FILTERS. They restore {gained:,} chars "
              f"({100 * gained / max(a['chars'], 1):.1f}%) and cut fragmentation "
              f"by {defrag:.1f} points.")
    elif set(lv) <= {1}:
        print("  VERDICT: no inferred levels — this parse predates the release or the\n"
              "           document has no structure to infer from. Filters are harmless.")
    else:
        print("  VERDICT: no meaningful difference. This document is cleanly structured;\n"
              "           the filters cost nothing and are worth leaving on for the next one.")

    if verbose:
        print("\n  section labels the filters REJECTED (now indexed as body text):")
        rejected = {r["section"] for r in off} - {r["section"] for r in on}
        for s in sorted(rejected, key=len, reverse=True)[:12]:
            print(f"    - {s[:96]}")
        if not rejected:
            print("    (none)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="Docling .json export(s)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="show rejected headings and QA examples")
    args = ap.parse_args()

    seen = 0
    for pattern in args.files:
        for p in sorted(pathlib.Path().glob(pattern)) or [pathlib.Path(pattern)]:
            if not p.exists():
                print(f"skip (not found): {p}", file=sys.stderr)
                continue
            if p.suffix.lower() != ".json":
                print(f"skip (not a .json export): {p.name}", file=sys.stderr)
                continue
            try:
                report(p, args.verbose)
                seen += 1
            except ValueError as e:      # read_docling_json's schema guard
                print(f"skip: {e}", file=sys.stderr)
    if not seen:
        sys.exit("Nothing checked.")
    print()


if __name__ == "__main__":
    main()
