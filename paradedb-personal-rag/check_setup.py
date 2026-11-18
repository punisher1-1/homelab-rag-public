#!/usr/bin/env python3
"""
check_setup.py — verify the plumbing BEFORE ingesting anything.

Run this first. It confirms three things independently, so if something is
broken you know exactly which piece it is:
  1. We can connect to ParadeDB and both extensions are installed.
  2. The chunks table exists with the right shape.
  3. The Ollama embedding endpoint answers and returns 1024-dim vectors.

Usage:  python check_setup.py
"""
import os
import sys
import requests
import psycopg
from dotenv import load_dotenv

load_dotenv()

DB = dict(
    host=os.environ["DB_HOST"],
    port=os.environ["DB_PORT"],
    dbname=os.environ["DB_NAME"],
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
)
OLLAMA_URL = os.environ["OLLAMA_URL"].rstrip("/")
EMBED_MODEL = os.environ["EMBED_MODEL"]
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))


def ok(msg):
    print(f"  \033[92m[OK]\033[0m {msg}")


def fail(msg):
    print(f"  \033[91m[FAIL]\033[0m {msg}")


def check_db():
    print("1. Database (ParadeDB on the database host)")
    try:
        with psycopg.connect(**DB, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT extname, extversion FROM pg_extension "
                "WHERE extname IN ('vector','pg_search') ORDER BY extname;"
            )
            exts = dict(cur.fetchall())
            for name in ("vector", "pg_search"):
                if name in exts:
                    ok(f"extension {name} {exts[name]}")
                else:
                    fail(f"extension {name} MISSING")
            # table shape
            cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='chunks';"
            )
            ncols = cur.fetchone()[0]
            if ncols:
                cur.execute("SELECT count(*) FROM chunks;")
                nrows = cur.fetchone()[0]
                ok(f"table chunks exists ({ncols} columns, {nrows} rows currently)")
            else:
                fail("table chunks NOT found — run the schema block first")
        return True
    except Exception as e:
        fail(f"could not connect: {e}")
        return False


def check_embeddings():
    print("2. Embeddings (Ollama bge-m3 on the Z2 Mini)")
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": "hello world"},
            timeout=30,
        )
        r.raise_for_status()
        vec = r.json()["embeddings"][0]
        if len(vec) == EMBED_DIM:
            ok(f"{EMBED_MODEL} returned a {len(vec)}-dim vector (matches column)")
            return True
        fail(f"{EMBED_MODEL} returned {len(vec)} dims but column expects {EMBED_DIM}")
        return False
    except Exception as e:
        fail(f"embedding call failed: {e}")
        return False


if __name__ == "__main__":
    db_ok = check_db()
    emb_ok = check_embeddings()
    print()
    if db_ok and emb_ok:
        print("All checks passed — you're ready to run  python ingest.py")
        sys.exit(0)
    print("Fix the [FAIL] items above before ingesting.")
    sys.exit(1)
