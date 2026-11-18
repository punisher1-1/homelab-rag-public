# RAG reviewer service

Operator quick reference: **RAG Document Intake and Repair Runbook** in
`Local_AI_Vault/wiki/concepts/rag-document-intake-and-repair-runbook.md`.
Use that checklist for routine intake, NAS reports, reviewed decisions, and
completion checks. This README also contains historical deployment checkpoints;
their earlier document statuses are not current status reports.

Private API used by n8n to review parsed Docling JSON before promotion into the
approved corpus. The service accepts document identifiers relative to
`documents/incoming`; arbitrary host paths are rejected.

The same service is also the trusted bridge to private Docling Serve. Raw source
files live beneath `documents/raw/<collection>/`; n8n queues conversions using
only a relative identifier and full SHA-256. The bridge uploads the source directly
to Docling over the private Compose network and atomically publishes the returned
DoclingDocument JSON into `documents/incoming/<collection>/`.

The container is intentionally not published on a VM port. In the n8n Compose
project, n8n reaches it at `http://rag-reviewer:8110` over `n8n_default`.

## Deployment layout on the n8n host

```text
/opt/n8n/docker-compose.yml
/opt/n8n/compose.rag-reviewer.yaml
/opt/n8n/rag-reviewer-src/
/mnt/rag-files/rag-store/        # host CIFS mount
```

Generate `/opt/n8n/reviewer-api.key` as a mode-`0600` random secret. This is an
application key, not the NAS password, and Compose mounts it only into the
reviewer container. Store the same value in an n8n Header Auth credential as
`X-API-Key`.

Copy only the existing ParadeDB password into `/opt/n8n/paradedb-password` and
set its mode to `0600`. Compose mounts it only into the reviewer container. The
worker connects directly to ParadeDB at `paradedb-host:5432` and to the bge-m3
embedder at `embed-host:11434`; neither credential nor service is exposed to
the n8n workflow.

Start or update the combined project with:

```bash
docker compose -f docker-compose.yml -f compose.rag-reviewer.yaml up -d --build docling-serve rag-reviewer
```

Generate `/opt/n8n/docling-api.key` as a second mode-`0600` random secret. The
pinned Docling CPU image has no published host port, no NAS mount, one conversion
worker, a 10 GB memory ceiling, and a six-vCPU ceiling.

Generate `/opt/n8n/rebuild-api.key` as a separate mode-`0600` secret mounted only
into the reviewer. It is deliberately absent from n8n, so the normal workflow
credential cannot invoke the destructive rebuild endpoints.

The conversion API is deliberately asynchronous at the bridge boundary:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/v1/raw` | Discover supported files stable across two scans |
| `POST` | `/v1/conversions` | Queue one path-and-SHA-verified conversion |
| `GET` | `/v1/conversions/{sha256}` | Read the durable receipt |

Completed receipts are stored beneath `rag-store/conversions`. A failed receipt
is not automatically retried; send the same request with `retry: true` after the
cause is corrected. The raw source remains on the NAS as the durable original.
If the reviewer restarts during an active conversion, startup marks that receipt
`interrupted`; the next two-scan discovery automatically queues exactly one new
attempt while ordinary Docling failures still require an explicit retry.

To submit new material, place it under a collection folder, for example:

```text
/mnt/rag-files/rag-store/documents/raw/bentley-manuals/manual.pdf
```

The active `RAG - Convert Raw Documents` workflow checks this folder every five
minutes. Successful output appears as
`documents/incoming/bentley-manuals/manual.json`, where the intake/review workflow
picks it up on its normal two-scan cycle.

Operational checks:

```bash
cd /opt/n8n
docker compose -f docker-compose.yml -f compose.rag-reviewer.yaml ps
docker logs --tail 100 docling-serve
docker stats --no-stream docling-serve rag-reviewer n8n n8n-postgres
```

Rollback leaves NAS originals and receipts intact:

```bash
cd /opt/n8n
cp compose.rag-reviewer.yaml.pre-docling compose.rag-reviewer.yaml
docker compose -f docker-compose.yml -f compose.rag-reviewer.yaml up -d --build rag-reviewer
docker stop docling-serve
```

## Test-lab corpus rebuild

The legacy `ingest.py --reset` still works, but a Mac-side run records
`/Volumes/...` source paths and does not reconcile durable ingestion receipts.
Use the reviewer-managed rebuild for a clean test reset with canonical identities:

```bash
docker exec rag-reviewer python request_rebuild.py \
  --confirm "TRUNCATE AND REBUILD CHUNKS"
```

This command is intentionally destructive to the ParadeDB `chunks` table only.
It refuses an empty approved store, serializes behind any active ingestion,
archives top-level receipts beneath
`rag-store/ingestions/archive/<rebuild-id>/`, truncates and resets the table
identity, then re-ingests every approved document as `rag://approved/...`.
Documents, conversions, reviews, and archived receipts remain untouched. A final
rebuild receipt is written beneath `rag-store/rebuilds/`; any failed document makes
the overall rebuild fail visibly rather than reporting a partial corpus as healthy.

The pilot accepts `.json` Docling exports only. Files must retain identical size
and modification time across two `/v1/incoming` scans before they are returned.

### Re-review after a verified false positive

A recorded deterministic `FAIL` intentionally prevents rediscovery and cannot be
overridden by clicking Approve. After fixing and testing the preflight checker,
deploy the updated reviewer image and verify that the unchanged incoming JSON
now reports `WARN` or `PASS`. Then archive the old failed receipt and requeue it:

```bash
docker exec rag-reviewer python replay_review.py \
  "collection/document.json" \
  "SHA256_FROM_FAILED_REVIEW_RECEIPT" \
  --confirm "ARCHIVE FAILED REVIEW AND REQUEUE"
```

The command verifies the full SHA-256, incoming path, failed receipt, and current
preflight before moving the receipt into `reviews/archive/<sha256>/`. It does not
promote or ingest the document. The normal n8n intake workflow rediscovers it
after two matching scans; a `WARN` still requires explicit human approval. If
preflight remains `FAIL`, the receipt stays in place and nothing is requeued.

### Private human-approval inbox

For a `WARN`, the intake workflow publishes its runtime form URL through
`POST /v1/reviews/approval-link` before entering the Wait node. The reviewer
accepts only a matching, unapproved review receipt and an n8n URL on the n8n host's
Tailscale address. It writes a clickable inbox at
`rag-store/reviews/pending/index.html` on the NAS. Open that file from a
Tailscale-connected client and sign in with the n8n Wait form's Basic Auth
credential. The inbox is not a push notification or a pop-up.

The form defaults to Reject. Recording either human decision removes its link
from the inbox. Do not publish a workflow with the Wait form set to `None`
authentication. The Basic Auth credential must be created in n8n by the
operator and selected on the Wait node; its password never belongs in the
workflow export, repository, or NAS.

To replay an already-recorded unapproved `WARN` through a newly fixed approval
workflow, first stop its old waiting n8n execution. Then, after deploying the
new reviewer and publishing the authenticated workflow, run:

```bash
docker exec rag-reviewer python replay_review.py \
  "collection/document.json" \
  "FULL_SHA256_FROM_WARNING_RECEIPT" \
  --confirm "ARCHIVE UNAPPROVED WARNING AND REQUEUE"
```

The command refuses an approved review, stale source hash, or a current
preflight `FAIL`. It archives the old receipt and removes any stale inbox link;
two stable intake scans then create a new review. It does not approve, promote,
or ingest anything by itself.

After promotion, `POST /v1/ingestions` queues exactly one approved document.
Only one writer runs at a time. `GET /v1/ingestions/{sha256}` returns a durable
receipt stored beneath `rag-store/ingestions`; a job is successful only after a
matching SHA and non-zero chunk count are verified in ParadeDB.

`GET /health` verifies the NAS layout, a real ParadeDB login, and availability
of the configured embedding model. It returns `degraded` if any dependency is
unavailable without exposing connection secrets.

## Failed-review inbox (NAS, no push notification)

Every `record_review` with final status `FAIL` now atomically writes a failure
entry and refreshes `rag-store/reviews/failures/index.html`. This includes the
existing n8n **Record deterministic failure** branch: no workflow replacement or
new credential is needed. Startup backfills older FAIL receipts whose unchanged
document is still incoming. `GET /v1/reviews/alerts` returns the open entries to
authenticated private clients. This inbox does not cover transport failures that
prevent a review receipt from being recorded, conversion errors, or ingestion
errors; those still have their separate receipts/n8n execution errors.

Open `/Volumes/RAG_Files/rag-store/reviews/failures/index.html` on the Mac and
reload to refresh. It shows the document, exact hash, summary, and flagged PDF
page/evidence, HTML-escaped and without external scripts or resources. It cannot
approve documents. Failed content remains blocked. Replaying a corrected review,
creating a curated derivative, or moving the document to rejected resolves the
old alert but retains its JSON record.

## Store overview page (NAS, snapshot)

`store_overview.py` writes `rag-store/OVERVIEW.html`: every reviewed document
by name with its current state (ingested, ingest running or failed, blocked,
waiting for approval, rejected), a "Needs you" list with links to repair
batches and the approval inbox, approved files that predate the reviewer, raw
PDFs and their conversions, and a guide to what each folder is for. It also
surfaces ingestion errors, which the failures inbox does not. It is read-only
apart from that one page and uses only the standard library:

```bash
python3 rag-reviewer/store_overview.py /Volumes/RAG_Files/rag-store
```

It is a snapshot; rerun it to refresh. Folder names stay as they are because
the reviewer code depends on them.

## Explicit page exclusions for example forms

There is deliberately no automatic "skip all forms" rule. An operator chooses
the original **1-based PDF indices**, not printed page labels, and gives a reason.
The exclusion command accepts only current **incoming** JSON plus its full hash.
It creates a curated derivative by physically removing text, tables, pictures,
and their children from the selected pages and rewriting Docling references.
Both quality checks and chunking therefore see exactly the same retained
content; arbitrary flags embedded in a source document cannot suppress checks.
Cross-page items that mix excluded and retained content are refused for manual
region review. Original page indices and printed-page citations stay intact;
curated prose chunks do not cross page boundaries.

The exact original JSON is archived at
`reviews/page-exclusions/<old-sha>/original.json`; a separate record binds the
reason, page list, timestamp, and old/new hashes. Raw PDFs are never modified.
Existing review receipts are retained but cannot authorize the new hash. The
derivative goes through fresh discovery/review, not automatic approval. Curation
metadata is carried into review receipts and searchable chunk metadata.

After deploying the updated reviewer, first inspect the current hash:

```bash
docker exec rag-reviewer python -c \
  'from reviewer_core import ReviewerStore; import os,json; s=ReviewerStore(os.environ["RAG_ROOT"]); print(json.dumps(s.preflight("payroll/2026 California Employer Guide.json"), indent=2))'
```

Then use that **JSON hash**, not the PDF hash or an old UI comparison hash:

```bash
docker exec rag-reviewer python exclude_pages.py \
  "payroll/2026 California Employer Guide.json" CURRENT_INCOMING_JSON_SHA256 \
  --pages 67 76 77 \
  --reason "Illustrative completed forms; sample identities and amounts are not reference content." \
  --confirm "EXCLUDE PDF PAGES"
```

The authenticated equivalent is `POST /v1/documents/exclude-pages` with
`document`, `sha256`, `pages`, `reason`, and `confirmation`. It is not wired to
an LLM or automatic n8n decision. API threads and container operator commands
serialize curation/review/promotion for the same identifier. Run operator commands
inside the reviewer container, not directly against the NAS from another host.
Restore only from the archived original after checking that the derivative has
not been promoted/ingested; do not overwrite an active approved file.

Local verification (does not contact the embedder or DB):

```bash
python -m unittest discover -s rag-reviewer -p 'test_*.py'
python -m unittest discover -s paradedb-personal-rag -p 'test_*.py'
```

Deployment: preserve the existing image/source snapshot, update only the source
files listed in `rag-reviewer/Dockerfile`, and rebuild/recreate **rag-reviewer
only** with the current Compose files. Do not redeploy Docling, n8n, or the DB,
and do not reset the corpus. Verify reviewer startup and the NAS inbox before
applying exclusions. Rollback to the saved source/image leaves originals and
curation archives intact; curated JSON remains physically filtered even on the
previous reviewer. Page 79's verified `R&TC` abbreviation has a narrow checker
exemption; actual `&inish` substitutions still fail.

### Home-lab deployment checkpoint — 2026-10-12

Deployed to `n8n-host`, reviewer only. The existing Python
runtime/dependencies were preserved by a source-only overlay of the saved image,
not the floating-base clean build. Reproduction/rollback assets are on the VM:

- Source and Compose backup: `/opt/n8n/reviewer-backup-20261012.oJ1vHI/`.
- Saved image: `n8n-rag-reviewer:pre-page-exclusions-20261012`.
- Overlay recipe: `runtime-preserved.Dockerfile` in that backup directory;
  build it against `/opt/n8n/rag-reviewer-src` and tag `n8n-rag-reviewer:latest`.
- A future ordinary Compose build still resolves the floating base/dependency
  ranges; treat that as a separate runtime upgrade and validate it explicitly.

Authenticated health passed; the alerts endpoint refused unauthenticated access.
52 local tests and 38 tests present in the live source tree passed. California's
incoming JSON was curated using original PDF pages 67, 76, and 77. Original JSON
hash `dc8cdc67030684a19d245df6340d148df20abdc64b6dd6ecafe00b560fd04bdc`
is archived; derivative hash is
`1b0e8ec32e504557d1290e69454accd7bd20167f08811a593be07a4da1bf95de`.
The raw PDF hash was unchanged, retained-content preflight passed, and the actual
chunker produced 2,099 candidate chunks with no excluded-page citations (no
embedding/database write in that check). At this checkpoint fresh scheduled
review and ingestion were pending; do not confuse preflight PASS with ingestion.
Texas remained blocked with six findings, visible in `reviews/failures/index.html`.

## Repair batches (first version, deployed 2026-10-12)

New recorded FAILs and existing open failures automatically create
`reviews/repairs/<incoming-json-sha>/batch.json` and `index.html`. The existing
failure inbox links to each report. No n8n workflow edits or NAS access for n8n
are needed. Reports have no external scripts, network calls, or credentials.
Open the HTML locally from the NAS in your normal browser. Page images and
bounding-box provenance help the human compare suggestions against the source.

The private reviewer queues sequential, forced-RapidOCR retries for only flagged
PDF pages. The raw PDF must match a successful conversion receipt's full hash
and parsed-output identifier/hash (or a preserved curation ancestor archive).
An origin filename alone is not trusted. Missing sources, API failures, remaining
damage, and cross-page items remain visible for manual action. All text-integrity
findings are in one report, but at most eight pages are retried per invocation;
deferred pages are shown. Continue with:

```bash
docker exec rag-reviewer python repair_batch.py retry INCOMING_JSON_SHA256
```

No automatic retry suggestion is applied, including a checker-passing candidate.
In the report choose **Keep blocked / unchanged**, **Verified correct — accept
original as-is**, **Replace with reviewed text**, or **Exclude this entire item**
for each finding. Edit replacement text if needed,
give a reason, and export `repair-decisions.json`. The report is a local decision
editor, not an approval endpoint; exporting does not mutate any corpus file.
On the Mac, transfer the downloaded decisions file (adjust its name if the
browser appended a suffix):

```bash
scp "$HOME/Downloads/repair-decisions.json" user@n8n-host:/tmp/repair-decisions.json
```

On n8n-node, apply through stdin. **Do not use `docker cp` into this read-only
container.**

```bash
docker exec -i rag-reviewer python repair_batch.py apply /dev/stdin \
  --confirm "APPLY REVIEWED REPAIR BATCH" < /tmp/repair-decisions.json
```

The apply operation verifies the input hash and item identities, archives exact
original bytes before modifying the incoming JSON, retains original OCR in `orig`
for replacements, reindexes references for exclusions, and records changes/reason
in `rag_curation.repairs`. Changed content requires fresh normal review and a new
successful ingestion receipt. Keeping a damaged item unchanged can leave the new
document blocked. This is deliberately not partial-document ingestion.
`awaiting_fresh_review` means the decisions were applied, not that the document
was approved or ingested. Wait for the new hash's ingestion receipt to report
`succeeded` with a positive `chunk_count`, then test retrieval. A green n8n
execution alone can be an empty discovery run.

Scope limits: this version covers the current **text-integrity checker**, not
automated table/figure completeness detection or LLM visual judging. Table/form
cell edits and replacements of cross-page blocks are refused for structural/region
review; an explicitly selected entire cross-page text item can be excluded.
Full page evidence is provided, not an automatic crop-based visual editor.
There is no automatic approval or document reset.

Runtime/source rollback backup is
`/opt/n8n/reviewer-backup-repair-batches-20261012/`; saved image is
`n8n-rag-reviewer:pre-repair-batches-20261012`. The overlay recipe there preserves
the prior dependency runtime. Only rag-reviewer was recreated. Texas's six-item
batch completed targeted OCR; three candidates passed the text checker and three
still require manual review. Its incoming JSON hash remained unchanged; no repair
decisions were applied during deployment testing. Browser automation could not
open the local-file report due to its URL policy, so interactive export still
needs a human browser check.

## Verified correct (accept as-is)

The repair report includes **Verified correct — accept original as-is** for
false-positive text-integrity findings. Compare the original item with the source
before choosing it. The replacement textbox is ignored for this action. Export
and apply decisions with the existing `repair_batch.py apply /dev/stdin` process;
export alone does not authorize any change.

Original text, OCR, and provenance remain intact. Operator-written receipts in
`reviews/verified-correct` bind acceptance to the derived document SHA-256,
document identifier, exact item and checker findings. Embedded input metadata
cannot authorize an exception. Any document change or changed checker finding
requires verification again. Keep blocked is still the default; other findings
remain active. Acceptance triggers fresh normal review, not automatic approval
or ingestion, and does not override a model FAIL. Raw standalone parse checks
still report the original findings because they do not load operator receipts.

## Approval-inbox cleanup recovery (2026-10-13)

Approval recording persists the review before removing its pending-form link.
A missing `reviews/pending/<sha>.json` previously could produce a 404 during
cleanup, leaving a saved PASS in incoming without promotion or ingestion.
Approval inbox publication/deletion/rendering now shares a process-safe lock;
deletion is idempotent, stale directory entries are skipped, and repeat cleanup
refreshes the HTML even when the pending JSON is already absent. Permission,
I/O, and malformed-JSON errors are not silently swallowed.

If this occurs on an older image, first deploy the fix, verify the saved review
and unchanged input hash, then resume the failed step. Do not reapprove content,
erase the review receipt, or reset the corpus. The same authenticated review,
promotion, and ingestion endpoints can recover a specifically authorized document
when execution retry is unavailable; preserve the recorded human decision.
Historical n8n failures remain historical and do not describe the recovered job.

The Proxmox recovery replayed its exact saved approval twice (HTTP 200), promoted
the same hash into approved, and started ingestion. Check the durable receipt for
completion; starting a job is not ingestion success. Source/image rollback:
`/opt/n8n/reviewer-backup-approval-cleanup-20261013/` and
`n8n-rag-reviewer:pre-approval-cleanup-20261013`. Only reviewer code changed;
dependencies and other services were retained. Local tests: 64 reviewer + 13
parser/manifest; candidate-container tests: 64 reviewer tests.
