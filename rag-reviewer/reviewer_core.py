from __future__ import annotations

import hashlib
import fcntl
import html
import json
import os
import pathlib
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager
from functools import wraps
from urllib.parse import urlsplit

import parse_check
import verified_findings
from page_exclusions import exclude_pdf_pages


ALLOWED_SUFFIXES = {".json"}
VALID_STATUSES = {"PASS", "WARN", "FAIL"}


class ReviewError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def resource_lock(key: str):
    locks = pathlib.Path(tempfile.gettempdir()) / "rag-document-locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    name = hashlib.sha256(key.encode()).hexdigest()
    with (locks / name).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def serialized_document(action):
    """Coordinate API threads and operator commands in the same container."""
    @wraps(action)
    def locked(self, identifier, *args, **kwargs):
        self._clean_identifier(identifier)
        with resource_lock(f"{self.root}/{identifier}"):
            return action(self, identifier, *args, **kwargs)
    return locked


def approval_origin() -> tuple[str, str | None, int | None]:
    """The only n8n origin whose form links may enter the approval inbox.

    Set N8N_FORM_ORIGIN to the private address n8n's form routes are reached on,
    for example http://n8n.internal:5678.
    """
    origin = os.environ.get("N8N_FORM_ORIGIN", "").strip()
    if not origin:
        raise ReviewError("N8N_FORM_ORIGIN is not configured")
    return url_origin(urlsplit(origin))


def url_origin(parts) -> tuple[str, str | None, int | None]:
    return parts.scheme, parts.hostname, parts.port or {"http": 80, "https": 443}.get(parts.scheme)


class ReviewerStore:
    """Safe access to the RAG lifecycle folders beneath one NAS root."""

    def __init__(self, rag_root: str | os.PathLike):
        self.root = pathlib.Path(rag_root).resolve()
        self.documents = self.root / "documents"
        self.incoming = self.documents / "incoming"
        self.approved = self.documents / "approved"
        self.rejected = self.documents / "rejected"
        self.reviews = self.root / "reviews"
        self.pending_approvals = self.reviews / "pending"
        self.failures = self.reviews / "failures"
        self._scan_state: dict[str, tuple[int, int]] = {}

    def ensure_layout(self) -> None:
        for folder in (self.incoming, self.approved, self.rejected, self.reviews,
                       self.pending_approvals, self.failures):
            folder.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _clean_identifier(identifier: str) -> pathlib.PurePosixPath:
        if not identifier or "\\" in identifier:
            raise ReviewError("document must be a non-empty POSIX relative path")
        rel = pathlib.PurePosixPath(identifier)
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            raise ReviewError("document must not be absolute or contain path traversal")
        if len(rel.parts) < 2:
            raise ReviewError("document must include a collection and filename")
        if rel.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ReviewError("only parsed Docling .json files are accepted in the pilot")
        return rel

    def resolve(self, bucket: str, identifier: str, *, must_exist: bool = True) -> pathlib.Path:
        bases = {
            "incoming": self.incoming,
            "approved": self.approved,
            "rejected": self.rejected,
        }
        if bucket not in bases:
            raise ReviewError("unknown document bucket")
        base = bases[bucket].resolve()
        candidate = (base / self._clean_identifier(identifier)).resolve()
        if not candidate.is_relative_to(base):
            raise ReviewError("document escapes the configured NAS folder")
        if must_exist and not candidate.is_file():
            raise FileNotFoundError(identifier)
        return candidate

    def health(self) -> dict:
        checks = {}
        for name, folder in {
            "root": self.root,
            "incoming": self.incoming,
            "approved": self.approved,
            "rejected": self.rejected,
            "reviews": self.reviews,
        }.items():
            checks[name] = {"path": str(folder), "exists": folder.is_dir()}
        return {"status": "ok" if all(v["exists"] for v in checks.values()) else "degraded",
                "checks": checks}

    def list_incoming(self) -> list[dict]:
        """Return files unchanged across two scans and not already reviewed."""
        current: dict[str, tuple[int, int]] = {}
        ready = []
        if not self.incoming.is_dir():
            return ready
        for path in sorted(self.incoming.rglob("*.json")):
            if not path.is_file() or path.name.startswith("."):
                continue
            rel = path.relative_to(self.incoming).as_posix()
            try:
                self._clean_identifier(rel)
                stat = path.stat()
            except (OSError, ReviewError):
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            current[rel] = signature
            if self._scan_state.get(rel) != signature:
                continue
            digest = sha256_file(path)
            if (self.reviews / f"{digest}.json").is_file():
                continue
            ready.append({
                "document": rel,
                "collection": pathlib.PurePosixPath(rel).parts[0].lower(),
                "sha256": digest,
                "size": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
            })
        self._scan_state = current
        return ready

    @serialized_document
    def preflight(self, identifier: str, expected_sha256: str | None = None) -> dict:
        path = self.resolve("incoming", identifier)
        digest = sha256_file(path)
        if expected_sha256 and digest != expected_sha256.lower():
            raise ReviewError("stale SHA-256: document changed after discovery")
        result = verified_findings.assess(self.reviews, identifier, digest, path)
        result.update({
            "review_id": f"sha256:{digest}",
            "document": identifier,
            "sha256": digest,
            "checks": {
                "schema": "PASS",
                "manifest": "NOT_CHECKED",
                "text_integrity": result["status"],
                "structure": "NOT_CHECKED",
            },
            "samples": self._bounded_samples(path, result["findings"]),
            "curation": json.loads(path.read_text(encoding="utf-8")).get("rag_curation"),
        })
        return result

    @serialized_document
    def exclude_pages(self, identifier: str, expected_sha256: str, pages: list[int],
                      reason: str, confirmation: str) -> dict:
        """Explicit operator curation of INCOMING content, never approval."""
        if confirmation != "EXCLUDE PDF PAGES":
            raise ReviewError("confirmation must exactly match EXCLUDE PDF PAGES")
        if not reason.strip() or len(reason) > 1000:
            raise ReviewError("a non-empty exclusion reason of at most 1000 characters is required")
        source = self.resolve("incoming", identifier)
        original = source.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        if digest != expected_sha256.lower():
            raise ReviewError("stale SHA-256: document changed before page exclusion")
        document = json.loads(original)
        curated = exclude_pdf_pages(document, pages)
        history = document.get("rag_curation", {}).get("page_exclusions", [])
        if not isinstance(history, list):
            raise ReviewError("invalid existing curation history")
        curation = {"pdf_pages": sorted(set(pages)), "reason": reason.strip(),
                    "source_sha256": digest, "recorded_at": utc_now()}
        curated["rag_curation"] = {"page_exclusions": [*history, curation]}
        # Archive the exact input bytes BEFORE replacing the watched file.
        # This changes its hash, invalidating all prior review/ingestion receipts.
        archive = self.reviews / "page-exclusions" / digest
        archive.mkdir(parents=True, exist_ok=True)
        backup = archive / "original.json"
        if backup.exists():
            if backup.read_bytes() != original:
                raise ReviewError("exclusion archive conflicts with the source")
        else:
            with backup.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        if sha256_file(source) != digest:
            raise ReviewError("incoming document changed while archiving page exclusions")
        self._atomic_json(source, curated)
        new_digest = sha256_file(source)
        self._atomic_json(archive / f"{new_digest}.json", {
            "document": identifier, **curation, "derived_sha256": new_digest,
        })
        self.remove_approval_link(digest)
        self.resolve_failure(digest, f"Curated derivative created: {new_digest}")
        self._scan_state.pop(identifier, None)
        return {"document": identifier, "sha256": new_digest,
                "source_sha256": digest, "excluded_pdf_pages": curation["pdf_pages"],
                "status": "awaiting_fresh_review", "archived_original": str(backup)}

    @staticmethod
    def _bounded_samples(path: pathlib.Path, findings: list[dict]) -> list[dict]:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        samples = []
        seen = set()
        for finding in findings[:8]:
            key = (finding.get("page"), finding.get("snippet"))
            if key not in seen:
                seen.add(key)
                samples.append({"page": finding.get("page"),
                                "kind": "finding", "text": finding.get("snippet", "")[:500]})
        texts = [item for item in payload.get("texts", [])
                 if (item.get("text") or "").strip()]
        if texts:
            for index in sorted({0, len(texts) // 2, len(texts) - 1}):
                item = texts[index]
                page = parse_check.item_page(item)
                text = " ".join(item.get("text", "").split())[:500]
                key = (page, text)
                if text and key not in seen and len(samples) < 12:
                    seen.add(key)
                    samples.append({"page": page, "kind": "coverage", "text": text})
        return samples

    @serialized_document
    def record_review(self, identifier: str, sha256: str, record: dict) -> dict:
        path = self.resolve("incoming", identifier)
        actual = sha256_file(path)
        if actual != sha256.lower():
            raise ReviewError("stale SHA-256: document changed before review was recorded")
        deterministic = str(record.get("deterministic_status", "")).upper()
        model = str(record.get("model_status", "")).upper()
        final = str(record.get("final_status", "")).upper()
        if {deterministic, model, final} - VALID_STATUSES:
            raise ReviewError("deterministic_status, model_status, and final_status are required")
        human_approved = bool(record.get("human_approved", False))
        if deterministic == "FAIL" and final != "FAIL":
            raise ReviewError("a deterministic FAIL cannot be overridden")
        if model == "FAIL" and final != "FAIL":
            raise ReviewError("a model FAIL cannot be promoted")
        if final == "PASS" and (deterministic == "WARN" or model == "WARN") and not human_approved:
            raise ReviewError("WARN requires explicit human approval")
        if final == "PASS" and deterministic != "PASS" and not human_approved:
            raise ReviewError("automatic promotion requires deterministic PASS")

        actual_deterministic = verified_findings.assess(self.reviews, identifier, sha256_file(path), path)["status"]
        if deterministic != actual_deterministic:
            raise ReviewError(
                f"deterministic status mismatch: supplied {deterministic}, "
                f"current result is {actual_deterministic}"
            )

        canonical = {
            "review_id": f"sha256:{actual}",
            "document": identifier,
            "sha256": actual,
            **record,
            "deterministic_status": deterministic,
            "model_status": model,
            "final_status": final,
            "human_approved": human_approved,
            "curation": json.loads(path.read_text(encoding="utf-8")).get("rag_curation"),
        }
        self.reviews.mkdir(parents=True, exist_ok=True)
        destination = self.reviews / f"{actual}.json"
        if destination.exists():
            existing = json.loads(destination.read_text(encoding="utf-8"))
            if all(existing.get(key) == value for key, value in canonical.items()):
                if existing.get("human_decision") is not None:
                    self.remove_approval_link(actual)
                self._sync_failure(existing)
                return existing
            canonical["supersedes_recorded_at"] = existing.get("recorded_at")
        stored = {**canonical, "recorded_at": utc_now()}
        self._atomic_json(destination, stored)
        self._sync_failure(stored)
        if stored.get("human_decision") is not None:
            self.remove_approval_link(actual)
        return stored

    def _sync_failure(self, review: dict) -> None:
        if review.get("final_status") != "FAIL":
            self.resolve_failure(review["sha256"], "Review no longer FAIL")
            return
        self.failures.mkdir(parents=True, exist_ok=True)
        alert = {k: review.get(k) for k in (
            "document", "sha256", "recorded_at", "deterministic_status", "model_status",
            "summary", "findings")}
        alert.update(status="open", action="Inspect source; curate exclusions or fix extraction, then re-review.")
        self._atomic_json(self.failures / f"{review['sha256']}.json", alert)
        self._render_failure_inbox()

    def resolve_failure(self, digest: str, reason: str) -> None:
        path = self.failures / f"{digest}.json"
        if path.is_file():
            alert = json.loads(path.read_text(encoding="utf-8"))
            alert.update(status="resolved", resolution=reason, resolved_at=utc_now())
            self._atomic_json(path, alert)
            self._render_failure_inbox()

    def list_failures(self) -> list[dict]:
        return [alert for path in sorted(self.failures.glob("*.json"))
                if (alert := json.loads(path.read_text(encoding="utf-8"))).get("status") == "open"]

    def backfill_failures(self) -> None:
        """Surface old failed receipts only while their matching file is incoming."""
        for path in sorted(self.reviews.glob("*.json")):
            review = json.loads(path.read_text(encoding="utf-8"))
            if review.get("final_status") != "FAIL":
                continue
            try:
                source = self.resolve("incoming", review["document"])
                if sha256_file(source) == review.get("sha256"):
                    self._sync_failure(review)
            except (FileNotFoundError, ReviewError, KeyError):
                continue
        self._render_failure_inbox()

    def _render_failure_inbox(self) -> None:
        with resource_lock(f"{self.root}/failure-inbox"):
            self._write_failure_inbox()

    def _write_failure_inbox(self) -> None:
        rows = []
        for alert in self.list_failures():
            evidence = "".join(
                "<li>PDF page " + html.escape(str(f.get("page"))) + ": "
                + html.escape(str(f.get("snippet", f.get("evidence", "")))) + "</li>"
                for f in (alert.get("findings") or [])[:50])
            rows.append("<article><h2>" + html.escape(alert["document"]) + "</h2><p>"
                        + html.escape(alert.get("summary") or "Review failed")
                        + "</p><p>SHA-256: " + html.escape(alert["sha256"])
                        + '</p><p><a href="../repairs/' + html.escape(alert['sha256'])
                        + '/index.html">Open repair batch</a> (reload after retries)</p><ul>'
                        + evidence + "</ul></article>")
        content = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                   '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
                   '<title>RAG failures</title><h1>Documents blocked from ingestion</h1>'
                   '<p>Local NAS inbox; reload to refresh. No document is approved here. '
                   'Page numbers below are 1-based PDF indices, not printed page labels.</p>'
                   + ("".join(rows) or "<p>No open failures.</p>") + "</html>\n")
        self.failures.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.failures,
                                         prefix=".inbox-", suffix=".tmp", delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.failures / "index.html")

    @serialized_document
    def publish_approval_link(self, identifier: str, sha256: str, url: str) -> dict:
        """Write a private, human-visible handoff for an unapproved WARN receipt."""
        digest = sha256.lower()
        path = self.resolve("incoming", identifier)
        if sha256_file(path) != digest:
            raise ReviewError("incoming document changed before approval handoff")
        receipt_path = self.reviews / f"{digest}.json"
        if not receipt_path.is_file():
            raise ReviewError("no recorded review exists for this SHA-256")
        review = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (review.get("document") != identifier or review.get("sha256") != digest
                or review.get("final_status") != "WARN"
                or review.get("human_approved") is not False
                or review.get("human_decision") is not None):
            raise ReviewError("approval handoff requires a matching unapproved WARN")

        parsed = urlsplit(url)
        if (url_origin(parsed) != approval_origin()
                or parsed.username or parsed.password
                or parsed.fragment or not parsed.path.startswith("/form-waiting/")):
            raise ReviewError("approval URL must be the configured n8n form route")

        with resource_lock(f"{self.root}/approval-inbox"):
            self.pending_approvals.mkdir(parents=True, exist_ok=True)
            entry = {"document": identifier, "sha256": digest, "url": url,
                     "recorded_at": utc_now()}
            destination = self.pending_approvals / f"{digest}.json"
            if destination.is_file():
                existing = json.loads(destination.read_text(encoding="utf-8"))
                if existing.get("url") != url:
                    raise ReviewError("a different approval link is already pending")
            else:
                self._atomic_json(destination, entry)
            self._render_approval_inbox_unlocked()
        return {"document": identifier, "sha256": digest,
                "deterministic_status": review["deterministic_status"],
                "model_status": review["model_status"],
                "final_status": "WARN", "summary": review.get("summary", "")}

    def remove_approval_link(self, sha256: str) -> None:
        with resource_lock(f"{self.root}/approval-inbox"):
            destination = self.pending_approvals / f"{sha256.lower()}.json"
            destination.unlink(missing_ok=True)
            # Refresh even if already removed: an earlier request may have
            # deleted the JSON but failed before publishing the new HTML.
            self._render_approval_inbox_unlocked()

    def _render_approval_inbox(self) -> None:
        with resource_lock(f"{self.root}/approval-inbox"):
            self._render_approval_inbox_unlocked()

    def _render_approval_inbox_unlocked(self) -> None:
        rows = []
        for path in sorted(self.pending_approvals.glob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                # CIFS listings can briefly retain a deleted entry. Only
                # absence is benign; permission/I/O/invalid JSON still fails.
                continue
            title = html.escape(entry["document"])
            url = html.escape(entry["url"], quote=True)
            digest = html.escape(entry["sha256"])
            rows.append(f'<li><a href="{url}" rel="noreferrer">{title}</a> '
                        f'<small>SHA-256: {digest}</small></li>')
        content = ("<!doctype html>\n<html lang=\"en\"><meta charset=\"utf-8\">"
                   "<title>RAG approvals</title><h1>Pending RAG approvals</h1>"
                   "<p>Open only while connected to the home Tailscale network. "
                   "Verify the source PDF before approving; Reject is the safe default.</p>"
                   + ("<ul>" + "".join(rows) + "</ul>" if rows else "<p>No pending approvals.</p>")
                   + "</html>\n")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8",
                                         dir=self.pending_approvals, prefix=".inbox-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.pending_approvals / "index.html")

    @staticmethod
    def _atomic_json(destination: pathlib.Path, payload: dict) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix=".review-", suffix=".tmp", delete=False) as stream:
            temp = pathlib.Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(destination)

    @serialized_document
    def move_reviewed(self, identifier: str, sha256: str, destination_bucket: str) -> dict:
        if destination_bucket not in {"approved", "rejected"}:
            raise ReviewError("destination must be approved or rejected")
        review_path = self.reviews / f"{sha256.lower()}.json"
        if not review_path.is_file():
            raise ReviewError("no recorded review exists for this SHA-256")
        review = json.loads(review_path.read_text(encoding="utf-8"))
        required = "PASS" if destination_bucket == "approved" else "FAIL"
        if review.get("document") != identifier or review.get("final_status") != required:
            raise ReviewError(f"recorded review does not authorize move to {destination_bucket}")

        source = self.resolve("incoming", identifier, must_exist=False)
        destination = self.resolve(destination_bucket, identifier, must_exist=False)
        if destination.is_file():
            if sha256_file(destination) == sha256.lower():
                if destination_bucket == "rejected":
                    self.resolve_failure(sha256.lower(), "Document moved to rejected")
                return {"status": "unchanged", "document": identifier,
                        "sha256": sha256.lower(), "bucket": destination_bucket}
            raise ReviewError("destination exists with different content")
        if not source.is_file():
            raise FileNotFoundError(identifier)
        if sha256_file(source) != sha256.lower():
            raise ReviewError("stale SHA-256: document changed before promotion")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        if destination_bucket == "rejected":
            self.resolve_failure(sha256.lower(), "Document moved to rejected")
        return {"status": "moved", "document": identifier,
                "sha256": sha256.lower(), "bucket": destination_bucket}
