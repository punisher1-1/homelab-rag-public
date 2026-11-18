from __future__ import annotations

import hmac
import os
import pathlib

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, StrictInt

from conversion_core import ConversionError, ConversionManager
from ingestion_core import IngestionError, IngestionManager
from reviewer_core import ReviewError, ReviewerStore
from repair_core import RepairManager


app = FastAPI(title="RAG Reviewer", version="0.1.0")
store = ReviewerStore(os.environ.get("RAG_ROOT", "/data/rag-store"))
conversions = ConversionManager(store)
ingestions = IngestionManager(store)
repairs = RepairManager(store)


def authorize(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get("REVIEWER_API_KEY", "")
    key_file = os.environ.get("REVIEWER_API_KEY_FILE", "")
    if not expected and key_file:
        try:
            expected = pathlib.Path(key_file).read_text(encoding="utf-8").strip()
        except OSError:
            expected = ""
    if not expected:
        raise HTTPException(status_code=503, detail="reviewer API key is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid API key")


def authorize_rebuild(
    x_rebuild_key: str | None = Header(default=None, alias="X-Rebuild-Key"),
) -> None:
    expected = os.environ.get("REBUILD_API_KEY", "")
    key_file = os.environ.get("REBUILD_API_KEY_FILE", "")
    if not expected and key_file:
        try:
            expected = pathlib.Path(key_file).read_text(encoding="utf-8").strip()
        except OSError:
            expected = ""
    if not expected:
        raise HTTPException(status_code=503, detail="rebuild API key is not configured")
    if not x_rebuild_key or not hmac.compare_digest(x_rebuild_key, expected):
        raise HTTPException(status_code=401, detail="invalid rebuild API key")


class DocumentRef(BaseModel):
    document: str
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class ConversionRequest(DocumentRef):
    retry: bool = False


class RebuildRequest(BaseModel):
    confirmation: str


class ReviewRecord(DocumentRef):
    deterministic_status: str
    model_status: str
    final_status: str
    human_approved: bool = False
    human_decision: str | None = Field(default=None, max_length=30)
    reviewer_notes: str = Field(default="", max_length=1000)
    summary: str = Field(default="", max_length=1000)
    findings: list[dict] = Field(default_factory=list, max_length=50)


class ApprovalLink(DocumentRef):
    url: str = Field(max_length=2048)


class PageExclusion(DocumentRef):
    pages: list[StrictInt] = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=1000)
    confirmation: str


def call(action):
    try:
        return action()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"document not found: {exc}") from exc
    except (ReviewError, ConversionError, IngestionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.on_event("startup")
def startup() -> None:
    store.ensure_layout()
    store.backfill_failures()
    conversions.ensure_layout()
    conversions.recover_interrupted()
    ingestions.ensure_layout()
    ingestions.recover_interrupted_rebuilds()
    for alert in sorted(store.list_failures(), key=lambda a: len(a.get('findings') or [])):
        try:
            repairs.start(alert['document'], alert['sha256'])
        except (ValueError, OSError):
            # The original failure remains durable even if batch preparation fails.
            import logging
            logging.getLogger(__name__).exception('Repair batch preparation failed')


@app.on_event("shutdown")
def shutdown() -> None:
    repairs.pool.shutdown(wait=False)
    conversions.shutdown()
    ingestions.shutdown()


@app.get("/health", dependencies=[Depends(authorize)])
def health():
    result = store.health()
    result["checks"]["docling"] = conversions.health()
    result["checks"].update(ingestions.health())
    result["status"] = (
        "ok"
        if all(check.get("exists", check.get("ok", False)) for check in result["checks"].values())
        else "degraded"
    )
    return result


@app.get("/v1/raw", dependencies=[Depends(authorize)])
def raw_documents():
    return {"documents": call(conversions.list_raw)}


@app.post("/v1/conversions", dependencies=[Depends(authorize)], status_code=202)
def start_conversion(request: ConversionRequest):
    return call(
        lambda: conversions.start(request.document, request.sha256, retry=request.retry)
    )


@app.get("/v1/conversions/{conversion_id}", dependencies=[Depends(authorize)])
def conversion_status(conversion_id: str):
    return call(lambda: conversions.status(conversion_id))


@app.get("/v1/incoming", dependencies=[Depends(authorize)])
def incoming():
    return {"documents": call(store.list_incoming)}


@app.post("/v1/reviews/preflight", dependencies=[Depends(authorize)])
def preflight(request: DocumentRef):
    return call(lambda: store.preflight(request.document, request.sha256))


@app.get("/v1/reviews/alerts", dependencies=[Depends(authorize)])
def review_alerts():
    return {"failures": call(store.list_failures)}


@app.post("/v1/documents/exclude-pages", dependencies=[Depends(authorize)])
def exclude_pages(request: PageExclusion):
    return call(lambda: store.exclude_pages(
        request.document, request.sha256, request.pages, request.reason, request.confirmation
    ))


@app.post("/v1/reviews/record", dependencies=[Depends(authorize)])
def record_review(request: ReviewRecord):
    payload = request.model_dump(exclude={"document", "sha256"})
    result = call(lambda: store.record_review(request.document, request.sha256, payload))
    if result['final_status'] == 'FAIL':
        try:
            repairs.start(request.document, request.sha256)
        except (ValueError, OSError):
            import logging
            logging.getLogger(__name__).exception('Repair batch preparation failed')
    return result


@app.post('/v1/repairs', dependencies=[Depends(authorize)], status_code=202)
def start_repair(request: DocumentRef):
    return call(lambda: repairs.start(request.document, request.sha256))


@app.get('/v1/repairs/{digest}', dependencies=[Depends(authorize)])
def repair_status(digest: str):
    return call(lambda: repairs.status(digest))


@app.post("/v1/reviews/approval-link", dependencies=[Depends(authorize)])
def publish_approval_link(request: ApprovalLink):
    return call(lambda: store.publish_approval_link(
        request.document, request.sha256, request.url
    ))


@app.post("/v1/documents/promote", dependencies=[Depends(authorize)])
def promote(request: DocumentRef):
    return call(lambda: store.move_reviewed(request.document, request.sha256, "approved"))


@app.post("/v1/documents/reject", dependencies=[Depends(authorize)])
def reject(request: DocumentRef):
    return call(lambda: store.move_reviewed(request.document, request.sha256, "rejected"))


@app.post("/v1/ingestions", dependencies=[Depends(authorize)], status_code=202)
def start_ingestion(request: DocumentRef):
    return call(lambda: ingestions.start(request.document, request.sha256))


@app.get("/v1/ingestions/{ingestion_id}", dependencies=[Depends(authorize)])
def ingestion_status(ingestion_id: str):
    return call(lambda: ingestions.status(ingestion_id))


@app.post("/v1/rebuilds", dependencies=[Depends(authorize_rebuild)], status_code=202)
def start_rebuild(request: RebuildRequest):
    return call(lambda: ingestions.start_rebuild(request.confirmation))


@app.get("/v1/rebuilds/{rebuild_id}", dependencies=[Depends(authorize_rebuild)])
def rebuild_status(rebuild_id: str):
    return call(lambda: ingestions.rebuild_status(rebuild_id))
