from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from reviewer_core import ReviewError, ReviewerStore, sha256_file, utc_now


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REBUILD_CONFIRMATION = "TRUNCATE AND REBUILD CHUNKS"
APPROVED_SUFFIXES = {".pdf", ".md", ".markdown", ".txt", ".docx", ".json"}


class IngestionError(ValueError):
    pass


class IngestionManager:
    """Serialize approved-document writes and persist status on the NAS."""

    def __init__(
        self,
        store: ReviewerStore,
        runner: Callable[[pathlib.Path, str, str, str], dict] | None = None,
        resetter: Callable[[], None] | None = None,
    ):
        self.store = store
        self.receipts = store.root / "ingestions"
        self.rebuilds = store.root / "rebuilds"
        self._runner = runner or self._run_ingest
        self._resetter = resetter or self._truncate_chunks
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-ingest")
        self._lock = threading.Lock()
        self._jobs: dict[str, Future] = {}
        self._rebuild_jobs: dict[str, Future] = {}

    def ensure_layout(self) -> None:
        self.receipts.mkdir(parents=True, exist_ok=True)
        self.rebuilds.mkdir(parents=True, exist_ok=True)

    def recover_interrupted_rebuilds(self) -> int:
        """Mark destructive rebuilds abandoned by a process restart as failed."""
        recovered = 0
        self.ensure_layout()
        for path in sorted(self.rebuilds.glob("*.json")):
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if receipt.get("status") not in {"queued", "running"}:
                continue
            receipt.update(
                status="failed",
                completed_at=utc_now(),
                error="IngestionError: worker restarted during corpus rebuild; rerun explicitly",
            )
            self.store._atomic_json(path, receipt)
            recovered += 1
        return recovered

    def health(self) -> dict:
        checks = {
            "paradedb": {"ok": False},
            "embedder": {"ok": False, "model": os.environ.get("EMBED_MODEL")},
        }
        try:
            self._load_secret_env("DB_PASSWORD")
            import psycopg

            connection = psycopg.connect(
                host=os.environ["DB_HOST"],
                port=os.environ["DB_PORT"],
                dbname=os.environ["DB_NAME"],
                user=os.environ["DB_USER"],
                password=os.environ["DB_PASSWORD"],
                connect_timeout=3,
            )
            connection.execute("SELECT 1")
            connection.close()
            checks["paradedb"]["ok"] = True
        except Exception as exc:
            checks["paradedb"]["error"] = type(exc).__name__

        try:
            import requests

            response = requests.get(
                f"{os.environ['OLLAMA_URL'].rstrip('/')}/api/tags", timeout=3
            )
            response.raise_for_status()
            expected = os.environ.get("EMBED_MODEL", "").split(":", 1)[0]
            names = [item.get("name", "") for item in response.json().get("models", [])]
            checks["embedder"]["ok"] = any(
                name == expected or name.startswith(f"{expected}:") for name in names
            )
            if not checks["embedder"]["ok"]:
                checks["embedder"]["error"] = "configured model is not installed"
        except Exception as exc:
            checks["embedder"]["error"] = type(exc).__name__
        return checks

    def start(self, identifier: str, sha256: str) -> dict:
        digest = sha256.lower()
        if not SHA256_RE.fullmatch(digest):
            raise IngestionError("sha256 must contain exactly 64 lowercase hex characters")

        path = self.store.resolve("approved", identifier)
        if sha256_file(path) != digest:
            raise IngestionError("stale SHA-256: approved document does not match the request")
        self._require_pass_review(identifier, digest)
        collection = pathlib.PurePosixPath(identifier).parts[0].lower()
        canonical_source = f"rag://approved/{identifier}"

        with self._lock:
            existing = self._read_receipt(digest, required=False)
            if existing and existing.get("status") == "succeeded":
                return existing
            future = self._jobs.get(digest)
            if future and not future.done():
                return self._read_receipt(digest)

            receipt = {
                "ingestion_id": digest,
                "document": identifier,
                "sha256": digest,
                "source_path": canonical_source,
                "collection": collection,
                "status": "queued",
                "requested_at": utc_now(),
                "started_at": None,
                "completed_at": None,
                "chunk_count": None,
                "embed_model": os.environ.get("EMBED_MODEL"),
                "embed_dim": None,
                "error": None,
            }
            self._write_receipt(digest, receipt)
            self._jobs[digest] = self._executor.submit(
                self._execute, path, identifier, digest, collection, canonical_source
            )
            return receipt

    def status(self, ingestion_id: str) -> dict:
        digest = ingestion_id.removeprefix("sha256:").lower()
        if not SHA256_RE.fullmatch(digest):
            raise IngestionError("invalid ingestion id")
        with self._lock:
            receipt = self._read_receipt(digest)
            future = self._jobs.get(digest)
            if receipt.get("status") in {"queued", "running"} and not (
                future and not future.done()
            ):
                receipt.update(
                    status="failed",
                    completed_at=utc_now(),
                    error="IngestionError: worker restarted before the job completed",
                )
                self._write_receipt(digest, receipt)
            return receipt

    def start_rebuild(self, confirmation: str) -> dict:
        if confirmation != REBUILD_CONFIRMATION:
            raise IngestionError(
                f"confirmation must exactly match: {REBUILD_CONFIRMATION}"
            )
        documents = self._approved_documents()
        if not documents:
            raise IngestionError("approved document store is empty; refusing to truncate")

        with self._lock:
            for rebuild_id, future in self._rebuild_jobs.items():
                if not future.done():
                    return self._read_rebuild_receipt(rebuild_id)
            rebuild_id = f"{utc_now().replace(':', '').replace('+00:00', 'Z')}-{uuid.uuid4().hex[:8]}"
            receipt = {
                "rebuild_id": rebuild_id,
                "status": "queued",
                "requested_at": utc_now(),
                "started_at": None,
                "completed_at": None,
                "documents_total": len(documents),
                "documents_succeeded": 0,
                "documents_failed": 0,
                "chunk_count": None,
                "receipt_archive": None,
                "failures": [],
                "error": None,
            }
            self._write_rebuild_receipt(rebuild_id, receipt)
            self._rebuild_jobs[rebuild_id] = self._executor.submit(
                self._execute_rebuild, rebuild_id, documents
            )
            return receipt

    def rebuild_status(self, rebuild_id: str) -> dict:
        if not re.fullmatch(r"[0-9T.\-Z+]+-[0-9a-f]{8}", rebuild_id):
            raise IngestionError("invalid rebuild id")
        with self._lock:
            receipt = self._read_rebuild_receipt(rebuild_id)
            future = self._rebuild_jobs.get(rebuild_id)
            if receipt.get("status") in {"queued", "running"} and not (
                future and not future.done()
            ):
                receipt.update(
                    status="failed",
                    completed_at=utc_now(),
                    error="IngestionError: worker restarted during corpus rebuild; rerun explicitly",
                )
                self._write_rebuild_receipt(rebuild_id, receipt)
            return receipt

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _require_pass_review(self, identifier: str, digest: str) -> None:
        review_path = self.store.reviews / f"{digest}.json"
        if not review_path.is_file():
            raise IngestionError("no recorded review exists for this SHA-256")
        review = json.loads(review_path.read_text(encoding="utf-8"))
        if review.get("document") != identifier or review.get("final_status") != "PASS":
            raise IngestionError("recorded review does not authorize ingestion")

    def _approved_documents(self) -> list[tuple[pathlib.Path, str, str, str, str]]:
        documents = []
        for path in sorted(self.store.approved.rglob("*")):
            if (
                not path.is_file()
                or path.name.startswith(".")
                or path.name.lower() == "readme.md"
                or path.suffix.lower() not in APPROVED_SUFFIXES
                or any(part.startswith(".") for part in path.relative_to(self.store.approved).parts)
            ):
                continue
            rel = path.relative_to(self.store.approved)
            if len(rel.parts) > 1:
                identifier = rel.as_posix()
                collection = rel.parts[0].lower()
            else:
                identifier = f"general/{rel.name}"
                collection = "general"
            digest = sha256_file(path)
            documents.append(
                (path, identifier, digest, collection, f"rag://approved/{identifier}")
            )
        return documents

    def _execute_rebuild(
        self,
        rebuild_id: str,
        documents: list[tuple[pathlib.Path, str, str, str, str]],
    ) -> None:
        receipt = self._read_rebuild_receipt(rebuild_id)
        receipt.update(status="running", started_at=utc_now(), error=None)
        self._write_rebuild_receipt(rebuild_id, receipt)
        try:
            archive = self._archive_ingestion_receipts(rebuild_id)
            self._resetter()
            for old_receipt in self.receipts.glob("*.json"):
                old_receipt.unlink()
            receipt["receipt_archive"] = str(archive.relative_to(self.store.root))
            self._write_rebuild_receipt(rebuild_id, receipt)

            total_chunks = 0
            failures = []
            succeeded = 0
            for path, identifier, digest, collection, canonical_source in documents:
                document_receipt = {
                    "ingestion_id": digest,
                    "document": identifier,
                    "sha256": digest,
                    "source_path": canonical_source,
                    "collection": collection,
                    "status": "running",
                    "requested_at": receipt["requested_at"],
                    "started_at": utc_now(),
                    "completed_at": None,
                    "chunk_count": None,
                    "embed_model": os.environ.get("EMBED_MODEL"),
                    "embed_dim": None,
                    "error": None,
                    "rebuild_id": rebuild_id,
                }
                self._write_receipt(digest, document_receipt)
                try:
                    if sha256_file(path) != digest:
                        raise IngestionError("approved document changed before rebuild ingestion")
                    result = self._runner(path, collection, digest, canonical_source)
                    if sha256_file(path) != digest:
                        raise IngestionError("approved document changed during rebuild ingestion")
                    document_receipt.update(
                        status="succeeded",
                        completed_at=utc_now(),
                        chunk_count=result["chunk_count"],
                        collection=result.get("collection", collection),
                        embed_model=result.get("embed_model", document_receipt["embed_model"]),
                        embed_dim=result.get("embed_dim"),
                        reason=result.get("reason"),
                    )
                    total_chunks += int(result["chunk_count"])
                    succeeded += 1
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"[:2000]
                    document_receipt.update(
                        status="failed", completed_at=utc_now(), error=message
                    )
                    failures.append({"document": identifier, "error": message})
                self._write_receipt(digest, document_receipt)
                receipt.update(
                    documents_succeeded=succeeded,
                    documents_failed=len(failures),
                    chunk_count=total_chunks,
                    failures=failures,
                )
                self._write_rebuild_receipt(rebuild_id, receipt)

            receipt.update(
                status="succeeded" if not failures else "failed",
                completed_at=utc_now(),
                documents_succeeded=succeeded,
                documents_failed=len(failures),
                chunk_count=total_chunks,
                failures=failures,
                error=(
                    None
                    if not failures
                    else f"{len(failures)} approved document(s) failed to rebuild"
                ),
            )
        except Exception as exc:
            receipt.update(
                status="failed",
                completed_at=utc_now(),
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )
        self._write_rebuild_receipt(rebuild_id, receipt)

    def _archive_ingestion_receipts(self, rebuild_id: str) -> pathlib.Path:
        archive = self.receipts / "archive" / rebuild_id
        archive.mkdir(parents=True, exist_ok=False)
        for receipt in self.receipts.glob("*.json"):
            shutil.copy2(receipt, archive / receipt.name)
        return archive

    def _truncate_chunks(self) -> None:
        self._load_secret_env("DB_PASSWORD")
        import psycopg

        with psycopg.connect(
            host=os.environ["DB_HOST"],
            port=os.environ["DB_PORT"],
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
        ) as connection:
            connection.execute("TRUNCATE chunks RESTART IDENTITY")
            connection.commit()

    def _execute(
        self,
        path: pathlib.Path,
        identifier: str,
        digest: str,
        collection: str,
        canonical_source: str,
    ) -> None:
        receipt = self._read_receipt(digest)
        receipt.update(status="running", started_at=utc_now(), error=None)
        self._write_receipt(digest, receipt)
        try:
            if sha256_file(path) != digest:
                raise IngestionError("approved document changed before ingestion started")
            result = self._runner(path, collection, digest, canonical_source)
            if sha256_file(path) != digest:
                raise IngestionError("approved document changed during ingestion")
            receipt.update(
                status="succeeded",
                completed_at=utc_now(),
                chunk_count=result["chunk_count"],
                collection=result.get("collection", collection),
                embed_model=result.get("embed_model", receipt.get("embed_model")),
                embed_dim=result.get("embed_dim"),
                reason=result.get("reason"),
                error=None,
            )
        except Exception as exc:  # receipt is the durable failure boundary
            receipt.update(
                status="failed",
                completed_at=utc_now(),
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )
        self._write_receipt(digest, receipt)

    @staticmethod
    def _load_secret_env(name: str) -> None:
        if os.environ.get(name):
            return
        secret_path = os.environ.get(f"{name}_FILE")
        if secret_path:
            os.environ[name] = pathlib.Path(secret_path).read_text(encoding="utf-8").strip()

    def _run_ingest(
        self, path: pathlib.Path, collection: str, digest: str, canonical_source: str
    ) -> dict:
        self._load_secret_env("DB_PASSWORD")
        import ingest
        from corpus_manifest import Manifest

        with ingest.psycopg.connect(**ingest.DB) as conn:
            # The Mac historically wrote /Volumes/... while this worker sees
            # /data/.... Adopt a unique, matching legacy row set by content hash
            # so the handoff does not duplicate chunks or re-embed the corpus.
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*), count(DISTINCT source_path), "
                    "min(metadata->>'embed_model'), min((metadata->>'embed_dim')::int) "
                    "FROM chunks WHERE metadata->>'sha' IN (%s, %s) "
                    "AND metadata->>'collection' = %s AND metadata->>'file' = %s",
                    (digest, digest[:16], collection, path.name),
                )
                legacy_count, legacy_sources, legacy_model, legacy_dim = cursor.fetchone()
            expected_model = os.environ.get("EMBED_MODEL")
            expected_dim = int(os.environ.get("EMBED_DIM", "0") or 0)
            if legacy_count and legacy_sources > 1:
                raise IngestionError(
                    "multiple existing source paths match this SHA; deduplicate before adoption"
                )
            if legacy_count and (
                legacy_model != expected_model or legacy_dim != expected_dim
            ):
                raise IngestionError(
                    "existing chunks use a different embedding model or dimension"
                )
            if legacy_count:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE chunks SET source_path = %s, "
                        "metadata = jsonb_set(metadata, '{sha}', to_jsonb(%s::text)) "
                        "WHERE metadata->>'sha' IN (%s, %s) AND metadata->>'collection' = %s "
                        "AND metadata->>'file' = %s",
                        (
                            canonical_source,
                            digest,
                            digest,
                            digest[:16],
                            collection,
                            path.name,
                        ),
                    )
                conn.commit()
                count, reason, actual_collection = legacy_count, "adopted-existing", collection
            else:
                count, reason, actual_collection = ingest.ingest_drop_file(
                    conn,
                    path,
                    collection,
                    force=False,
                    manifest=Manifest(require=False),
                    source_path=canonical_source,
                    content_sha256=digest,
                )
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*), min(metadata->>'embed_model'), "
                    "min((metadata->>'embed_dim')::int) "
                    "FROM chunks WHERE source_path = %s AND metadata->>'sha' = %s",
                    (canonical_source, digest),
                )
                verified_count, embed_model, embed_dim = cursor.fetchone()
        if not verified_count:
            raise IngestionError("post-ingestion verification found no matching chunks")
        if count and count != verified_count:
            raise IngestionError(
                f"chunk verification mismatch: writer={count}, database={verified_count}"
            )
        return {
            "chunk_count": verified_count,
            "collection": actual_collection,
            "embed_model": embed_model,
            "embed_dim": embed_dim,
            "reason": reason,
        }

    def _receipt_path(self, digest: str) -> pathlib.Path:
        return self.receipts / f"{digest}.json"

    def _read_receipt(self, digest: str, *, required: bool = True) -> dict | None:
        path = self._receipt_path(digest)
        if not path.is_file():
            if required:
                raise FileNotFoundError(digest)
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_receipt(self, digest: str, payload: dict) -> None:
        self.ensure_layout()
        self.store._atomic_json(self._receipt_path(digest), payload)

    def _rebuild_receipt_path(self, rebuild_id: str) -> pathlib.Path:
        return self.rebuilds / f"{rebuild_id}.json"

    def _read_rebuild_receipt(self, rebuild_id: str) -> dict:
        path = self._rebuild_receipt_path(rebuild_id)
        if not path.is_file():
            raise FileNotFoundError(rebuild_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_rebuild_receipt(self, rebuild_id: str, payload: dict) -> None:
        self.ensure_layout()
        self.store._atomic_json(self._rebuild_receipt_path(rebuild_id), payload)
