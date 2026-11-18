#!/usr/bin/env python3
"""Tests for the provenance guard (app.py, added 2026-08-09).

Run with no dependencies and no running gateway:

    python3 test_guard.py

Every case below is REAL TEXT from a logged session, not invented phrasing —
which is the point. The 2026-08-05 turn-3 case is the one that matters: it is the
model at its BEST (delivered GOVERN/MANAGE with real citations, explicitly refused
MEASURE), and a first draft of this guard classified it as a violation and would
have stripped its perfectly good citations. That failure is why the guard has two
tiers instead of one.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "app.py")).read()

# Extract the guard block so we don't need psycopg / fastapi / a live DB.
_start = SRC.index("# Two tiers, because")
_end = SRC.index("# ---------------------------------------------------------- generation plumbing")
NS = {}
exec("import re, os\nDISCLAIM_GUARD='warn'\nSTRIP_MODEL_SOURCES=True\n"
     + SRC[_start:_end], NS)  # noqa: S102

classify = NS["classify_disclaimer"]
strip = NS["strip_model_sources"]
Stripper = NS["SourcesStripper"]

# The abstention floor lives above the guard block, so pull it separately.
FLOOR_NS = {}
_fs, _fe = SRC.index("def retrieval_is_weak"), SRC.index("def locator(")
FLOOR_SRC = SRC[_fs:_fe]


def weak(stats, min_sim=0.0, min_rerank=None):
    """Evaluate retrieval_is_weak under a specific floor configuration."""
    ns = {"ABSTAIN_MIN_SIM": min_sim, "ABSTAIN_MIN_RERANK": min_rerank}
    exec(FLOOR_SRC, ns)  # noqa: S102
    return ns["retrieval_is_weak"](stats)

# Real output from the 2026-08-09 "how long should the PowerPoint be" turn: a
# question with zero support in an ATF/NIST corpus, where the model invented TWO
# citation lists and put the honest disclaimer BETWEEN them.
POWERPOINT_TURN = """Recommended length: 10-15 slides

A typical structure might be:

1. Title slide
2. What is NIST AI 100-1?
3. Why this matters

Timing: aim for 15-20 minutes.

**Sources:**

* atf-p-5320-8-1 — §PAPERWORK REDUCTION ACT NOTICE, p. 208
* NIST.SP.800-53r5 — §1.2 TARGET AUDIENCE, p. 30

Note: This guidance is based on general presentation best practices and is not \
sourced from the provided context.

Sources:

* atf-p-5320-8-1 — §CLASS, p. 192; §GENERALINFORMATION, p. 196
* NIST.SP.800-53r5 — §PT-4 CONSENT, p. 259
"""

SOURCES = ("\n\n**Sources:**\n"
           "- nist.ai.100-1 — §Executive Summary, p. 6\n"
           "- nist.ai.100-1_1 — §Appendix D, p. 47")

# (text, expected_tier, label)
CASES = [
    ("I don't have a \"why this matters\" section in the provided context for "
     "NIST AI 100-1.",
     "absence", "2026-08-09 turn 2 — decline"),

    ("Note: This \"Why This Matters\" section is drafted from general knowledge of "
     "the AI RMF 1.0 framework and is not sourced from the provided context passages.",
     "external", "2026-08-09 turn 3 — the disclaimer that started this"),

    ("If you'd like, I can help you draft a section based on general knowledge, but "
     "it would not be sourced from the provided context.",
     "external", "2026-08-09 turn 2 — the offer"),

    ("I cannot fabricate or invent detailed explanations for the MEASURE function "
     "(not present in any passage).",
     "absence", "2026-08-05 turn 3 — the IDEAL refusal, must NOT be suppressed"),

    ("Per §5.1 Govern, p. 27, the GOVERN function establishes accountability "
     "structures across the AI lifecycle.",
     None, "clean grounded answer"),

    ("The framework is voluntary, risk-based, consensus-driven and "
     "technology-agnostic.",
     None, "clean grounded answer, no locator"),

    ("<think>Is this in my passages or am I recalling it? Not in the context, "
     "maybe. Let me re-read chunk [4]... yes it's there.</think>"
     "NIST AI 100-1 is the AI RMF 1.0 — §Executive Summary, p. 6.",
     None, "reasoning trace must not trip the guard"),

    ("The provided context does not cover Part 2, so you will need to obtain the "
     "full NIST AI 100-1 document.",
     None, "2026-08-05 turn 1 — KNOWN GAP: false advice this guard does not catch"),
]


def main():
    fails = 0

    for text, want, label in CASES:
        got, _ = classify(text)
        ok = got == want
        fails += not ok
        print(f"{'PASS' if ok else '**FAIL**':10} expect={str(want):9} "
              f"got={str(got):9} {label}")

    def check(name, cond):
        nonlocal fails
        fails += not cond
        print(f"{'PASS' if cond else '**FAIL**':10} {name}")

    print()
    NS["DISCLAIM_GUARD"] = "warn"
    g = NS["guard_sources"]
    check("grounded answer passes through untouched",
          g(SOURCES, CASES[4][0]) == SOURCES)
    check("external+warn relabels the block",
          "Retrieved (not cited)" in g(SOURCES, CASES[1][0]))
    check("absence+warn KEEPS the Sources header",
          "**Sources:**" in g(SOURCES, CASES[3][0]))

    NS["DISCLAIM_GUARD"] = "suppress"
    g = NS["guard_sources"]
    check("external+suppress drops the citations",
          "nist.ai.100-1" not in g(SOURCES, CASES[1][0]))
    check("absence+suppress KEEPS the citations (the ideal turn is protected)",
          "nist.ai.100-1" in g(SOURCES, CASES[3][0]))

    NS["DISCLAIM_GUARD"] = "off"
    g = NS["guard_sources"]
    check("off restores pre-2026-08-09 behaviour exactly",
          g(SOURCES, CASES[1][0]) == SOURCES)

    # ---- model-authored Sources blocks -------------------------------------
    print()
    out = strip(POWERPOINT_TURN)
    check("both fabricated blocks removed",
          "atf-p-5320-8-1" not in out and "PT-4 CONSENT" not in out)
    check("the disclaimer BETWEEN the two blocks survives",
          "not sourced from the provided context" in out)
    check("body prose survives", "Recommended length: 10-15 slides" in out)
    check("a numbered list in the BODY is not mistaken for citations",
          "1. Title slide" in out and "3. Why this matters" in out)
    check("no orphaned 'Sources:' heading left behind",
          "Sources:" not in out)
    check("guard still fires on the stripped answer",
          classify(out)[0] == "external")

    # A sentence that merely contains the word must not trigger the heading rule.
    prose = "Check the sources: they disagree on p. 12.\nThat matters here.\n"
    check("'sources:' mid-sentence is not treated as a heading",
          strip(prose) == prose)

    # Streaming must produce byte-identical output to the whole-string path, at
    # any chunk boundary — token boundaries are arbitrary and land mid-heading.
    for size in (1, 3, 7, 13, 64):
        s = Stripper()
        streamed = "".join(s.feed(POWERPOINT_TURN[i:i + size])
                           for i in range(0, len(POWERPOINT_TURN), size)) + s.flush()
        check(f"streaming at {size}-char chunks matches non-streaming",
              streamed == out)

    # ---- abstention floor ---------------------------------------------------
    print()
    # The PowerPoint turn: ten ATF/firewall chunks for a question about slide counts.
    junk = {"top_sim": 0.28, "top_rerank": None, "n": 10}
    good = {"top_sim": 0.71, "top_rerank": None, "n": 10}

    check("floors OFF by default — nothing changes until calibrated",
          not weak(junk) and not weak(good))
    check("sim floor catches the junk retrieval",
          weak(junk, min_sim=0.35))
    check("sim floor leaves a good retrieval alone",
          not weak(good, min_sim=0.35))
    check("reranker score wins over similarity when both are set",
          weak({"top_sim": 0.99, "top_rerank": 0.02, "n": 10},
               min_sim=0.35, min_rerank=0.5))
    check("falls back to similarity when the reranker is off/failed",
          weak({"top_sim": 0.10, "top_rerank": None, "n": 10},
               min_sim=0.35, min_rerank=0.5))
    check("empty stats (task prompt / no question) never abstains",
          not weak({}, min_sim=0.9) and not weak(None, min_sim=0.9))

    NS["DISCLAIM_GUARD"] = "warn"
    check("guard does not double-annotate an abstention note",
          NS["guard_sources"](NS["ABSTAIN_NOTE"], CASES[1][0]) == NS["ABSTAIN_NOTE"])

    print(f"\n{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
