#!/usr/bin/env python3
"""
extract_links.py — harvest source URLs out of a PDF and sort them by authority.

WHAT THIS IS FOR
----------------
Finding official state payroll regulations by hand means searching 50 state
websites and following link trails until something official appears. The owner, on
doing Colorado that way: "it wasn't fun."

An AI-generated summary of a state's payroll law is not usable as a corpus — it
is a tertiary source, and the 2026-08-02 preflight already caught one of them
padding itself with a "§6.1 Metadata Anchors" section. But those documents cite
their sources as real embedded hyperlinks, and THAT part is valuable: it is a
bibliography someone else assembled. This script throws away the prose and keeps
the bibliography, sorted so the official sources are obvious.

    generated PDF  ──►  extract_links.py  ──►  list of .gov URLs
                                                     │
                                        you download the PRIMARY documents
                                                     │
                                          Docling ──►  dropzone/  ──►  ingest.py

The output is deliberately NOT a corpus. It is a shopping list.

WHAT IT DOES NOT DO
-------------------
It cannot tell you whether a link supports the sentence it was attached to. A
URL that resolves is LIVENESS, NOT CORRECTNESS — the same distinction that cost
four days on the reranker (2026-08-01), where /info reported healthy while every
inference returned NaN. --check confirms a page exists. Only reading the primary
document confirms the law.

USAGE
-----
    python extract_links.py "Texas_Payroll_Laws 2.pdf"
    python extract_links.py *.pdf --csv links.csv
    python extract_links.py new_jersey_payroll_laws.pdf --yaml   # manifest stub
    python extract_links.py somefile.pdf --check                 # verify each URL resolves
    python extract_links.py somefile.pdf --official-only

Requires: pymupdf (already a transitive dep via pymupdf4llm), requests for --check.
"""
import argparse
import collections
import csv
import re
import sys
import urllib.parse
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF is required:  pip install pymupdf")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
# Three buckets, and only three, because domain is the ONLY signal available
# here that is actually reliable. Guessing "law firm blog" vs "payroll vendor
# marketing page" from a hostname produces confident wrong answers, and a
# misfiled source is worse than an unfiled one — the whole point of the
# `authority:` field is that a summary never gets cited to an employer as if it
# were the rule. UNCLASSIFIED means "a human looks at this", which is honest.

OFFICIAL_SUFFIXES = (".gov", ".mil")
# State sites still using the legacy pattern: dol.state.nj.us, twc.state.tx.us
OFFICIAL_PATTERNS = (
    re.compile(r"\.state\.[a-z]{2}\.us$"),
    re.compile(r"^[a-z0-9-]+\.[a-z]{2}\.us$"),
)

# Well-known legal aggregators and reference publishers. Useful — often the
# fastest way to READ a statute — but they are republishers, not the issuing
# authority, and their archives carry superseded year-editions. The Texas
# document cited five different Justia year-editions of one chapter as five
# separate references, which is how a citation count gets inflated.
KNOWN_SECONDARY = {
    "law.justia.com", "justia.com", "public.law", "casetext.com",
    "findlaw.com", "law.cornell.edu", "courtlistener.com", "leagle.com",
    "nolo.com", "lexisnexis.com", "westlaw.com", "govregs.com",
}

# FEDERAL issuers. A state summary always cites some of these (the FLSA baseline
# is what a state exceeds, matches, or is silent about), and they must be tagged
# `jurisdiction: US`, not the state — otherwise `query.py -j ME` would return
# federal text labelled as Maine law, which is a worse error than missing it.
FEDERAL_DOMAINS = {
    "dol.gov", "irs.gov", "ssa.gov", "eeoc.gov", "nlrb.gov", "uscis.gov",
    "gpo.gov", "govinfo.gov", "ecfr.gov", "federalregister.gov",
    "congress.gov", "bls.gov", "treasury.gov",
}

# News and trade press. Useful for "when did this change and why", never for
# what the rule IS.
KNOWN_NEWS = {
    "texastribune.org", "news.bloombergtax.com", "bloombergtax.com",
    "thetexan.news", "reuters.com", "apnews.com", "law360.com", "shrm.org",
    "natlawreview.com", "jdsupra.com",
}

OFFICIAL, SECONDARY, NEWS, UNCLASSIFIED = "OFFICIAL", "SECONDARY", "NEWS", "UNCLASSIFIED"
BUCKET_ORDER = [OFFICIAL, SECONDARY, NEWS, UNCLASSIFIED]

BUCKET_NOTE = {
    OFFICIAL: "issuing authority — DOWNLOAD THESE (authority: primary)",
    SECONDARY: "republisher / aggregator — fine for reading, cite the original (authority: secondary)",
    NEWS: "reporting and commentary — context only, never the rule",
    UNCLASSIFIED: "unknown — law-firm posts and vendor pages land here. Triage by eye.",
}


def registrable(netloc: str) -> str:
    """'www.nj.gov' -> 'nj.gov'. Crude but adequate for grouping US sources."""
    host = netloc.lower().split(":")[0]
    parts = host.split(".")
    if len(parts) > 2 and parts[-1] == "us":       # dol.state.nj.us
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) > 2 else host


def classify(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    if host.endswith(OFFICIAL_SUFFIXES) or any(p.search(host) for p in OFFICIAL_PATTERNS):
        return OFFICIAL
    reg = registrable(host)
    if host in KNOWN_SECONDARY or reg in KNOWN_SECONDARY:
        return SECONDARY
    if host in KNOWN_NEWS or reg in KNOWN_NEWS:
        return NEWS
    return UNCLASSIFIED


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
URL_RE = re.compile(r"https?://[^\s<>\"'\)\]\}]+", re.I)
# Trailing punctuation that belongs to the sentence, not the URL.
TRAILING = ".,;:!?’”'\")]}>"


def clean_url(u: str) -> str:
    u = u.strip()
    while u and u[-1] in TRAILING:
        u = u[:-1]
    return u


def extract(pdf_path: Path):
    """Return [{url, page, anchor, kind}] — annotations first, then bare text URLs.

    Two sources, because generated PDFs use both: real link annotations (the
    clickable rectangles) and plain-text URLs printed in a references list.
    Taking only one of them silently loses sources.
    """
    doc = fitz.open(pdf_path)
    found, seen = [], {}

    for pno in range(len(doc)):
        page = doc[pno]
        for link in page.get_links():
            uri = link.get("uri")
            if not uri or not uri.lower().startswith("http"):
                continue
            uri = clean_url(uri)
            # The text sitting under the link rectangle. For a references list
            # this is the document's title ("NJDOL — Earned Sick Leave Law"),
            # which is exactly what you want the downloaded file to be named.
            # For inline citations it is just a marker like "[40]".
            try:
                anchor = " ".join(page.get_textbox(link["from"]).split())[:120]
            except Exception:
                anchor = ""
            if uri not in seen:
                seen[uri] = {"url": uri, "page": pno + 1, "anchor": anchor,
                             "kind": "link", "count": 0}
                found.append(seen[uri])
            seen[uri]["count"] += 1
            if anchor and not _useful(seen[uri]["anchor"]) and _useful(anchor):
                seen[uri]["anchor"] = anchor

        for m in URL_RE.finditer(page.get_text()):
            uri = clean_url(m.group(0))
            if uri and uri not in seen:
                seen[uri] = {"url": uri, "page": pno + 1, "anchor": "",
                             "kind": "text", "count": 1}
                found.append(seen[uri])

    doc.close()

    # DROP WRAPPED FRAGMENTS. A references section prints URLs as visible text,
    # and a long one wraps mid-URL — PyMuPDF then reads the first line only, so
    # you get `https://www.twdb.texas.gov/financial/instructions/doc/` as a
    # separate "source". It isn't; it's the head of a URL already captured as a
    # clickable annotation. Any text-derived URL that is a strict prefix of an
    # annotation URL is a fragment. (Measured on the Texas file: 22 of 69
    # apparent sources were this.)
    annots = [f["url"] for f in found if f["kind"] == "link"]
    kept = []
    for f in found:
        if f["kind"] == "text" and any(
            a != f["url"] and a.startswith(f["url"]) for a in annots
        ):
            continue
        kept.append(f)

    for f in kept:
        f["bucket"] = classify(f["url"])
        f["domain"] = registrable(urllib.parse.urlparse(f["url"]).netloc)
    return kept


def _useful(anchor: str) -> bool:
    """'[40]' is not a title. 'NJDOL — Earned Sick Leave Law' is."""
    return bool(anchor) and not re.fullmatch(r"[\[\(]?\d{1,3}[\]\).,]?", anchor.strip())


def check_liveness(rows, timeout=12):
    """HEAD (falling back to GET) each URL. Records status only.

    Reminder in code, because it is the mistake this project keeps making:
    a 200 means the PAGE EXISTS. It does not mean the page says what the
    document claimed it says.
    """
    try:
        import requests
    except ImportError:
        sys.exit("--check needs requests:  pip install requests")
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (corpus link check)"
    for r in rows:
        try:
            resp = sess.head(r["url"], allow_redirects=True, timeout=timeout)
            if resp.status_code >= 400:
                resp = sess.get(r["url"], allow_redirects=True, timeout=timeout, stream=True)
            r["status"] = resp.status_code
            final = resp.url.rstrip("/")
            r["redirect"] = final if final != r["url"].rstrip("/") else ""
        except Exception as e:
            r["status"] = "ERR"
            r["redirect"] = type(e).__name__
        print(f"  {r['status']:>4}  {r['url'][:96]}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def report(rows, source_name, official_only=False):
    buckets = collections.defaultdict(list)
    for r in rows:
        buckets[r["bucket"]].append(r)

    print(f"\n{'=' * 78}\n{source_name}  —  {len(rows)} unique URL(s)\n{'=' * 78}")
    counts = "  ".join(f"{b}={len(buckets[b])}" for b in BUCKET_ORDER if buckets[b])
    print(f"  {counts}\n")

    for bucket in BUCKET_ORDER:
        items = buckets.get(bucket)
        if not items or (official_only and bucket != OFFICIAL):
            continue
        print(f"\n--- {bucket}  ({len(items)})  — {BUCKET_NOTE[bucket]}")
        by_domain = collections.defaultdict(list)
        for r in items:
            by_domain[r["domain"]].append(r)
        for domain in sorted(by_domain, key=lambda d: (-len(by_domain[d]), d)):
            group = by_domain[domain]
            print(f"\n  {domain}  ({len(group)})")
            for r in sorted(group, key=lambda x: x["page"]):
                status = f" [{r['status']}]" if "status" in r else ""
                print(f"    p{r['page']:<3}{status} {r['url']}")
                if _useful(r["anchor"]):
                    print(f"          “{r['anchor']}”")
    print()


def write_csv(rows, path):
    cols = ["bucket", "domain", "url", "anchor", "page", "kind", "count",
            "status", "redirect", "source_pdf"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: (BUCKET_ORDER.index(x["bucket"]),
                                             x["domain"], x["page"])):
            w.writerow(r)
    print(f"CSV written: {path}  ({len(rows)} rows)")


def write_yaml_stub(rows, jurisdiction=None):
    """Emit a commented candidate-source block for manifest.yaml.

    Commented on purpose. These are CANDIDATES — you have not downloaded them,
    you cannot hash them, and you have not confirmed they are the current
    edition. Uncomment an entry only once the file is on disk.
    """
    official = [r for r in rows if r["bucket"] == OFFICIAL]
    jur = (jurisdiction or "XX").upper()
    print("\n# " + "-" * 74)
    print(f"# CANDIDATE SOURCES for {jur} — harvested from a generated summary PDF.")
    print("# These are LEADS, not documents. Download the primary source, run it")
    print("# through Docling, then fill in `file:` and `sha256:` and uncomment.")
    print("# " + "-" * 74)
    for r in sorted(official, key=lambda x: (x["domain"], x["page"])):
        title = r["anchor"] if _useful(r["anchor"]) else ""
        is_federal = r["domain"] in FEDERAL_DOMAINS
        code = "US" if is_federal else jur
        print(f"#  - file: US-{code}-<name>.json")
        print(f"#    sha256: <fill after download>")
        print(f"#    jurisdiction: {code}")
        print(f"#    level: {'federal' if is_federal else 'state'}")
        print(f"#    authority: primary")
        print(f"#    source_url: {r['url']}")
        if title:
            print(f"#    notes: {title}")
        print("#")
    if not official:
        print("#  (no .gov sources found — this document cites no primary law)")


def main():
    ap = argparse.ArgumentParser(
        description="Harvest and classify source URLs from PDFs.")
    ap.add_argument("pdfs", nargs="+", help="PDF file(s)")
    ap.add_argument("--csv", help="write all rows to this CSV")
    ap.add_argument("--yaml", action="store_true",
                    help="print a commented manifest.yaml candidate block")
    ap.add_argument("--jurisdiction", "-j", help="2-letter code used in the --yaml stub")
    ap.add_argument("--check", action="store_true",
                    help="verify each URL resolves (liveness only — NOT correctness)")
    ap.add_argument("--official-only", action="store_true",
                    help="print only .gov/.us sources")
    args = ap.parse_args()

    all_rows = []
    for spec in args.pdfs:
        path = Path(spec)
        if not path.is_file():
            print(f"skip (not a file): {path}", file=sys.stderr)
            continue
        rows = extract(path)
        for r in rows:
            r["source_pdf"] = path.name
        if args.check:
            print(f"checking {len(rows)} URLs from {path.name}…", file=sys.stderr)
            check_liveness(rows)
        report(rows, path.name, official_only=args.official_only)
        if args.yaml:
            write_yaml_stub(rows, args.jurisdiction)
        all_rows.extend(rows)

    if args.csv and all_rows:
        write_csv(all_rows, args.csv)

    n_off = sum(1 for r in all_rows if r["bucket"] == OFFICIAL)
    print(f"\nTotal: {len(all_rows)} unique URLs, {n_off} official.")
    if n_off:
        print("Next: download the OFFICIAL ones, parse with Docling, add to "
              "dropzone/<collection>/ and manifest.yaml.")


if __name__ == "__main__":
    main()
