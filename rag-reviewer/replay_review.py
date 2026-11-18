#!/usr/bin/env python3
"""Archive a failed or unapproved warning review for a controlled replay.

This is an operator-only container command, not an n8n API. It never approves,
promotes, or ingests a document. Once the old receipt is archived, the normal
two-scan n8n intake workflow discovers the unchanged incoming file again.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
from datetime import datetime, timezone
from uuid import uuid4

from reviewer_core import ReviewError, ReviewerStore, sha256_file


FAIL_CONFIRMATION = "ARCHIVE FAILED REVIEW AND REQUEUE"
WARN_CONFIRMATION = "ARCHIVE UNAPPROVED WARNING AND REQUEUE"


def replay_failed_review(
    store: ReviewerStore, identifier: str, sha256: str, *, allow_pending_warn: bool = False
) -> dict:
    digest = sha256.lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ReviewError("expected a full 64-character SHA-256")

    incoming = store.resolve("incoming", identifier)
    if sha256_file(incoming) != digest:
        raise ReviewError("incoming document no longer matches the supplied SHA-256")

    receipt_path = store.reviews / f"{digest}.json"
    if not receipt_path.is_file():
        raise ReviewError("no top-level review receipt exists for this SHA-256")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("document") != identifier or receipt.get("sha256") != digest:
        raise ReviewError("review receipt does not match this document and SHA-256")
    status = receipt.get("final_status")
    pending_warn = (
        status == "WARN"
        and receipt.get("human_approved") is False
        and receipt.get("human_decision") is None
    )
    if not (pending_warn if allow_pending_warn else status == "FAIL"):
        raise ReviewError("receipt does not match the explicitly selected replay mode")

    preflight = store.preflight(identifier, digest)
    if preflight["status"] == "FAIL":
        raise ReviewError("current preflight still FAILs; fix the parser or document first")

    archive_dir = store.reviews / "archive" / digest
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archived = archive_dir / f"{stamp}-{uuid4().hex}.json"
    receipt_path.replace(archived)
    store.remove_approval_link(digest)
    store.resolve_failure(digest, "Archived review for a controlled replay")
    return {
        "document": identifier,
        "sha256": digest,
        "preflight_status": preflight["status"],
        "archived_receipt": str(archived),
        "next_step": "Wait for two n8n intake scans; human approval is still required for WARN.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", help="relative incoming identifier, e.g. pmi/book.json")
    parser.add_argument("sha256", help="full SHA-256 from the failed review receipt")
    parser.add_argument(
        "--confirm", required=True,
        help=f"{FAIL_CONFIRMATION!r} or {WARN_CONFIRMATION!r}",
    )
    args = parser.parse_args()
    if args.confirm not in {FAIL_CONFIRMATION, WARN_CONFIRMATION}:
        parser.error("--confirm must select a supported replay mode exactly")
    root = pathlib.Path(os.environ.get("RAG_ROOT", "/data/rag-store"))
    try:
        result = replay_failed_review(
            ReviewerStore(root), args.document, args.sha256,
            allow_pending_warn=args.confirm == WARN_CONFIRMATION,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f"review not requeued: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
