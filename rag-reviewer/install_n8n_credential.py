#!/usr/bin/env python3
"""Build a temporary n8n import file without printing the secret."""

import argparse
import json
import os
import pathlib


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    key = pathlib.Path(args.key_file).read_text(encoding="utf-8").strip()
    if len(key) < 32:
        raise SystemExit("reviewer API key is unexpectedly short")
    output = pathlib.Path(args.output)
    output.write_text(json.dumps([{
        "id": "RAGReviewAuth001",
        "name": "RAG Reviewer API",
        "type": "httpHeaderAuth",
        "data": {"name": "X-API-Key", "value": key},
    }]), encoding="utf-8")
    os.chmod(output, 0o600)


if __name__ == "__main__":
    main()
