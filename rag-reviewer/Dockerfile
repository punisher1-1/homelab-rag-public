FROM python:3.12-slim

RUN groupadd --gid 1000 reviewer \
    && useradd --uid 1000 --gid 1000 --create-home reviewer

WORKDIR /app
COPY rag-reviewer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY paradedb-personal-rag/parse_check.py ./parse_check.py
COPY paradedb-personal-rag/parse_check_words.txt paradedb-personal-rag/parse_check_words.COPYRIGHT ./
COPY paradedb-personal-rag/ingest.py ./ingest.py
# ingest.py imports it; it needs pymupdf + fonttools from requirements.txt.
COPY paradedb-personal-rag/code_lines.py ./code_lines.py
COPY paradedb-personal-rag/corpus_manifest.py ./corpus_manifest.py
COPY paradedb-personal-rag/migrate_full_sha.py ./migrate_full_sha.py
COPY rag-reviewer/reviewer_core.py rag-reviewer/conversion_core.py rag-reviewer/ingestion_core.py rag-reviewer/app.py rag-reviewer/request_rebuild.py rag-reviewer/replay_review.py rag-reviewer/page_exclusions.py rag-reviewer/exclude_pages.py ./

USER 1000:1000
COPY rag-reviewer/repair_core.py rag-reviewer/repair_batch.py rag-reviewer/verified_findings.py ./
EXPOSE 8110
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8110"]
