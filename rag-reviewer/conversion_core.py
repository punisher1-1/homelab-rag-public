from __future__ import annotations

import json
import mimetypes
import os
import pathlib
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

import requests

from reviewer_core import ReviewError, ReviewerStore, sha256_file, utc_now


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_SOURCE_SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".txt",
    ".csv", ".xml", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
}


class ConversionError(ValueError):
    pass


class ConversionManager:
    """Convert stable NAS source files through private Docling Serve."""

    def __init__(
        self,
        store: ReviewerStore,
        runner: Callable[[pathlib.Path], dict] | None = None,
    ):
        self.store = store
        self.raw = store.documents / "raw"
        self.receipts = store.root / "conversions"
        self._runner = runner or self._run_docling
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rag-convert")
        self._lock = threading.Lock()
        self._jobs: dict[str, Future] = {}
        self._scan_state: dict[str, tuple[int, int]] = {}

    def ensure_layout(self) -> None:
        self.raw.mkdir(parents=True, exist_ok=True)
        self.receipts.mkdir(parents=True, exist_ok=True)

    def recover_interrupted(self) -> int:
        """Make jobs abandoned by a process restart eligible for one safe retry."""
        recovered = 0
        self.ensure_layout()
        for path in sorted(self.receipts.glob("*.json")):
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if receipt.get("status") not in {"queued", "running"}:
                continue
            receipt.update(
                status="interrupted",
                completed_at=utc_now(),
                error="ConversionError: worker restarted before the job completed",
            )
            self.store._atomic_json(path, receipt)
            recovered += 1
        return recovered

    @staticmethod
    def _clean_identifier(identifier: str) -> pathlib.PurePosixPath:
        if not identifier or "\\" in identifier:
            raise ConversionError("document must be a non-empty POSIX relative path")
        rel = pathlib.PurePosixPath(identifier)
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            raise ConversionError("document must not be absolute or contain path traversal")
        if len(rel.parts) < 2:
            raise ConversionError("document must include a collection and filename")
        if rel.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES:
            raise ConversionError(f"unsupported source format: {rel.suffix or '(none)'}")
        return rel

    def resolve_raw(self, identifier: str, *, must_exist: bool = True) -> pathlib.Path:
        base = self.raw.resolve()
        candidate = (base / self._clean_identifier(identifier)).resolve()
        if not candidate.is_relative_to(base):
            raise ConversionError("document escapes the configured raw folder")
        if must_exist and not candidate.is_file():
            raise FileNotFoundError(identifier)
        return candidate

    def list_raw(self) -> list[dict]:
        """Return supported, stable source files without an existing receipt."""
        current: dict[str, tuple[int, int]] = {}
        ready = []
        if not self.raw.is_dir():
            return ready
        for path in sorted(self.raw.rglob("*")):
            if not path.is_file() or path.name.startswith(".") or path.name.endswith(".partial"):
                continue
            rel = path.relative_to(self.raw).as_posix()
            try:
                self._clean_identifier(rel)
                stat = path.stat()
            except (OSError, ConversionError):
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            current[rel] = signature
            if self._scan_state.get(rel) != signature:
                continue
            digest = sha256_file(path)
            receipt = self._read_receipt(digest, required=False)
            if receipt and receipt.get("status") != "interrupted":
                continue
            item = {
                "document": rel,
                "collection": pathlib.PurePosixPath(rel).parts[0].lower(),
                "sha256": digest,
                "size": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
            }
            if receipt:
                item["retry"] = True
                item["previous_status"] = "interrupted"
            ready.append(item)
        self._scan_state = current
        return ready

    def start(self, identifier: str, sha256: str, *, retry: bool = False) -> dict:
        digest = sha256.lower()
        if not SHA256_RE.fullmatch(digest):
            raise ConversionError("sha256 must contain exactly 64 lowercase hex characters")
        path = self.resolve_raw(identifier)
        if sha256_file(path) != digest:
            raise ConversionError("stale SHA-256: raw document does not match the request")

        output_identifier = str(pathlib.PurePosixPath(identifier).with_suffix(".json"))
        output = self.store.resolve("incoming", output_identifier, must_exist=False)

        with self._lock:
            existing = self._read_receipt(digest, required=False)
            if existing and existing.get("status") == "succeeded":
                return existing
            future = self._jobs.get(digest)
            if future and not future.done():
                return self._read_receipt(digest)
            if existing and existing.get("status") == "interrupted":
                retry = True
            if existing and not retry:
                return existing

            receipt = {
                "conversion_id": digest,
                "document": identifier,
                "sha256": digest,
                "source_path": f"rag://raw/{identifier}",
                "output_document": output_identifier,
                "output_sha256": None,
                "status": "queued",
                "requested_at": utc_now(),
                "started_at": None,
                "completed_at": None,
                "attempt": int(existing.get("attempt", 0) if existing else 0) + 1,
                "engine": "docling-serve",
                "engine_image": os.environ.get("DOCLING_IMAGE"),
                "settings": {
                    "to_formats": ["json"],
                    "do_ocr": True,
                    "force_ocr": False,
                    "table_mode": "accurate",
                    "image_export_mode": "placeholder",
                },
                "processing_time": None,
                "warnings": [],
                "error": None,
            }
            self._write_receipt(digest, receipt)
            self._jobs[digest] = self._executor.submit(
                self._execute, path, output, identifier, digest
            )
            return receipt

    def status(self, conversion_id: str) -> dict:
        digest = conversion_id.removeprefix("sha256:").lower()
        if not SHA256_RE.fullmatch(digest):
            raise ConversionError("invalid conversion id")
        with self._lock:
            receipt = self._read_receipt(digest)
            future = self._jobs.get(digest)
            if receipt.get("status") in {"queued", "running"} and not (
                future and not future.done()
            ):
                receipt.update(
                    status="interrupted",
                    completed_at=utc_now(),
                    error="ConversionError: worker restarted before the job completed",
                )
                self._write_receipt(digest, receipt)
            return receipt

    def health(self) -> dict:
        check = {"ok": False, "url": os.environ.get("DOCLING_URL")}
        try:
            response = requests.get(
                f"{self._base_url()}/version", headers=self._headers(), timeout=5
            )
            response.raise_for_status()
            check["ok"] = True
            check["version"] = response.json()
        except Exception as exc:
            check["error"] = type(exc).__name__
        return check

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _execute(
        self,
        path: pathlib.Path,
        output: pathlib.Path,
        identifier: str,
        digest: str,
    ) -> None:
        receipt = self._read_receipt(digest)
        receipt.update(status="running", started_at=utc_now(), error=None)
        self._write_receipt(digest, receipt)
        try:
            if sha256_file(path) != digest:
                raise ConversionError("raw document changed before conversion started")
            result = self._runner(path)
            if sha256_file(path) != digest:
                raise ConversionError("raw document changed during conversion")
            status = str(result.get("status", "")).lower()
            if status not in {"success", "partial_success"}:
                raise ConversionError(f"Docling returned status {status or '(missing)'}")
            document = result.get("document", {}).get("json_content")
            if isinstance(document, str):
                document = json.loads(document)
            if not isinstance(document, dict) or document.get("schema_name") != "DoclingDocument":
                raise ConversionError("Docling response did not contain a DoclingDocument JSON object")
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists():
                if json.loads(output.read_text(encoding="utf-8")) != document:
                    raise ConversionError("incoming destination exists with different content")
            else:
                self.store._atomic_json(output, document)
            output_digest = sha256_file(output)
            receipt.update(
                status="succeeded",
                completed_at=utc_now(),
                output_sha256=output_digest,
                processing_time=result.get("processing_time"),
                warnings=result.get("errors") or [],
                error=None,
            )
        except Exception as exc:
            receipt.update(
                status="failed",
                completed_at=utc_now(),
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )
        self._write_receipt(digest, receipt)

    def _run_docling(self, path: pathlib.Path) -> dict:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as stream:
            response = requests.post(
                f"{self._base_url()}/v1/convert/file",
                headers=self._headers(),
                files={"files": (path.name, stream, mime)},
                data={
                    "to_formats": "json",
                    "do_ocr": "true",
                    "force_ocr": "false",
                    "table_mode": "accurate",
                    "image_export_mode": "placeholder",
                },
                timeout=(30, int(os.environ.get("DOCLING_REQUEST_TIMEOUT", "7300"))),
            )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _base_url() -> str:
        return os.environ.get("DOCLING_URL", "http://docling-serve:5001").rstrip("/")

    @staticmethod
    def _headers() -> dict[str, str]:
        key = os.environ.get("DOCLING_API_KEY", "")
        key_file = os.environ.get("DOCLING_API_KEY_FILE", "")
        if not key and key_file:
            key = pathlib.Path(key_file).read_text(encoding="utf-8").strip()
        if not key:
            raise ConversionError("Docling API key is not configured")
        return {"X-Api-Key": key}

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
