#!/usr/bin/env python3
"""Upgrade legacy 16-character chunk hashes to full SHA-256 values."""

import argparse
import hashlib
import os
import pathlib

import psycopg
from dotenv import load_dotenv


load_dotenv()


def full_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="commit the metadata upgrade")
    args = parser.parse_args()

    root = pathlib.Path(os.environ["DROP_DIR"]).expanduser().resolve()
    files = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.startswith(".")
        and path.suffix.lower() in {".json", ".pdf", ".md", ".markdown", ".txt", ".docx"}
        and path.name.lower() != "readme.md"
    ]
    password = os.environ.get("DB_PASSWORD", "")
    if not password and os.environ.get("DB_PASSWORD_FILE"):
        password = pathlib.Path(os.environ["DB_PASSWORD_FILE"]).read_text(
            encoding="utf-8"
        ).strip()
    db = {
        "host": os.environ["DB_HOST"],
        "port": os.environ["DB_PORT"],
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": password,
    }
    matched_files = updated_rows = 0
    with psycopg.connect(**db) as connection:
        with connection.cursor() as cursor:
            for path in files:
                rel = path.relative_to(root)
                collection = rel.parts[0].lower() if len(rel.parts) > 1 else "general"
                digest = full_sha256(path)
                cursor.execute(
                    "SELECT count(*), count(DISTINCT source_path) FROM chunks "
                    "WHERE metadata->>'file' = %s AND metadata->>'collection' = %s "
                    "AND metadata->>'sha' = %s",
                    (path.name, collection, digest[:16]),
                )
                count, sources = cursor.fetchone()
                if not count:
                    continue
                if sources != 1:
                    raise RuntimeError(
                        f"refusing ambiguous migration for {rel}: {sources} source paths"
                    )
                matched_files += 1
                updated_rows += count
                print(f"{rel}: {count} rows {digest[:16]} -> {digest}")
                cursor.execute(
                    "UPDATE chunks SET metadata = "
                    "jsonb_set(metadata, '{sha}', to_jsonb(%s::text)) "
                    "WHERE metadata->>'file' = %s AND metadata->>'collection' = %s "
                    "AND metadata->>'sha' = %s",
                    (digest, path.name, collection, digest[:16]),
                )
        if args.apply:
            connection.commit()
        else:
            connection.rollback()
    mode = "updated" if args.apply else "would update"
    print(f"{mode} {updated_rows} rows across {matched_files} files")


if __name__ == "__main__":
    main()
