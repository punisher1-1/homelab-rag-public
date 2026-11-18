#!/usr/bin/env python3
"""Rebuild parse_check_words.txt from a SCOWL release.

parse_check.py treats parse_check_words.txt as its whole dictionary, so the
encoding check (v->y: 'liabilitv', 'specifv') behaves the same on every host,
including the rag-reviewer container, which has no /usr/share/dict/words.

Source: SCOWL 2020.12.07 (Spell Checker Oriented Word Lists, Kevin Atkinson),
    https://downloads.sourceforge.net/project/wordlist/SCOWL/2020.12.07/scowl-2020.12.07.tar.gz
    sha256 5587667caa20c4891390c2d42dbb4d5c4c3f41bee77af1457ece3ba23fb859cc
License: see parse_check_words.COPYRIGHT (SCOWL's own Copyright file).

Selection: the words, upper (capitalised) and proper-names lists for
english, american, british, british_z and canadian spellings, size <= 70
(SCOWL's "large" spell-checker size). Entries are lowercased; anything that is
not plain ASCII letters, or is shorter than 2 letters, is dropped. EXTRA adds
corpus terms SCOWL does not carry (they were in the old hand-made list).

Usage:
    tar -xzf scowl-2020.12.07.tar.gz
    python3 build_parse_check_words.py scowl-2020.12.07/final
"""
import os
import pathlib
import re
import sys

MAX_SIZE = 70
SPELLINGS = ("english", "american", "british", "british_z", "canadian")
CATEGORIES = ("words", "upper", "proper-names")
EXTRA = (
    "ascii", "bitwise", "charset", "config", "csv", "dict", "etc", "html",
    "json", "newline", "pdf", "workset", "xml", "yaml",
)
OUT = pathlib.Path(__file__).resolve().with_name("parse_check_words.txt")

_NAME = re.compile(r"(?P<spelling>[a-z_]+)-(?P<category>[a-z-]+)\.(?P<size>\d+)$")


def selected(final_dir: pathlib.Path) -> list[pathlib.Path]:
    out = []
    for path in sorted(final_dir.iterdir()):
        m = _NAME.match(path.name)
        if (m and m.group("spelling") in SPELLINGS
                and m.group("category") in CATEGORIES
                and int(m.group("size")) <= MAX_SIZE):
            out.append(path)
    return out


def build(final_dir: pathlib.Path) -> set[str]:
    words: set[str] = set(EXTRA)
    for path in selected(final_dir):
        # SCOWL's final/ lists are ISO-8859-1.
        with open(path, encoding="latin-1") as fh:
            for line in fh:
                w = line.strip().lower()
                if w.isascii() and w.isalpha() and len(w) >= 2:
                    words.add(w)
    return words


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not os.path.isdir(argv[0]):
        print(__doc__, file=sys.stderr)
        return 2
    final_dir = pathlib.Path(argv[0])
    files = selected(final_dir)
    if not files:
        print(f"no SCOWL lists found in {final_dir}", file=sys.stderr)
        return 1
    words = build(final_dir)
    OUT.write_text("".join(w + "\n" for w in sorted(words)), encoding="ascii")
    print(f"wrote {len(words)} words from {len(files)} SCOWL lists to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
