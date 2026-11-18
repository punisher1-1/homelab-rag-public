#!/usr/bin/env python3
"""
corpus_profile.py — declare a document's own conventions, then VERIFY the claim.

WHY THIS EXISTS
---------------
Publishers number pages, label sections and mark cross-references their own way.
TxDOT numbers per chapter (`4-4`, `A-1`); NIST AI 100-1 numbers flat (`7`); the
TxDOT structural manual marks references to the AASHTO spec with angle brackets
(`<Table 3.4.1-1>`) and nothing else in the corpus does. A 50-state payroll corpus
will have 50 of these.

The tempting fix is a script per document. That ages badly, and 2026-08-18 is the
evidence: three separate defects in ONE shared `printed_page_map()` were found
because a single function ran across nine documents and a corpus-wide sweep showed
which ones changed. Nine bespoke scripts would each have "worked" on their own
document, and nobody would ever have run the comparison.

So conventions go in `manifest.yaml` as DATA, next to `jurisdiction` and `sha256`,
and this module does the thing that makes data worth having: **it treats a profile
as a claim about the document and checks it, exactly like `sha256` is checked.**

    profile:
      page_number: '\\d+-\\d+'      # "printed pages look like this"
      external_ref: '<[^>]+>'      # "these point outside the document"

An unverified profile is worse than none, because it reads as a description of the
document while being a description of what somebody assumed. Every failure this
project has logged has that shape.

WHAT GOES HERE vs WHAT GOES IN ingest.py
----------------------------------------
The rule was derived on 2026-08-07, when `DOCLING_MAX_HEADING_LEVEL` turned out to
be "a per-document property masquerading as a setting" (two ATF documents disagreed
about it) while `DOCLING_MAX_HEADING_CHARS` stayed global because a heading longer
than 80 characters is a sentence in ANY corpus.

    universal truth  -> code, as a default        (a 4-digit year is not a page number)
    document property -> manifest, as a profile   (how THIS publisher numbers pages)

Test: if you would want the rule applied to a document you have not seen yet, it
belongs in code.

USAGE
-----
    python3 corpus_profile.py dropzone/TXDOT            # exit 1 if any FAIL
    python3 corpus_profile.py dropzone/Payroll --strict # WARN also exits 1
    python3 corpus_profile.py dropzone/TXDOT --quiet

Reads Docling JSON exports only (a profile is checked against parsed structure, so
there is nothing to check on a raw .pdf entry — those are skipped and reported).

Depends on `figure_index.py` for document walking (stdlib only) and PyYAML via
`corpus_manifest.load()`. No database, no embedder, safe to run anywhere.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import corpus_manifest
import figure_index as fx

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Every key a profile may contain. An UNKNOWN KEY IS AN ERROR, not a warning:
# a typo'd key is silently inert, which is the exact failure mode this file
# exists to stop. `collection: atf` matching zero rows for weeks, `contains: "40"`
# never matching its passage, DOCLING_MAX_HEADING_LEVEL erasing every (Nx) label
# — all of them silent, all of them found late.
PROFILE_KEYS = {
    # --- assertions: the document must mostly match, or the profile is wrong ---
    "page_number":   "regex a printed page footer must match, e.g. '\\d+-\\d+'",
    "section_id":    "regex a section label should match, e.g. '\\d+(\\.\\d+)*'",
    # --- detectors: things to find and flag, zero matches means a stale profile ---
    "external_ref":  "regex marking a reference OUT of this document, e.g. '<[^>]+>'",
    "foreign_text":  "list of regexes marking text belonging to ANOTHER document",
    "drop_sections": "list of heading substrings that are boilerplate, not content",
    # --- canonicalisation ---
    "citation_form": "template for the canonical citation, e.g. 'Tex. Lab. Code § {section}'",
    # --- thresholds ---
    "min_match":     "fraction of items an assertion must match (default 0.80)",
}

DEFAULT_MIN_MATCH = 0.80

# Fields a citation_form template may reference.
CITATION_FIELDS = {"section", "page", "printed_page", "title", "jurisdiction",
                   "jurisdiction_name"}

FAIL, WARN, OK, SKIP = "FAIL", "WARN", "ok", "skip"


class Finding:
    __slots__ = ("level", "check", "detail")

    def __init__(self, level, check, detail):
        self.level, self.check, self.detail = level, check, detail

    def __str__(self):
        mark = {FAIL: "⛔ FAIL", WARN: "⚠️  WARN", OK: "   ok ", SKIP: "   -- "}[self.level]
        return f"  {mark}  {self.check:<16} {self.detail}"


# ---------------------------------------------------------------------------
# Schema validation — check the profile before checking the document
# ---------------------------------------------------------------------------

def validate_schema(profile: dict) -> list[Finding]:
    out = []
    unknown = [k for k in profile if k not in PROFILE_KEYS]
    if unknown:
        out.append(Finding(FAIL, "schema",
                           f"unknown key(s): {', '.join(sorted(unknown))}. "
                           f"Known: {', '.join(sorted(PROFILE_KEYS))}"))

    for key in ("page_number", "section_id", "external_ref"):
        pat = profile.get(key)
        if pat is None:
            continue
        if not isinstance(pat, str):
            out.append(Finding(FAIL, "schema", f"`{key}` must be a single regex string"))
            continue
        try:
            re.compile(pat)
        except re.error as e:
            out.append(Finding(FAIL, "schema", f"`{key}` is not a valid regex: {e}"))

    for key in ("foreign_text", "drop_sections"):
        val = profile.get(key)
        if val is None:
            continue
        if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            out.append(Finding(FAIL, "schema", f"`{key}` must be a list of strings"))
            continue
        if key == "foreign_text":
            for v in val:
                try:
                    re.compile(v)
                except re.error as e:
                    out.append(Finding(FAIL, "schema", f"`foreign_text` entry {v!r}: {e}"))

    tmpl = profile.get("citation_form")
    if tmpl is not None:
        if not isinstance(tmpl, str):
            out.append(Finding(FAIL, "schema", "`citation_form` must be a string"))
        else:
            fields = set(re.findall(r"\{(\w+)\}", tmpl))
            if not fields:
                out.append(Finding(FAIL, "schema",
                                   "`citation_form` has no {placeholder} — it would be "
                                   "the same literal string on every chunk"))
            bad = fields - CITATION_FIELDS
            if bad:
                out.append(Finding(FAIL, "schema",
                                   f"`citation_form` references unknown field(s): "
                                   f"{', '.join(sorted(bad))}. "
                                   f"Available: {', '.join(sorted(CITATION_FIELDS))}"))

    mm = profile.get("min_match", DEFAULT_MIN_MATCH)
    if not isinstance(mm, (int, float)) or not 0 < mm <= 1:
        out.append(Finding(FAIL, "schema", "`min_match` must be a number in (0, 1]"))

    if not out:
        out.append(Finding(OK, "schema", f"{len(profile)} key(s), all known and well-formed"))
    return out


# ---------------------------------------------------------------------------
# Document checks — does the document actually behave the way the profile says?
# ---------------------------------------------------------------------------

def check_page_number(doc: dict, pattern: str, min_match: float) -> list[Finding]:
    """The printed-page footers must match the declared shape.

    This is the check that would have caught the 2026-08-18 truncation defect on
    the day it shipped: with `page_number: '\\d+-\\d+'` declared for lad, a map
    full of bare `2`, `20`, `4` matches 0% and fails loudly, instead of sitting
    in the corpus for eight days looking like a page number.
    """
    rx = re.compile(pattern)
    pmap = fx.printed_page_map(doc)
    if not pmap:
        return [Finding(WARN, "page_number",
                        "declared, but the document has no numbered footers at all "
                        "(scanned, or screenshots only)")]
    hits = [v for v in pmap.values() if rx.fullmatch(str(v))]
    frac = len(hits) / len(pmap)
    misses = sorted({str(v) for v in pmap.values() if not rx.fullmatch(str(v))})
    level = OK if frac >= min_match else FAIL
    detail = (f"{len(hits)}/{len(pmap)} pages match /{pattern}/ ({frac:.0%})")
    if misses:
        detail += f" · non-matching values: {', '.join(misses[:6])}"
    return [Finding(level, "page_number", detail)]


def check_page_monotonic(doc: dict) -> list[Finding]:
    """Printed pages should advance by one within a chapter.

    Runs whenever `page_number` is declared, no configuration. A jump out of
    sequence means either the footer regex is truncating, or a page number was
    harvested from a SCREENSHOT of another document — the contamination class
    found on 2026-08-18, where lad carries 'Page 39 of 60' from the Amarillo
    Master Plan. Cheap, and it needs no ground truth.
    """
    pmap = fx.printed_page_map(doc)
    pairs = re.compile(r"(\d+)-(\d+)")
    prev = None
    bad = []
    for pdf_page in sorted(pmap):
        m = pairs.fullmatch(str(pmap[pdf_page]))
        if not m:
            continue
        cur = (int(m.group(1)), int(m.group(2)))
        if prev and pdf_page == prev[0] + 1:
            pv = prev[1]
            ok = (cur[0] == pv[0] and cur[1] == pv[1] + 1) or \
                 (cur[0] == pv[0] + 1 and cur[1] == 1)
            if not ok:
                bad.append(f"pdf {prev[0]}={pv[0]}-{pv[1]} -> pdf {pdf_page}={cur[0]}-{cur[1]}")
        prev = (pdf_page, cur)
    if not prev:
        return [Finding(SKIP, "page_order", "no chapter-relative pages to sequence")]
    if bad:
        return [Finding(WARN, "page_order",
                        f"{len(bad)} irregular step(s): {'; '.join(bad[:3])}")]
    return [Finding(OK, "page_order", f"{len(pmap)} pages, sequence unbroken")]


def check_section_id(doc: dict, pattern: str, min_match: float) -> list[Finding]:
    """Section labels should look like section identifiers.

    A WARN rather than a FAIL, because real documents carry unnumbered headings
    ('Contents', 'References') legitimately. What it catches is the junk-heading
    class: '§Focus the view on the intersection.' scored rank 1 on 2026-08-18 with
    a citation a reader cannot look up.
    """
    rx = re.compile(pattern)
    heads = [(t.get("text") or "").strip() for t in doc.get("texts", [])
             if (t.get("label") or "") in ("section_header", "title")]
    heads = [h for h in heads if h]
    if not heads:
        return [Finding(WARN, "section_id", "no section headers in this document")]
    hits = [h for h in heads if rx.match(h)]
    frac = len(hits) / len(heads)
    level = OK if frac >= min_match else WARN
    junk = [h for h in heads if not rx.match(h) and h.rstrip().endswith((".", ";", ":", ","))]
    detail = f"{len(hits)}/{len(heads)} headings match /{pattern}/ ({frac:.0%})"
    if junk:
        detail += f" · {len(junk)} end in sentence punctuation, e.g. {junk[0][:52]!r}"
    return [Finding(level, "section_id", detail)]


def check_external_ref(doc: dict, pattern: str) -> list[Finding]:
    """Count references that point OUT of this document.

    Zero matches means the profile is describing a convention the document does
    not use — stale or copied from a sibling. Non-zero is not a problem, it is
    inventory: those chunks retrieve well and answer nothing, which is worse than
    a miss because it looks complete.
    """
    rx = re.compile(pattern)
    n, pages = 0, set()
    for t in doc.get("texts", []):
        found = rx.findall(t.get("text") or "")
        if found:
            n += len(found)
            prov = t.get("prov") or []
            if prov:
                pages.add(prov[0].get("page_no"))
    if n == 0:
        return [Finding(WARN, "external_ref",
                        f"/{pattern}/ never matches — profile is stale or copied "
                        "from another document")]
    return [Finding(OK, "external_ref",
                    f"{n} reference(s) point outside this document, on {len(pages)} page(s) "
                    "— flag these chunks, they cannot be answered from this corpus")]


def check_foreign_text(doc: dict, patterns: list[str]) -> list[Finding]:
    """Find text belonging to a DIFFERENT document.

    Manuals that reproduce other publications as figures hand you their page
    numbers, titles and headings as body text. Measured on lad 2026-08-18: 15
    items from the Amarillo Master Plan, including 'Page 39 of 60' in chunk text
    and 'Hollywood/Loop 335' promoted to a section_header, so chunks are cited
    with a section that does not exist in the manual.
    """
    out = []
    for pat in patterns:
        rx = re.compile(pat)
        hits = []
        for t in doc.get("texts", []):
            if rx.search(t.get("text") or ""):
                prov = t.get("prov") or []
                hits.append((prov[0].get("page_no") if prov else None,
                             t.get("label"), (t.get("text") or "")[:44]))
        if hits:
            worst = [h for h in hits if h[1] in ("section_header", "title", "page_footer")]
            level = FAIL if worst else WARN
            detail = f"/{pat}/ matches {len(hits)} item(s)"
            if worst:
                detail += (f" — {len(worst)} became a HEADING or FOOTER, "
                           f"e.g. p{worst[0][0]} [{worst[0][1]}] {worst[0][2]!r}")
            out.append(Finding(level, "foreign_text", detail))
        else:
            out.append(Finding(OK, "foreign_text", f"/{pat}/ clean"))
    return out


def check_drop_sections(doc: dict, names: list[str]) -> list[Finding]:
    """Boilerplate sections declared for removal must actually exist.

    A name that matches nothing is a stale profile entry, and it reads as
    protection you do not have. Targets the self-description class: 'Metadata
    Anchors', 'Keywords', 'Index' — sections that list every topic in the file,
    contain none of the substance, and satisfy eval clauses on their own.
    """
    heads = [(t.get("text") or "") for t in doc.get("texts", [])
             if (t.get("label") or "") in ("section_header", "title")]
    out = []
    for name in names:
        n = sum(1 for h in heads if name.lower() in h.lower())
        if n:
            out.append(Finding(OK, "drop_sections", f"{name!r} present ({n}) — strip on re-parse"))
        else:
            out.append(Finding(WARN, "drop_sections", f"{name!r} matches nothing — stale entry"))
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def check_document(path: pathlib.Path, entry: dict) -> tuple[list[Finding], dict]:
    profile = entry.get("profile") or {}
    if not profile:
        return [Finding(SKIP, "profile", "no profile declared")], {}

    findings = validate_schema(profile)
    if any(f.level == FAIL for f in findings):
        findings.append(Finding(SKIP, "document",
                                "schema failed — document checks not run"))
        return findings, profile

    if path.suffix.lower() != ".json":
        findings.append(Finding(SKIP, "document",
                                f"{path.suffix} entry — a profile is checked against parsed "
                                "structure, so convert to Docling JSON first"))
        return findings, profile

    try:
        doc = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError) as e:
        findings.append(Finding(FAIL, "document", f"cannot read {path.name}: {e}"))
        return findings, profile
    if doc.get("schema_name") != "DoclingDocument":
        findings.append(Finding(FAIL, "document",
                                f"not a Docling export (schema_name="
                                f"{doc.get('schema_name')!r})"))
        return findings, profile

    mm = float(profile.get("min_match", DEFAULT_MIN_MATCH))
    if "page_number" in profile:
        findings += check_page_number(doc, profile["page_number"], mm)
        findings += check_page_monotonic(doc)
    if "section_id" in profile:
        findings += check_section_id(doc, profile["section_id"], mm)
    if "external_ref" in profile:
        findings += check_external_ref(doc, profile["external_ref"])
    if "foreign_text" in profile:
        findings += check_foreign_text(doc, profile["foreign_text"])
    if "drop_sections" in profile:
        findings += check_drop_sections(doc, profile["drop_sections"])
    return findings, profile


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=pathlib.Path, help="an approved document collection folder")
    ap.add_argument("--strict", action="store_true", help="treat WARN as failure")
    ap.add_argument("--quiet", action="store_true", help="only print WARN and FAIL")
    args = ap.parse_args(argv)

    try:
        entries = corpus_manifest.load(args.folder)
    except corpus_manifest.ManifestError as e:
        print(f"⛔ {e}", file=sys.stderr)
        return 2
    if not entries:
        # Distinguish "no manifest file" from "manifest with no documents listed".
        # The first is a missing control; the second is a collection that has not been
        # populated yet. Reporting both as "no manifest" is the same class of
        # misleading-silence this file exists to prevent.
        if (args.folder / corpus_manifest.MANIFEST_NAME).is_file():
            print(f"{args.folder}/{corpus_manifest.MANIFEST_NAME} exists but lists no "
                  f"documents — nothing to check yet")
        else:
            print(f"no {corpus_manifest.MANIFEST_NAME} in {args.folder} — "
                  f"documents here will be ingested with no jurisdiction or authority")
        return 0

    fails = warns = profiled = 0
    for name, entry in sorted(entries.items()):
        path = args.folder / name
        findings, profile = check_document(path, entry)
        if profile:
            profiled += 1
        shown = [f for f in findings if not (args.quiet and f.level in (OK, SKIP))]
        if shown:
            print(f"\n{name}")
            for f in shown:
                print(f)
        fails += sum(1 for f in findings if f.level == FAIL)
        warns += sum(1 for f in findings if f.level == WARN)

    print(f"\n{'=' * 72}")
    print(f"  {len(entries)} document(s), {profiled} with a profile · "
          f"{fails} FAIL · {warns} WARN")
    if fails:
        print("  ⛔ A profile is a CLAIM about a document. One of them is false.")
        return 1
    if warns and args.strict:
        print("  ⚠️  --strict: warnings treated as failure.")
        return 1
    print("  ✅ Every declared convention holds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
