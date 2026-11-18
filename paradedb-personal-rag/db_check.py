#!/usr/bin/env python3
"""
db_check.py — corpus QA against the live ParadeDB index.

Answers "what is ACTUALLY in the database?" rather than reasoning from files on
disk. Run it from this directory so it finds .env:

    source ~/paradedb-rag-venv/bin/activate
    cd "…/Homelab:AI/homelab-rag/paradedb-personal-rag"
    python db_check.py

Why a file and not a heredoc: `python - <<'PY'` reads from stdin, so there is no
calling frame, and python-dotenv's find_dotenv() walks stack frames to locate
.env — it raises `AssertionError: frame.f_back is not None`. Passing an explicit
path (below) sidesteps find_dotenv entirely.
"""
import os
import re
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


def rule(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


with psycopg.connect(**DB, connect_timeout=10) as conn, conn.cursor() as cur:

    # ---------------------------------------------------------------- inventory
    rule("1. DOCUMENT INVENTORY — does each document's text mention its own name?")
    cur.execute("""
        SELECT source_path,
               count(*)                                             AS chunks,
               bool_or(chunk_text ILIKE '%Mossberg%')                AS mossberg,
               bool_or(chunk_text ILIKE '%V0348718%')                AS serial,
               bool_or(chunk_text ILIKE '%Martin Ewer%')             AS ewer,
               bool_or(chunk_text ILIKE '%Blade Pistol Stabilizer%') AS blade,
               bool_or(chunk_text ILIKE '%concealed on a person%')   AS conceal
        FROM chunks
        GROUP BY source_path
        ORDER BY source_path;
    """)
    rows = cur.fetchall()
    hdr = ("source_path", "chunks", "mossbg", "serial", "ewer", "blade", "conceal")
    print(f"{hdr[0]:<52}{hdr[1]:>7} {hdr[2]:>7} {hdr[3]:>7} {hdr[4]:>6} {hdr[5]:>6} {hdr[6]:>8}")
    for r in rows:
        name = (r[0] or "")[-50:]
        print(f"{name:<52}{r[1]:>7} {str(r[2]):>7} {str(r[3]):>7} {str(r[4]):>6} {str(r[5]):>6} {str(r[6]):>8}")

    print("\n  READ IT AS:")
    print("   • path says 'mossberg' but mossbg=False and ewer=True  -> filenames are swapped")
    print("   • conceal=True                                          -> the caveat IS indexed")

    # ------------------------------------------------------- the disputed number
    rule("2. IS '26' A MATCHABLE BM25 TOKEN? (word-boundary vs merged into a blob)")
    cur.execute(r"""
        SELECT source_path,
               count(*) FILTER (WHERE chunk_text ~ '\y26\y')        AS clean_26,
               count(*) FILTER (WHERE chunk_text ~ '[a-z]26|26[a-z]') AS merged_26,
               count(*) FILTER (WHERE chunk_text ~ '\y18\y')        AS clean_18
        FROM chunks
        GROUP BY source_path
        HAVING count(*) FILTER (WHERE chunk_text ~ '26') > 0
        ORDER BY source_path;
    """)
    print(f"{'source_path':<52}{'clean 26':>9}{'merged':>8}{'clean 18':>9}")
    for r in cur.fetchall():
        print(f"{(r[0] or '')[-50:]:<52}{r[1]:>9}{r[2]:>8}{r[3]:>9}")
    print("\n  merged_26 > 0 and clean_26 == 0  ->  BM25 cannot match '26' in that document")

    # --------------------------------------------------------------- OCR damage
    rule("3. OCR DAMAGE SCAN (the ingest-QA checks)")
    checks = [
        ("space-collapse (>=19 letters)",   r"[A-Za-z]{19,}"),
        ("mangled section symbol",          r"[^A-Za-z]([sS$]) ?(58|92|47)[0-9]"),
        ("digit-letter fusion",             r"[0-9](inches|inch)|less?than[0-9]"),
        ("digit/letter O confusion",        r"[0-9]\.[OolI]\y"),
    ]
    for label, pat in checks:
        cur.execute(
            "SELECT count(*), count(DISTINCT source_path) FROM chunks WHERE chunk_text ~ %s",
            (pat,),
        )
        n, docs = cur.fetchone()
        flag = "  <-- look at these" if n else ""
        print(f"  {label:<34} chunks={n:<5} docs={docs}{flag}")

    # ------------------------------------------------------------------- totals
    rule("4. TOTALS")
    cur.execute("SELECT count(*), count(DISTINCT source_path) FROM chunks;")
    n, docs = cur.fetchone()
    print(f"  {n} chunks across {docs} documents")
    cur.execute("""
        SELECT DISTINCT metadata->>'collection' AS collection, count(*)
        FROM chunks GROUP BY 1 ORDER BY 1;
    """)
    for c, k in cur.fetchall():
        print(f"    collection={c or '(none)':<14} {k} chunks")

print("\nDone. Nothing was modified — read-only.\n")
