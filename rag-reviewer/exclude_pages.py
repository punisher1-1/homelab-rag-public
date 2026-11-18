#!/usr/bin/env python3
"""Explicitly curate incoming Docling JSON; no approval or DB writes."""
import argparse
import json
import os

from reviewer_core import ReviewerStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", help="incoming collection/filename.json")
    parser.add_argument("sha256", help="full current incoming JSON hash, NOT the raw PDF hash")
    parser.add_argument("--pages", nargs="+", type=int, required=True,
                        help="1-based original PDF indices, never printed labels")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    try:
        result = ReviewerStore(os.environ.get("RAG_ROOT", "/data/rag-store")).exclude_pages(
            args.document, args.sha256, args.pages, args.reason, args.confirm
        )
    except (ValueError, OSError) as exc:
        parser.exit(1, f"page exclusion refused: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
