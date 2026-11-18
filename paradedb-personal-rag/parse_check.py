"""
parse_check.py — is this Docling JSON safe to ingest, as TEXT?

    python parse_check.py dropzone/bentley-manuals/FDOT\\ ORD\\ Install/*.json
    python parse_check.py dropzone/bentley-manuals --verbose

WHAT IT ANSWERS
---------------
heading_check.py asks whether the heading filters help. This asks whether the
WORDS are trustworthy. A PDF can look perfect on screen while its text layer
(or Docling's assembly of that layer) is garbage: letters in drawing order,
broken ToUnicode maps, Force-OCR leftovers.

Measured on the 2026-08-27 Force-OCR parse of FDOT Connect 2024 Installation
Guide.json — installer screenshots came back readable ("Next >", "Finish")
and the body still contained:

    p.2   'Nod  g      s  :ads Designer.'     OpenRoads, glyph order
    p.4   ', u      b    h This is a critical step'
    p.9   's s o  sdə  so  e'                 license-agreement paragraph
    p.12  '... DGN file to Iau s     c     es y d tly.'
    p.5   'liabilitv or responsibilitv'       y written as v

heading_check.py's ingest-QA regexes miss all of that: the tokens are short,
they are not 19-character space-collapses, and they are not section symbols.
Force-OCR is not a clean bill of health — run this on the JSON you actually
have, including the OCR'd one.

WHAT IT DOES NOT DO
-------------------
No database. No embedder. It never writes anything. It does not re-parse the
PDF. It scores the Docling export you already produced.

WHY ITEM-LEVEL, NOT CHUNK-LEVEL
-------------------------------
The FDOT guide is 13 pages of mostly-good prose with four poisoned sentences.
Averaging those into a page or a chunk dilutes them under a dictionary-hit
threshold (the rest of p.2 is fine English). The unit of damage is a Docling
text item. ingest.py will index those items, so that is what we flag.

READING THE OUTPUT
------------------
  glyph-gaps    4+ consecutive spaces wrapping 1–3 letter fragments. CAD/PDF
                drawing order, not English. Justification uses 2-spaces
                between real words and does not match.
  fragments     an item that is mostly 1-letter tokens and almost no real
                words. Citations like '26 U.S.C. 5845' are stripped first.
  encoding      a word that is a dictionary word if a typical font-cmap
                substitution is undone (v→y: liabilitv; F→&: &inish).
  odd-letter    IPA / non-Latin letters inside a token (ə in sdə).
  display-tracking  spaced uppercase cover lettering; review, not corruption.
  chart-axis-spacing  widely spaced month labels; review the figure itself.
  dunder        a "code" item with a damaged Python dunder (def init__(,
                super().__init_(), if _name_ II __main__":). Copied code
                will not run. ingest.py repairs the unambiguous shapes, but
                a book full of them needs a better parse first.
  code-cipher   a "code" item that is glyph IDs, not text: U+FFFD runs
                (replacement-chars) or capitals that become Python words when
                shifted back one letter (shifted-glyphs: GSPN ULJOUFS = from
                tkinter). Broken ToUnicode map on a code font; re-parse with
                OCR.

  FAIL for scramble/encoding damage. Benign display or chart-axis patterns are
  WARN so a human still reviews them before ingestion.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import re
import sys

# ---------------------------------------------------------------------------
# Word list
# ---------------------------------------------------------------------------

_DICT_PATHS = (
    "/usr/share/dict/words",
    "/usr/share/dict/american-english",
    "/usr/dict/words",
)

# Bundled dictionary. Ships with the script so the encoding check works on any
# host, even one with no /usr/share/dict/words (the rag-reviewer container).
# It is SCOWL size 70 (~130k words; build_parse_check_words.py rebuilds it,
# parse_check_words.COPYRIGHT is its license). When it is present it is the
# whole dictionary: the Mac and the container must score a document the same
# way, so no system dictionary is mixed in. An earlier 491-word bundled list
# fell through to /usr/share/dict/words on the Mac but stood alone in the
# container, so 'specifv' was flagged on one host and passed on the other.
_BUNDLED_DICT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "parse_check_words.txt")
# Below this the bundled file is truncated or the old hand-made list.
MIN_BUNDLED_WORDS = 50_000

FUNCTION = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "be", "by", "as", "at", "it", "if", "not", "no", "do", "from", "with",
    "this", "that", "these", "those", "than", "then", "so", "but", "we", "you",
    "your", "our", "their", "was", "were", "will", "can", "may", "use", "used",
    "using", "also", "into", "its", "has", "have", "had", "been", "all", "any",
    "per", "via", "etc", "up", "out", "off",
}

# Single letters that are content in this corpus, not scramble: coordinates,
# existing/proposed codes, the article/pronoun already in FUNCTION.
COORD_LETTERS = {"a", "i", "x", "y", "z"}

# Justification and sentence gaps. CAD glyph-order scramble uses runs of 4+.
WIDE_SPACE = re.compile(r"\s{4,}")

# Keep 'U.S.C.' / 'U.S.' as one token so legal citations do not look like
# a pile of 1-letter fragments.
ABBREV = re.compile(r"\b[A-Za-z](?:\.[A-Za-z])+\.?", re.ASCII)
TOKEN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?", re.ASCII)

# CamelCase, ALLCAPS acronyms, and things with digits: OpenRoads, FDOT, C3D.
IDENT = re.compile(
    r"^[A-Z]{2,8}$|^[A-Za-z]+[A-Z][A-Za-z0-9]+$|^[A-Za-z]*[0-9][A-Za-z0-9]*$"
)

# Ampersand standing in for F (Finish → &inish, FDOT → &DOd). HTML entities
# are '&amp;' / '&lt;' and are excluded.
AMP_AS_F = re.compile(r"&[A-Za-z]{2,}")
HTML_ENTITY = re.compile(r"&(?:amp|lt|gt|quot|nbsp|apos|#\d+|#x[0-9A-Fa-f]+);", re.I)
# Docling writes '_' inside URLs as ' _ ', so the Copilot Developer Guide's
# '?id=kb _ article&sys _ id=${SysId}' (PDF p. 738) and '&sysparm _ article='
# (p. 739) are still query parameters.
URL_QUERY_PARAMETER = re.compile(r"&[A-Za-z][A-Za-z0-9_]*(?: _ [A-Za-z0-9_]+)*=")
# '&' joining two URLs: 'https://m365.cloud.microsoft &https://copilot...'
# (Copilot Developer Guide, PDF p. 652).
AMP_BEFORE_URL = re.compile(r"&(?:https?://|www\.)", re.I)
# Visually verified in California Employer Guide, PDF p. 79: Revenue and
# Taxation Code. MITRE ATT&CK is the framework name (secpro2023v8, PDF pp. 23,
# 458, next to https://attack.mitre.org). Do not exempt arbitrary '&word'
# text (e.g. damaged '&inish').
VERIFIED_AMP_ABBREVIATION = re.compile(r"\bR&TC\b|\bMovies &TV\b|\bATT&CK\b")
# Measured 2026-11-06 on troubleshoot-windows-client (pages 1-1500).json, where
# these legitimate Windows strings were flagged in three repair rounds in a
# row and five correct lines were finally excluded to get past the check:
#   'PCI\VEN_1002&DEV_9480'  PnP hardware IDs (Docling also writes 'VEN _ 1002&DEV _ 9480')
#   '= 3 &&Maximized'        Visual FoxPro '&&' comment, printed so in the PDF (p. 110)
#   'Movies &TV'             the app name, in a KB title (p. 1467)
HARDWARE_ID_AMP = re.compile(r"&(?:DEV|SUBSYS|REV|PID|VID|MI|CC|VEN|PROD|COL)\s*_")
MONTH_ABBREVS = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                 "Sep", "Oct", "Nov", "Dec"}
# Visually verified against PMBOK 7 PDF p. 1. Keep this explicit: a dictionary
# match would vary with the host and could excuse unrelated OCR fragments.
VERIFIED_DISPLAY_WORDS = {("GLOBAL", "STANDARD")}

CLI_SWITCH_OR_DRIVE = re.compile(r"^(?:[/-][A-Za-z]|[A-Za-z]:)[,;]?$")
# A hex dump row ('71 7e 26 2f 8e 03 d2 01', windows-client p. 918): the
# letters a-f are digits here, not 1-letter fragments.
HEX_BYTES = re.compile(r"(?:[0-9A-Fa-f]{2}[\s,:-]+)+[0-9A-Fa-f]{2}")


def _is_short_junk(tok: str) -> bool:
    """1–3 letter token that is not a function word, coordinate, or acronym."""
    if not tok or not tok.isalpha():
        return False
    if not (1 <= len(tok) <= 3):
        return False
    low = tok.lower()
    if low in FUNCTION or low in COORD_LETTERS:
        return False
    if re.match(r"^[A-Z]{2,3}$", tok):
        return False
    return True


def _letter_core(tok: str) -> str:
    return "".join(c for c in tok if c.isalpha())


def glyph_gap_scramble(text: str) -> bool:
    """True when a 4+ space run sits between two short junk tokens.

    CAD glyph-order scramble looks like 'g      s' or 's     c'. Justified
    prose uses the same 4-space run as a sentence gap ('.    The') — both
    sides of that gap are real words, so it must not match.
    """
    for m in WIDE_SPACE.finditer(text):
        left = TOKEN.findall(text[:m.start()])
        right = TOKEN.findall(text[m.end():])
        if left and right and _is_short_junk(left[-1]) and _is_short_junk(right[0]):
            return True
    return False


def is_damage_letter(ch: str) -> bool:
    """Font-cmap / OCR junk letters, not real orthography.

    Latin-1 and the verified names Şafak and Špundak are legitimate text, not
    damage by themselves. IPA (ə) and CJK (日 standing in for a backslash) do
    not belong in this corpus.
    """
    if not ch.isalpha():
        return False
    o = ord(ch)
    if o <= 0x7F or 0xC0 <= o <= 0xFF or ch in {"Ş", "Š"}:
        return False
    return True

# Furniture labels ingest.py already drops. Flagging a running header does
# not predict what the index will contain.
SKIP_LABELS = {"page_header", "page_footer", "picture", "furniture"}

# Microsoft Learn web-page chrome printed to PDF. The site draws its button
# and badge icons with an icon font; the PDF text layer maps those glyphs to
# stray letters. Measured 2026-11-06 on troubleshoot-mem-intune.json: 48 of
# 80 findings were exactly these items —
#     'ﾉ Expand table'  (31, the table toolbar button; 28 were section_header,
#                        so every table's section became "Expand table")
#     'ｃ HOW-TO GUIDE' (14), 'ｅ OVERVIEW', 'ｉ REFERENCE'  (landing-page badges)
# They are UI labels, not content: skip them in the check and in ingest.py.
# Only an exact whole-item match WITH a leading icon glyph counts, so a real
# "Overview" heading or a bare "REFERENCE" line is untouched. Extend the label
# set only with evidence from a parsed document.
WEB_CHROME_LABELS = {
    "expand table",
    "how-to guide", "overview", "reference", "concept", "quickstart",
    "tutorial", "training", "video", "download", "architecture", "deploy",
    "get started", "what's new", "whats new", "learn",
    # Learn alert boxes. 'Ｕ Caution' was kept/verified twice in the 1-1500
    # part of troubleshoot-windows-client and reappeared in the 1501-2299 part.
    "caution", "warning", "important", "note", "tip",
}
_WEB_CHROME = re.compile(r"^(?P<icon>\S)\s*(?P<label>[A-Za-z][A-Za-z' -]*[A-Za-z])$")


def is_web_chrome(text: str) -> bool:
    """True for a Microsoft Learn button/badge label with its icon glyph."""
    m = _WEB_CHROME.match((text or "").strip())
    if not m or not is_damage_letter(m.group("icon")):
        return False
    return re.sub(r"\s+", " ", m.group("label")).lower() in WEB_CHROME_LABELS


def _read_words(path: str, *, one_per_line: bool) -> set[str]:
    words = set()
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                for tok in (line.strip(),) if one_per_line else line.split():
                    w = tok.lower()
                    if w.isalpha() and len(w) >= 2:
                        words.add(w)
    except OSError:
        pass
    return words


def load_words(bundled: str = _BUNDLED_DICT) -> set[str]:
    """Return the bundled dictionary, falling back to a system dictionary.

    A full bundled list (>= MIN_BUNDLED_WORDS) is returned on its own, so
    every host gets the same answer. A missing or suspiciously small bundled
    list is reported on stderr and topped up from a system dictionary when
    one exists; with neither, the encoding check is inert and that is said
    out loud so a silent PASS is impossible.
    """
    words = set()
    if os.path.isfile(bundled):
        words = _read_words(bundled, one_per_line=False)
        if len(words) >= MIN_BUNDLED_WORDS:
            return words
        print(
            "WARNING: bundled dictionary %s holds only %d words (expected "
            ">= %d). The encoding check (v->y) will miss damage and results "
            "will differ between hosts. Rebuild it with "
            "build_parse_check_words.py." % (bundled, len(words),
                                             MIN_BUNDLED_WORDS),
            file=sys.stderr,
        )
    else:
        print("WARNING: bundled dictionary %s is missing; results will "
              "depend on this host's system dictionary." % bundled,
              file=sys.stderr)
    for path in _DICT_PATHS:
        if os.path.isfile(path):
            words |= _read_words(path, one_per_line=True)
            if len(words) > 1000:
                return words
    if not words:
        warn = (
            "WARNING: no dictionary found (bundled %s missing and no system "
            "dictionary in %s). The encoding check (v->y, &->F) is now "
            "inert. Copy parse_check_words.txt next to this script or "
            "install a system dictionary."
        ) % (bundled, ", ".join(_DICT_PATHS))
        print(warn, file=sys.stderr)
    return words


WORDS = load_words()


def is_tracked_display_text(text: str) -> bool:
    """Recognize visually verified spaced cover words, not arbitrary junk.

    A display title such as "G L O B A L  S T A N D A R D" is searchable text,
    but should lead to human review rather than a hard corruption failure.
    The explicit allowlist leaves the FDOT single-letter scramble on FAIL and
    behaves identically on hosts with and without a system dictionary.
    """
    groups = re.split(r"\s{2,}", text.strip())
    if not groups:
        return False
    words = []
    for group in groups:
        letters = group.split(" ")
        if len(letters) < 4 or any(len(letter) != 1 or not letter.isupper()
                                   for letter in letters):
            return False
        words.append("".join(letters))
    return tuple(words) in VERIFIED_DISPLAY_WORDS


def is_chart_month_axis(text: str) -> bool:
    """A row of month labels can carry wide layout spaces without scrambled text."""
    labels = text.split()
    return bool(WIDE_SPACE.search(text)) and len(labels) >= 3 \
        and all(label in MONTH_ABBREVS for label in labels)


def item_page(item: dict):
    prov = item.get("prov") or []
    return prov[0].get("page_no") if prov else None


def tokens(text: str) -> list[str]:
    """Alphabetic tokens, with dotted abbreviations held together."""
    stripped = ABBREV.sub(" ", text)
    return TOKEN.findall(stripped)


def is_ident(tok: str) -> bool:
    return bool(IDENT.match(tok))


def odd_letters(text: str) -> list[str]:
    return [ch for ch in text if is_damage_letter(ch)]


def encoding_hits(text: str, toks: list[str]) -> list[str]:
    """Substitutions this corpus has actually produced, generalised.

    v→y  was liabilitv / responsibilitv on the Force-OCR FDOT install guide.
    &→F  is the same PDF's text layer (Finish → &inish) when it survives.
    """
    hits = []
    for tok in toks:
        low = tok.lower()
        if len(low) >= 6 and low.endswith("v") and low not in WORDS:
            fixed = low[:-1] + "y"
            if fixed in WORDS:
                hits.append(tok)
    for m in AMP_AS_F.finditer(text):
        raw = m.group(0)
        if HTML_ENTITY.match(raw + ";") or HTML_ENTITY.match(raw):
            continue
        # A URL query parameter like "&sc_lang_temp=en" is not "&inish".
        if URL_QUERY_PARAMETER.match(text[m.start():]):
            continue
        if AMP_BEFORE_URL.match(text[m.start():]):
            continue
        # PnP hardware ID ('VEN_8086&DEV_7A98') or a '&&' code comment/operator.
        if HARDWARE_ID_AMP.match(text[m.start():]):
            continue
        if m.start() > 0 and text[m.start() - 1] == "&":
            continue
        if any(a.start() <= m.start() and m.end() <= a.end()
               for a in VERIFIED_AMP_ABBREVIATION.finditer(text)):
            continue
        hits.append(raw)
    return hits


# ---------------------------------------------------------------------------
# Code-text repair (shared with ingest.py)
# ---------------------------------------------------------------------------
# Measured 2026-10-16 on the approved IN254 "2018_Tkinter GUI Application
# Development.json": 73 of 303 Docling "code" items carry damaged Python
# dunders (107 damaged occurrences against 49 correct `__init__`), e.g.
#
#     def init__(self): super().__init_()
#     if _name_ II __main___": ( ) dd∀ = dde app.mainloop()
#
# Copied into a sandbox, that code does not run. The rules below repair only
# shapes that have no legitimate Python reading; a correct `__init__` is
# rewritten to itself, and `self._name`, `__private`, `init_buttons` and an
# Enum's `_name_` are left alone. inspect_item() flags the same matches so a
# code-heavy book FAILs review instead of passing with broken samples.

# Dunders the damage has actually hit. Extend only with evidence.
DUNDER_WORDS = ("init", "name", "main")
_DW = "|".join(DUNDER_WORDS)

# `def init__(`, `def _init_(`  ->  `def __init__(`
_DEF_INIT = re.compile(r"\bdef(\s+)_{0,3}init_{1,3}(?=\s*\()")
# `super().init_(`, `super( )._init__(`  ->  `super().__init__(`. Only after a
# call's `)`, so a method a class really names `init_` is not touched.
_CALL_INIT = re.compile(r"(?<=\))(\s*\.\s*)_{0,3}init_{1,3}(?=\s*\()")
# The main guard, every variant seen in the book:
#   if _name_ __ == "__main___":   if _name_ II __main__":   if __name_ "__main___":
# II is OCR for ==. Both words must carry an underscore, so a real
# `if name == "main":` comparison never matches.
_MAIN_GUARD = re.compile(
    r"\bif\s+(?P<name>_{0,3}name_{0,3}(?:\s+_{1,3})?)\s*(?:==|II)?\s*(?:==|II)?"
    r"\s*(?P<q1>[\"']?)(?P<main>_{0,3}main_{0,3})(?P<q2>[\"']?)\s*:")
_GOOD_GUARD = re.compile(r"if\s+__name__\s*==\s*([\"'])__main__\1\s*:")
# Bare tokens with lopsided underscores: `__init_`, `_init__`, `__main___`.
# `_x_` is only repaired for init/main: `_name_` is a real Enum attribute.
_BARE_DUNDER = re.compile(rf"(?<![A-Za-z0-9_])(_{{0,3}})({_DW})(_{{1,3}})(?![A-Za-z0-9_])")
# The book's `app = App()` line printed rotated 180°, recovered by Docling as
# this literal string 49 times in the same book's code items.
ROTATED_LITERALS = {
    "( ) dd∀ = dde": "app = App()",
    "( ) ddA = dde": "app = App()",
}

# Characters that are always a mis-mapped glyph in this corpus. Measured on
# the approved pve-admin-guide.json: all 70 U+02C6 MODIFIER LETTER CIRCUMFLEX
# are regex/keyboard carets ('[ˆ,]+', '(?ˆi:', 'ˆO', '\\ˆ?[a-z]'), 0 real '^',
# and only 6 of the 70 are in code items — 21 are section headers, which
# become citation labels. No other approved document contains U+02C6. So
# this map applies to every label, not just code.
GLYPH_MAP = {"ˆ": "^"}


def _bare_fix(m: re.Match) -> str | None:
    lead, word, trail = m.groups()
    if (len(lead), len(trail)) == (2, 2):
        return None                         # already correct
    if lead == "":
        if len(trail) < 2:
            return None                     # `init_`, `name_`: ordinary names
    elif len(lead) == 1 and len(trail) == 1 and word == "name":
        return None                         # Enum `_name_`
    return f"__{word}__"


def _guard_fix(m: re.Match) -> str | None:
    text = m.group(0)
    if _GOOD_GUARD.fullmatch(text.strip()):
        return None
    if "_" not in m.group("name") or "_" not in m.group("main"):
        return None
    q = m.group("q1") or m.group("q2") or '"'
    return f"if __name__ == {q}__main__{q}:"


def _code_rules():
    """(pattern, fix) pairs; fix returns the replacement or None to keep."""
    return (
        (_MAIN_GUARD, _guard_fix),
        (_DEF_INIT, lambda m: f"def{m.group(1)}__init__"),
        (_CALL_INIT, lambda m: f"{m.group(1)}__init__"),
        (_BARE_DUNDER, _bare_fix),
    )


def _apply(rule, fix, text: str, hits: list[str]) -> str:
    def sub(m):
        new = fix(m)
        if new is None or new == m.group(0):
            return m.group(0)
        hits.append(m.group(0).strip())
        return new
    return rule.sub(sub, text)


def repair_code_text(text: str) -> tuple[str, list[str]]:
    """Repair damaged dunders and known rotated literals in a code item.

    Returns (repaired text, the damaged fragments that were replaced).
    """
    hits: list[str] = []
    for bad, good in ROTATED_LITERALS.items():
        if bad in text:
            hits.extend([bad] * text.count(bad))
            text = text.replace(bad, good)
    for rule, fix in _code_rules():
        text = _apply(rule, fix, text, hits)
    return text, hits


def dunder_damage(text: str) -> list[str]:
    """Damaged dunders in code text (rotated literals excluded)."""
    hits: list[str] = []
    for rule, fix in _code_rules():
        text = _apply(rule, fix, text, hits)
    return hits


# Code set in a subset font whose ToUnicode map is broken comes out as glyph
# IDs, not characters. Docling 2.140 on the IN254 Tkinter book (FreeMono):
#
#     GSPN ULJOUFS JNQPSU � SPPU � 5L��    = from tkinter import * root = Tk()
#
# Lowercase letters land one code point up and uppercased; digits, symbols and
# capitals mostly become U+FFFD. Code items are not lexicon-scored, so this
# passed as PASS. Undo the shift and see whether the words appear.
CODE_WORDS = {
    "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "false", "finally", "for", "from",
    "global", "if", "import", "in", "is", "lambda", "none", "nonlocal", "not",
    "or", "pass", "print", "raise", "return", "self", "super", "true", "try",
    "while", "with", "yield", "tk", "tkinter", "root", "init", "main", "name",
}
_REPLACEMENT = "\ufffd"


def _unshift(tok: str) -> str:
    return "".join(chr(ord(c) - 1) if "B" <= c <= "Z" else c for c in tok).lower()


def _known(tok: str) -> bool:
    low = tok.lower()
    return low in CODE_WORDS or (len(low) >= 3 and low in WORDS)


def code_cipher(text: str) -> list[str]:
    """Reasons a "code" item is glyph-ID cipher rather than code."""
    reasons = []
    visible = [c for c in text if not c.isspace()]
    bad = text.count(_REPLACEMENT)
    if bad >= 3 and bad / max(len(visible), 1) >= 0.05:
        reasons.append(f"replacement-chars:{bad}")
    words = [t for t in re.findall(r"[A-Za-z]+", text) if len(t) >= 2]
    caps = [t for t in words if t.isupper()]
    if len(caps) >= 3 and len(caps) / len(words) >= 0.5:
        before = sum(_known(t) for t in caps)
        after = sum(_known(_unshift(t)) for t in caps)
        if after >= 3 and after >= 2 * before + 2 and after / len(caps) >= 0.4:
            reasons.append(f"shifted-glyphs:{after}/{len(caps)}")
    return reasons


def repair_glyphs(text: str) -> str:
    for bad, good in GLYPH_MAP.items():
        text = text.replace(bad, good)
    return text


def repair_item_text(text: str, label: str) -> str:
    """What ingest.py indexes for a Docling item: glyph map for every label,
    plus the dunder/literal repairs for items Docling labels "code"."""
    text = repair_glyphs(text or "")
    if (label or "").lower() == "code":
        text, _ = repair_code_text(text)
    return text


class Finding:
    __slots__ = ("page", "label", "reasons", "snippet")

    def __init__(self, page, label, reasons: list[str], snippet: str):
        self.page = page
        self.label = label
        self.reasons = reasons
        self.snippet = snippet

    def as_dict(self) -> dict:
        return {
            "page": self.page,
            "label": self.label,
            "reasons": self.reasons,
            "snippet": self.snippet,
        }


def inspect_item(item: dict) -> Finding | None:
    label = (item.get("label") or "").lower()
    if label in SKIP_LABELS or item.get("content_layer") == "furniture":
        return None
    text = (item.get("text") or "").strip()
    if not text or is_web_chrome(text):
        return None

    toks = tokens(text)
    ws = text.split()
    # Command-line switches ('/s', '-f') and bare drive letters ('R:') are
    # one letter by design: 'Bcdboot D:\\windows /s R: /f ALL' (windows-client
    # p. 913) is a real command, not scramble.
    ones = [
        t for t in ws
        if len(_letter_core(t)) == 1
        and _letter_core(t).lower() not in COORD_LETTERS
        and not CLI_SWITCH_OR_DRIVE.match(t)
    ]
    long_orig = [t for t in toks if len(t) >= 4]
    dict_cand = [t.lower() for t in long_orig if not is_ident(t)]
    dict_n = len(dict_cand)

    reasons = []
    if glyph_gap_scramble(text):
        reasons.append("chart-axis-spacing" if is_chart_month_axis(text)
                       else "glyph-gaps")
    # Mostly 1-letter whitespace tokens and almost no real words.
    # Sheet lists ('S-001A, T-1') stay intact as whitespace tokens, so they
    # do not look like 's s o sdə so e'.
    if (
        len(ws) >= 5
        and not HEX_BYTES.fullmatch(text)
        and len(ones) >= 3
        and len(ones) / len(ws) >= 0.45
        and dict_n <= 2
    ):
        reasons.append("display-tracking" if is_tracked_display_text(text)
                       else "fragments")
    enc = encoding_hits(text, toks)
    if enc:
        reasons.append("encoding:" + ",".join(enc[:3]))
    odd = odd_letters(text)
    if odd:
        # Pure CJK 1-char items are usually mis-mapped bullets. Mixed-script
        # tokens (sdə, 日eng_data) are body text and will be indexed.
        mixed = any(
            is_damage_letter(ch) and any(c.isascii() and c.isalpha() for c in tok)
            for tok in ws
            for ch in tok
        )
        if mixed or len(text.strip()) > 3:
            reasons.append("odd-letter:" + "".join(dict.fromkeys(odd))[:6])

    if label == "code":
        damaged = dunder_damage(text)
        if damaged:
            reasons.append("dunder:" + ",".join(dict.fromkeys(damaged))[:60])
        reasons.extend("code-cipher:" + r for r in code_cipher(text))

    if not reasons:
        return None
    snippet = re.sub(r"\s+", " ", text)[:160]
    return Finding(item_page(item), label, reasons, snippet)


def check_file(path: pathlib.Path) -> tuple[dict, list[Finding]]:
    doc = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    if doc.get("schema_name") != "DoclingDocument":
        raise ValueError(
            f"{path.name} is not a Docling export (schema_name="
            f"{doc.get('schema_name')!r})"
        )
    findings = []
    n_items = 0
    for item in doc.get("texts") or []:
        label = (item.get("label") or "").lower()
        if label in SKIP_LABELS or item.get("content_layer") == "furniture":
            continue
        if not (item.get("text") or "").strip() or is_web_chrome(item.get("text")):
            continue
        n_items += 1
        hit = inspect_item(item)
        if hit:
            findings.append(hit)
    return {"items": n_items, "origin": (doc.get("origin") or {}).get("filename")}, findings


def assess_file(path: pathlib.Path, *, accept_finding=None) -> dict:
    """Return the machine-readable result for one Docling export."""
    path = pathlib.Path(path)
    meta, findings = check_file(path)
    if accept_finding is not None:
        findings = [f for f in findings if not accept_finding(f)]
    severe = [
        f for f in findings
        if any(r.split(":")[0] in {"glyph-gaps", "fragments", "encoding", "dunder",
                                     "code-cipher"}
               for r in f.reasons)
    ]
    odd_only = [f for f in findings if f not in severe]
    if severe or len(odd_only) >= 3:
        status = "FAIL"
    elif findings:
        status = "WARN"
    else:
        status = "PASS"

    by_signal: dict[str, int] = {}
    for finding in findings:
        for reason in finding.reasons:
            signal = reason.split(":", 1)[0]
            by_signal[signal] = by_signal.get(signal, 0) + 1

    return {
        "document": path.name,
        "origin": meta.get("origin"),
        "text_items_scored": meta["items"],
        "status": status,
        "by_signal": dict(sorted(by_signal.items())),
        "pages": sorted({f.page for f in findings if f.page is not None}),
        "findings": [f.as_dict() for f in findings],
    }


def report(path: pathlib.Path, verbose: bool) -> int:
    result = assess_file(path)
    findings = result["findings"]
    print(f"\n{'=' * 78}\n{path.name}\n{'=' * 78}")
    if result.get("origin"):
        print(f"  origin            : {result['origin']}")
    print(f"  text items scored : {result['text_items_scored']}")
    print(f"  flagged           : {len(findings)}")

    if result["by_signal"]:
        print("  by signal         : " + ", ".join(
            f"{k}={v}" for k, v in result["by_signal"].items()))
        print("  pages             : " + ", ".join(
            str(p) for p in result["pages"])
            or "(none)")

    show = findings if verbose else findings[:12]
    if show:
        print()
        for f in show:
            why = ", ".join(f["reasons"])
            print(f"  p.{f['page']} [{why}] {f['snippet']!r}")
        if not verbose and len(findings) > 12:
            print(f"  ... {len(findings) - 12} more (pass --verbose)")

    print()
    if result["status"] == "PASS":
        print("  VERDICT: PASS. No scramble / encoding damage in text items.")
        return 0
    if result["status"] == "FAIL":
        print("  VERDICT: FAIL. Do not ingest this parse.")
        print("           Force-OCR is not a clean bill of health — these items")
        print("           will be indexed verbatim. Re-parse the bad pages")
        print("           before ingest.py --documents-only.")
        return 1
    print("  VERDICT: WARN. Non-fatal text findings require human review.")
    print("           Verify the items above against the source before ingest.")
    return 0


def expand_args(patterns: list[str]) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for pattern in patterns:
        p = pathlib.Path(pattern)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.json")))
            continue
        # pathlib.Path.glob() rejects absolute patterns. glob.glob() supports
        # both absolute and relative arguments and preserves the old wildcard
        # behavior.
        matched = [pathlib.Path(match) for match in sorted(glob.glob(pattern))]
        out.extend(matched if matched else [p])
    # Sidecars from figure_index.py are not Docling exports.
    return [p for p in out if p.name != "manifest.yaml"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Score a Docling JSON export for text-layer damage (scramble, encoding, OCR leftovers) before ingest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("files", nargs="+",
                    help="Docling .json export(s), or a folder to walk")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="print every flagged item, not the first 12")
    ap.add_argument("--json", action="store_true",
                    help="emit one JSON object with structured results")
    args = ap.parse_args(argv)

    worst = 0
    seen = 0
    results = []
    errors = []
    for p in expand_args(args.files):
        if not p.exists():
            errors.append({"document": str(p), "error": "not found"})
            if not args.json:
                print(f"skip (not found): {p}", file=sys.stderr)
            continue
        if p.suffix.lower() != ".json":
            continue
        try:
            if args.json:
                result = assess_file(p)
                results.append(result)
                worst = max(worst, 1 if result["status"] == "FAIL" else 0)
            else:
                worst = max(worst, report(p, args.verbose))
            seen += 1
        except (ValueError, json.JSONDecodeError) as e:
            errors.append({"document": str(p), "error": str(e)})
            if not args.json:
                print(f"skip: {e}", file=sys.stderr)
    if args.json:
        print(json.dumps({"results": results, "errors": errors}, ensure_ascii=False))
    if not seen:
        if not args.json:
            print("Nothing checked.", file=sys.stderr)
        return 2
    if not args.json:
        print()
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
