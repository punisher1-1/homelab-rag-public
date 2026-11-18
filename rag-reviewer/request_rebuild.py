#!/usr/bin/env python3
"""Request and monitor a confirmation-gated canonical corpus rebuild."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import time

import requests


CONFIRMATION = "TRUNCATE AND REBUILD CHUNKS"


def api_key() -> str:
    value = os.environ.get("REBUILD_API_KEY", "")
    path = os.environ.get("REBUILD_API_KEY_FILE", "")
    if not value and path:
        value = pathlib.Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit("rebuild API key is not configured")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TRUNCATE ParadeDB chunks and rebuild every approved NAS document"
    )
    parser.add_argument(
        "--confirm",
        required=True,
        help=f'exact confirmation phrase: "{CONFIRMATION}"',
    )
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        raise SystemExit(f'confirmation must exactly match: "{CONFIRMATION}"')

    base_url = "http://127.0.0.1:8110"
    headers = {"X-Rebuild-Key": api_key()}
    response = requests.post(
        f"{base_url}/v1/rebuilds",
        headers=headers,
        json={"confirmation": args.confirm},
        timeout=30,
    )
    response.raise_for_status()
    receipt = response.json()
    rebuild_id = receipt["rebuild_id"]
    print(f"Rebuild queued: {rebuild_id}", flush=True)

    while receipt["status"] not in {"succeeded", "failed"}:
        time.sleep(5)
        response = requests.get(
            f"{base_url}/v1/rebuilds/{rebuild_id}", headers=headers, timeout=30
        )
        response.raise_for_status()
        receipt = response.json()
        print(
            f"status={receipt['status']} "
            f"documents={receipt['documents_succeeded']}/{receipt['documents_total']} "
            f"chunks={receipt['chunk_count']}",
            flush=True,
        )

    print(json.dumps(receipt, indent=2))
    if receipt["status"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
