#!/usr/bin/env python3
"""
kb_capture.py — turn a vendor forum answer into a structured, provenance-carrying KB note.

WHY THIS EXISTS
---------------
Bentley administrators answer real questions on Bentley Communities, and those answers
solve problems the official manuals do not cover. The content is worth having. The
FORMAT is the risk.

⛔ **Reformatting a forum post so it looks like an internal KB article erases the one
signal that matters.** A retrieved chunk carries its formatting, not your intent, so a
tech reading the answer cannot tell house documentation from a vendor post you liked.
That is laundering, even when the underlying content is good.

So this tool does the opposite of what "make it look like our KB" implies:

    NORMALISE THE STRUCTURE   (same skeleton, so retrieval behaves consistently)
    KEEP THE IDENTITY LOUD    (authority, responder, date and URL in the text itself)

Same distinction as the normalisation work on 2026-08-18: canonicalisation should make
one fact have one spelling, never make two sources look like one source.

THE FILTER IS A FIELD, NOT A HABIT
----------------------------------
What makes this corpus good is that only **Bentley administrators'** answers go in.
Right now that filter is the owner's judgement, and judgement does not survive automation:
the moment collection is scripted, "only admins" has to be something a script can
evaluate or quality silently reverts to general forum content while the corpus keeps
its trustworthy name. Hence `responder_role`, required and validated.

WHY TWO COLLECTIONS
-------------------
    dropzone/bentley-kb/       one MD per issue, living, community-derived
    dropzone/bentley-manuals/  vendor PDFs via Docling JSON, authoritative

Not one collection with a metadata flag, for three reasons:

1. **Different authority.** A manual is vendor-published; a forum answer is a vendor
   employee typing into a web form. Both useful, not interchangeable.
2. **Different cadence.** The KB grows as problems are solved; manuals change on
   release cycles.
3. ⭐ **The PoC question requires it.** "Do these community-derived fixes add value
   over the manuals?" is only answerable if the two are separately scopable. Merge
   them and the experiment cannot be run.

Also practical: `corpus_manifest.load()` reads `folder/manifest.yaml` and
`metadata_for()` looks at `path.parent`, so documents must sit **beside** their
manifest. Two collections respects that; nested subfolders would silently lose their
metadata.

USAGE
-----
    # blank template to fill in by hand (the "manual capture" path)
    python3 kb_capture.py new --title "ProjectWise DGN opens read-only" \\
        --out dropzone/bentley-kb/

    # wrap text already captured into Obsidian or a text file
    python3 kb_capture.py from-text answer.txt --title "..." --url "https://..." \\
        --out dropzone/bentley-kb/

    # best-effort extraction from a page saved in the browser (always -> status: draft)
    python3 kb_capture.py from-html saved_thread.html --out dropzone/bentley-kb/

    # ⭐ check every note in a collection: exit 1 on any FAIL
    python3 kb_capture.py validate dropzone/bentley-kb/

Stdlib + PyYAML (already in requirements.txt). No database, no network.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys
from html.parser import HTMLParser

try:
    import yaml
except ImportError:
    yaml = None

# ---------------------------------------------------------------------------
# Vocabulary — closed sets, because a typo in an open field is silently inert
# ---------------------------------------------------------------------------

AUTHORITY = {
    "vendor-manual":  "published by the vendor — a manual, release note, official help topic",
    "vendor-support": "a support case or direct answer from vendor support",
    "vendor-forum":   "a vendor EMPLOYEE answering on the vendor's own community",
    "community":      "a non-employee answering. Allowed, but say so",
    "internal":       "written by us",
}

RESPONDER_ROLE = {
    "bentley_admin", "bentley_support", "vendor_employee",
    "community_member", "internal", "unknown",
}

# Roles that do NOT justify an `authority: vendor-*` claim.
WEAK_ROLES = {"community_member", "unknown"}

STATUS = {
    "draft":      "captured, not yet checked by a human",
    "verified":   "someone applied it or reviewed it and it holds",
    "superseded": "kept for the record; a newer note replaces it",
}

REQUIRED_FIELDS = ("title", "collection", "authority", "source", "product",
                   "version", "status", "retrieved")
REQUIRED_SECTIONS = ("Symptom", "Environment", "Resolution", "Source")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_RE = re.compile(r"^https?://\S+$")

# ⛔ Phrases that mean the text is a QUESTION, not an answer.
#
# From a real Obsidian Web Clipper capture of a Bentley Civil Programming Forum
# thread (2026-08-19). The clipper returned: an empty `> [!success] Success` toast,
# the literal label "Question", the thread title, and the ASKER'S CODE — with no
# answer, no author, no date and no URL.
#
# That is the dangerous case, not merely an incomplete one. The captured C# uses real
# Bentley API names and is phrased exactly as a developer would search for it, so it
# retrieves beautifully. And the asker's own words are *"it looks Alignment.Element is
# not really valid"* — THE CAPTURED CODE IS THE CODE THAT FAILED. Ingested, a model
# could hand a developer the broken attempt as the fix, which is strictly worse than
# returning nothing.
QUESTION_MARKERS = (
    "thanks in advance", "any ideas", "any suggestions", "please help",
    "just wonder", "am i missing", "anyone know", "has anyone",
    "is not really valid", "doesn't work", "does not work", "not working",
    "any help would", "i'm stuck", "im stuck",
)

FAIL, WARN, OK = "FAIL", "WARN", "ok"


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

TEMPLATE = """---
title: {title_q}
collection: {collection_q}
authority: vendor-forum          # {authority_opts}
source: bentley-communities
source_url: {url_q}
responder_role: bentley_admin    # REQUIRED for vendor-forum — the filter as a field
post_date: {post_date_q}           # when the answer was written
retrieved: {retrieved_q}           # when you captured it
product: {product_q}
version: {version_q}               # "unknown" is allowed, but say it explicitly
status: draft                    # draft | verified | superseded
verified_by:
verified_date:
supersedes:
promoted_to_itglue:              # fill in when this graduates into IT Glue
tags: []
---

# {title}

## Symptom

<what the user sees, in the words a user would use — this is what a query will match>

## Environment

<product, version, OS, licensing mode, anything that decides whether this applies>

## Resolution

<the steps. Keep the vendor's wording where it is precise; do not paraphrase a
setting name>

## Source

{source_line}

## Notes

<your own observations. Mark clearly as ours, not theirs>
"""


def source_line(meta: dict) -> str:
    """The provenance sentence that gets INDEXED with the chunk.

    Deliberately prose rather than a bare URL: it puts the authority claim in front of
    the model as literal text it can copy, the same argument that justified stamping
    citations into chunk_text in the first place.
    """
    who = {
        "bentley_admin":   "a Bentley administrator",
        "bentley_support": "Bentley support",
        "vendor_employee": "a vendor employee",
        "community_member": "a community member (NOT a vendor employee)",
        "internal":        "an internal author",
    }.get(meta.get("responder_role", ""), "an unidentified responder")
    when = meta.get("post_date") or meta.get("retrieved") or "date unknown"
    ver = meta.get("version") or "unknown version"
    url = meta.get("source_url") or "(no URL recorded)"
    return (f"Answered by **{who}** on Bentley Communities, {when}, for "
            f"{meta.get('product', 'unknown product')} {ver}. "
            f"Community answer, not vendor-published documentation.\n\n{url}")


def yq(value) -> str:
    """Quote a scalar so it survives YAML frontmatter.

    Found by the first real capture: a Bentley thread titled
    `C# ORD Alignment: how to change properties like level, color lineType etc`
    produced `title: C# ORD Alignment: how to change …`, which YAML reads as two
    mapping values and rejects. Forum titles routinely carry `:` and `#`, so this is
    the common case rather than an edge one.
    """
    s = "" if value is None else str(value)
    if not s:
        return ""
    if re.search(r"""[:#\[\]{},&*!|>'"%@`]""", s) or s != s.strip():
        return "'" + s.replace("'", "''") + "'"
    return s


def slugify(title: str, date: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "untitled").lower()).strip("-")[:60]
    return f"{s}-{date}"


# ---------------------------------------------------------------------------
# HTML capture (best effort, always lands as draft)
# ---------------------------------------------------------------------------

class _Text(HTMLParser):
    """Strip tags, keep block structure. Deliberately dumb.

    A saved forum page is nav, sidebars, cookie banners and the answer. No parser
    reliably separates those without site-specific rules, so this extracts text and
    the result is marked `status: draft` — a human decides what the answer was.
    """
    SKIP = {"script", "style", "nav", "header", "footer", "svg", "noscript"}
    BLOCK = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "section"}

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0
        self.title = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()
        if self._skip:
            return
        t = data.strip()
        if t:
            self.parts.append(t + " ")

    def text(self) -> str:
        out = "".join(self.parts)
        out = re.sub(r"[ \t]+", " ", out)
        return re.sub(r"\n{3,}", "\n\n", out).strip()


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def split_frontmatter(text: str):
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    raw = text[3:end].strip("\n")
    body = text[end + 4:]
    if yaml is None:
        raise SystemExit("PyYAML is required to validate — pip install pyyaml")
    try:
        return yaml.safe_load(raw) or {}, body
    except yaml.YAMLError as e:
        raise ValueError(f"frontmatter is not valid YAML: {e}") from e


# ---------------------------------------------------------------------------
# Validation — every field is a claim, so check it
# ---------------------------------------------------------------------------

def validate_note(path: pathlib.Path) -> list[tuple[str, str]]:
    out = []
    try:
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return [(FAIL, str(e))]
    if meta is None:
        return [(FAIL, "no YAML frontmatter — the provenance lives there")]

    for f in REQUIRED_FIELDS:
        if not str(meta.get(f) or "").strip():
            out.append((FAIL, f"missing required field `{f}`"))

    auth = str(meta.get("authority") or "")
    if auth and auth not in AUTHORITY:
        out.append((FAIL, f"authority `{auth}` is not one of: {', '.join(sorted(AUTHORITY))}"))

    role = str(meta.get("responder_role") or "")
    if auth.startswith("vendor-") and auth != "vendor-manual":
        if not role:
            out.append((FAIL, "`responder_role` is REQUIRED when authority is vendor-forum "
                              "or vendor-support — it is the filter that makes this corpus good"))
        elif role in WEAK_ROLES:
            out.append((FAIL, f"authority `{auth}` claims a vendor source but responder_role "
                              f"is `{role}` — use authority: community, or correct the role"))
    if role and role not in RESPONDER_ROLE:
        out.append((FAIL, f"responder_role `{role}` is not one of: {', '.join(sorted(RESPONDER_ROLE))}"))

    st = str(meta.get("status") or "")
    if st and st not in STATUS:
        out.append((FAIL, f"status `{st}` is not one of: {', '.join(sorted(STATUS))}"))
    if st == "verified" and not (meta.get("verified_by") and meta.get("verified_date")):
        out.append((FAIL, "status: verified requires `verified_by` and `verified_date` — "
                          "'verified' with no verifier is a claim nobody made"))

    for f in ("post_date", "retrieved", "verified_date"):
        v = meta.get(f)
        if v and not DATE_RE.match(str(v)):
            out.append((FAIL, f"`{f}` must be ISO YYYY-MM-DD, got {v!r}"))

    url = str(meta.get("source_url") or "")
    if auth != "internal":
        if not url:
            out.append((FAIL, "`source_url` is required for anything not written by us — "
                              "for web content the URL IS the citation locator"))
        elif not URL_RE.match(url):
            out.append((FAIL, f"`source_url` does not look like a URL: {url!r}"))

    ver = str(meta.get("version") or "").strip().lower()
    if ver in ("unknown", "unspecified"):
        out.append((WARN, "version is unknown — version-mismatched vendor advice is "
                          "confidently specific and wrong, the worst combination"))

    missing = [s for s in REQUIRED_SECTIONS if not re.search(rf"^##\s+{s}\b", body, re.M)]
    if missing:
        out.append((FAIL, f"missing section(s): {', '.join(missing)}"))

    m = re.search(r"^##\s+Resolution\b(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    if m:
        res = m.group(1).strip()
        if "<" in res and ">" in res and len(res) < 200:
            out.append((FAIL, "Resolution still contains the template placeholder"))
        elif len(res) < 120:
            out.append((WARN, f"Resolution is {len(res)} chars — a stub, not a fix"))
        hits = [q for q in QUESTION_MARKERS if q in res.lower()]
        if hits:
            out.append((FAIL,
                        f"Resolution reads like a QUESTION, not an answer "
                        f"({', '.join(repr(h) for h in hits[:3])}). A captured thread often "
                        "contains only the asker's post — and the code in a question is the "
                        "code that FAILED. Move it to ## Captured source and write the fix."))
        if "```" in res and str(meta.get("version") or "").lower() in ("", "unknown"):
            out.append((WARN, "Resolution contains code but no version — API surfaces move "
                              "between releases, and code for the wrong release fails in ways "
                              "that look like the user's mistake"))

    if meta.get("promoted_to_itglue") and st != "superseded":
        out.append((WARN, "promoted to IT Glue but still status != superseded — "
                          "two live copies of one answer will compete for rank"))

    if not out:
        out.append((OK, f"{auth} · {role or 'n/a'} · {meta.get('product')} {meta.get('version')} · {st}"))
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def today() -> str:
    return datetime.date.today().isoformat()


def cmd_new(args) -> int:
    meta = {"responder_role": "bentley_admin", "product": args.product,
            "version": args.version, "post_date": args.post_date or today(),
            "retrieved": today(), "source_url": args.url or ""}
    text = TEMPLATE.format(
        title=args.title, title_q=yq(args.title), collection_q=yq(args.collection),
        authority_opts=" | ".join(sorted(AUTHORITY)),
        url_q=yq(args.url), post_date_q=yq(args.post_date), retrieved_q=yq(today()),
        product_q=yq(args.product), version_q=yq(args.version),
        source_line=source_line(meta))
    return _write(args, text, args.title, args.post_date or today())


def cmd_from_text(args) -> int:
    raw = pathlib.Path(args.file).read_text(encoding="utf-8", errors="ignore").strip()
    meta = {"responder_role": args.responder_role, "product": args.product,
            "version": args.version, "post_date": args.post_date or today(),
            "retrieved": today(), "source_url": args.url or ""}
    text = TEMPLATE.format(
        title=args.title, title_q=yq(args.title), collection_q=yq(args.collection),
        authority_opts=" | ".join(sorted(AUTHORITY)),
        url_q=yq(args.url), post_date_q=yq(args.post_date), retrieved_q=yq(today()),
        product_q=yq(args.product), version_q=yq(args.version),
        source_line=source_line(meta))
    text = text.replace("<the steps. Keep the vendor's wording where it is precise; do not paraphrase a\nsetting name>", raw)
    return _write(args, text, args.title, args.post_date or today())


def cmd_from_html(args) -> int:
    p = _Text()
    p.feed(pathlib.Path(args.file).read_text(encoding="utf-8", errors="ignore"))
    title = args.title or p.title or pathlib.Path(args.file).stem
    meta = {"responder_role": args.responder_role, "product": args.product,
            "version": args.version, "post_date": args.post_date or "",
            "retrieved": today(), "source_url": args.url or ""}
    text = TEMPLATE.format(
        title=title, title_q=yq(title), collection_q=yq(args.collection),
        authority_opts=" | ".join(sorted(AUTHORITY)),
        url_q=yq(args.url), post_date_q=yq(args.post_date), retrieved_q=yq(today()),
        product_q=yq(args.product), version_q=yq(args.version),
        source_line=source_line(meta))
    text += ("\n\n<!-- ------------------------------------------------------------------\n"
             "EXTRACTED TEXT — review and move the real answer into ## Resolution above,\n"
             "then delete this block. A saved page is nav, sidebars and cookie banners as\n"
             "well as the answer; no parser separates those reliably, which is why this\n"
             "lands as status: draft.\n"
             "------------------------------------------------------------------ -->\n\n")
    text += p.text()[:20000]
    return _write(args, text, title, args.post_date or today())


CLIP_NOISE = (
    re.compile(r"^>\s*\[!\w+\].*$", re.M),        # > [!success] Success
    re.compile(r"^>\s*$", re.M),                   # its empty continuation line
    re.compile(r"^(Question|Answer|Reply|Replies|Suggested Answer)\s*$", re.M | re.I),
)


def clean_clip(text: str) -> tuple[str, str | None]:
    """Strip Obsidian Web Clipper furniture, return (body, title_if_found).

    What a real capture contains besides the content, measured on a Bentley Civil
    Programming Forum clip: an empty `> [!success] Success` callout, the bare label
    `Question`, and horizontal rules where the page had section dividers. None of it
    is content, all of it embeds.
    """
    title = None
    m = re.search(r"^#{1,6}\s+(.*)$", text, re.M)
    if m:
        title = m.group(1).strip()
    for rx in CLIP_NOISE:
        text = rx.sub("", text)
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.M)       # horizontal rules
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, title


def cmd_from_clip(args) -> int:
    """Wrap an Obsidian Web Clipper capture — WITHOUT filling in the Resolution.

    ⛔ The Resolution is left as the placeholder ON PURPOSE, so the note fails
    validation until a human writes the fix. A clipped thread frequently contains only
    the question, and auto-filling Resolution from a clip is exactly how the asker's
    failed code becomes the corpus's documented answer.
    """
    raw = pathlib.Path(args.file).read_text(encoding="utf-8", errors="ignore")
    cleaned, found_title = clean_clip(raw)
    title = args.title or found_title or pathlib.Path(args.file).stem
    meta = {"responder_role": args.responder_role, "product": args.product,
            "version": args.version, "post_date": args.post_date or "",
            "retrieved": today(), "source_url": args.url or ""}
    text = TEMPLATE.format(
        title=title, title_q=yq(title), collection_q=yq(args.collection),
        authority_opts=" | ".join(sorted(AUTHORITY)),
        url_q=yq(args.url), post_date_q=yq(args.post_date), retrieved_q=yq(today()),
        product_q=yq(args.product), version_q=yq(args.version),
        source_line=source_line(meta))
    text += (
        "\n## Captured source\n\n"
        "<!-- Verbatim from the clip. The ANSWER may not be in here at all — clippers\n"
        "     routinely capture only the first post. Read it, find the actual fix, and\n"
        "     write it into ## Resolution above in your own words with the vendor's\n"
        "     exact setting names preserved. Do not move this block up wholesale. -->\n\n"
        + cleaned + "\n")
    n_code = cleaned.count("```") // 2
    rc = _write(args, text, title, args.post_date or today())
    if rc == 0:
        print(f"   captured {len(cleaned)} chars"
              + (f", {n_code} code block(s)" if n_code else ""))
        print("   ⚠️  Resolution is intentionally EMPTY — validate will fail until you "
              "write the fix")
    return rc


def _write(args, text: str, title: str, date: str) -> int:
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slugify(title, date)}.md"
    if path.exists() and not args.force:
        print(f"⛔ {path} exists — pass --force to overwrite", file=sys.stderr)
        return 1
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")
    print("   next: fill in the sections, then "
          f"`python3 kb_capture.py validate {out_dir}`")
    return 0


def cmd_validate(args) -> int:
    folder = pathlib.Path(args.folder)
    notes = sorted(p for p in folder.glob("*.md") if p.name.lower() != "readme.md")
    if not notes:
        print(f"no .md notes in {folder}")
        return 0
    fails = warns = 0
    for n in notes:
        findings = validate_note(n)
        shown = [f for f in findings if not (args.quiet and f[0] == OK)]
        if shown:
            print(f"\n{n.name}")
            for level, detail in shown:
                mark = {FAIL: "⛔ FAIL", WARN: "⚠️  WARN", OK: "   ok "}[level]
                print(f"  {mark}  {detail}")
        fails += sum(1 for f in findings if f[0] == FAIL)
        warns += sum(1 for f in findings if f[0] == WARN)
    print(f"\n{'=' * 72}")
    print(f"  {len(notes)} note(s) · {fails} FAIL · {warns} WARN")
    if fails:
        print("  ⛔ Provenance is the product here. A note that cannot say where it came "
              "from does not belong in the corpus.")
        return 1
    if warns and args.strict:
        return 1
    print("  ✅ Every note carries its provenance.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, need_title=True):
        p.add_argument("--title", required=need_title)
        p.add_argument("--url", default="")
        p.add_argument("--product", default="ProjectWise")
        p.add_argument("--version", default="unknown")
        p.add_argument("--post-date", dest="post_date", default="")
        p.add_argument("--responder-role", dest="responder_role", default="bentley_admin")
        p.add_argument("--collection", default="bentley-kb")
        p.add_argument("--out", default="dropzone/bentley-kb")
        p.add_argument("--force", action="store_true")

    common(sub.add_parser("new", help="blank template to fill in by hand"))
    p = sub.add_parser("from-text", help="wrap already-captured text")
    p.add_argument("file"); common(p)
    p = sub.add_parser("from-html", help="best-effort extract from a saved page")
    p.add_argument("file"); common(p, need_title=False)
    p = sub.add_parser("from-clip", help="wrap an Obsidian Web Clipper capture (.md)")
    p.add_argument("file"); common(p, need_title=False)

    v = sub.add_parser("validate", help="check every note in a collection")
    v.add_argument("folder")
    v.add_argument("--strict", action="store_true", help="warnings fail too")
    v.add_argument("--quiet", action="store_true")

    args = ap.parse_args(argv)
    return {"new": cmd_new, "from-text": cmd_from_text, "from-clip": cmd_from_clip,
            "from-html": cmd_from_html, "validate": cmd_validate}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
