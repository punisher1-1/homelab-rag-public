#!/usr/bin/env python3
"""
check_lad.py — post-ingest verification for the TxDOT Landscape & Aesthetics manual.

Run it the same way as db_check.py:

    source ~/paradedb-rag-venv/bin/activate
    cd "…/Homelab:AI/homelab-rag/paradedb-personal-rag"
    python check_lad.py

WHY A FILE AND NOT A HEREDOC: `python - <<'PY'` reads from stdin, so there is no
calling frame, and python-dotenv's find_dotenv() walks stack frames to locate
.env. Same reason db_check.py exists as a file.

WHAT IT CHECKS, and why each one is here rather than assumed:

  1. printed_page format — ingest.py truncated chapter-relative page numbers
     ("4-4" was stored as 4) until 2026-08-18. If a long-running shell imported
     the old module, the fix silently did not apply to this ingest. The only way
     to know is to read the value back out of the database.

  2. figure captions present — the claim that captions reach the index was made
     against the parser, not the corpus. Verify a scorer with scores, a retriever
     with ranked results, and an ingest with rows.

  3. chunk share — a document that is 3% of the corpus competing against one that
     is 46% will lose retrieval contests it deserves to win. Chunk share is a
     design constraint, not a statistic.
"""
import os
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DB = dict(
    host=os.environ["DB_HOST"],
    port=os.environ.get("DB_PORT", "5432"),
    dbname=os.environ["DB_NAME"],
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
)

DOC = "%lad.json"


def rule(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main() -> int:
    failures = []
    with psycopg.connect(**DB, connect_timeout=10) as conn, conn.cursor() as cur:

        # ------------------------------------------------------------------
        rule("1. printed_page — expect chapter-relative strings (1-2, 2-20, 4-4)")
        cur.execute("""
            SELECT metadata->>'page', metadata->>'printed_page', metadata->>'section'
            FROM chunks
            WHERE source_path LIKE %s AND metadata->>'page' IN ('8','36','73')
            ORDER BY (metadata->>'page')::int, chunk_index
        """, (DOC,))
        rows = cur.fetchall()
        seen = {}
        for pdf, printed, section in rows:
            seen.setdefault(pdf, printed)
            print(f"  pdf {pdf:<5} printed {str(printed):<8} {(section or '')[:46]}")
        expected = {"8": "1-2", "36": "2-20", "73": "4-4"}
        for pdf, want in expected.items():
            got = seen.get(pdf)
            if got != want:
                failures.append(
                    f"printed_page for pdf {pdf}: got {got!r}, expected {want!r}"
                    + ("  <- old module still loaded, re-run the ingest"
                       if got and "-" not in str(got) else ""))
        if not rows:
            failures.append("no lad chunks found at all — did the ingest write them?")

        # ------------------------------------------------------------------
        rule("2. figure captions in the indexed text")
        for needle in ("Concrete Mow Strip", "Figure 4-6", "Cone of Vision",
                       "Rainwater Harvesting Cistern"):
            cur.execute("SELECT count(*) FROM chunks "
                        "WHERE source_path LIKE %s AND chunk_text ILIKE %s",
                        (DOC, f"%{needle}%"))
            n = cur.fetchone()[0]
            print(f"  {needle:<32} {n:>4} chunk(s)")
            if n == 0:
                failures.append(f"caption text {needle!r} is not in the corpus")

        # ------------------------------------------------------------------
        rule("3. chunk share — can lad win a retrieval contest?")
        cur.execute("""
            SELECT title,
                   coalesce(metadata->>'collection','vault') AS collection,
                   count(*) AS chunks,
                   round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct_corpus
            FROM chunks GROUP BY 1,2 ORDER BY chunks DESC
        """)
        print(f"  {'title':<46}{'collection':<14}{'chunks':>8}{'% corpus':>10}")
        for title, coll, n, pct in cur.fetchall():
            print(f"  {title[:45]:<46}{coll[:13]:<14}{n:>8}{pct:>10}")

        cur.execute("""
            SELECT coalesce(metadata->>'collection','vault') AS collection,
                   count(*) FILTER (WHERE source_path LIKE %s) AS lad,
                   count(*) AS total
            FROM chunks GROUP BY 1 HAVING count(*) FILTER (WHERE source_path LIKE %s) > 0
        """, (DOC, DOC))
        for coll, lad, total in cur.fetchall():
            print(f"\n  within '{coll}': lad has {lad} of {total} chunks "
                  f"({100.0 * lad / total:.1f}%), so it is outnumbered "
                  f"{(total - lad) / max(lad, 1):.1f} to 1 by its collection-mates")

    # ----------------------------------------------------------------------
    rule("RESULT")
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  All checks passed.")
    print("  Presence is not retrievability — next run the real query:")
    print('    python query.py "what does the manual say about concrete mow '
          'strips" --collection txdot --full')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
