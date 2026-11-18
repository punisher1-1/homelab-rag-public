# ParadeDB RAG Gateway (Parade-API)

An OpenAI-compatible API that sits in front of your generation backend and forces
every chat turn through **hybrid retrieval** from [[paradedb|ParadeDB]] first.
Open WebUI connects to *this* — not to the backend directly — so it becomes a chat
box over your personal knowledge base.

```
Open WebUI ──► paradedb-rag-api (gateway-host:8100) ──┬─► embed (bge-m3, embed-host)
                                                 ├─► retrieve (ParadeDB, paradedb-host)
                                                 └─► generate (GEN_URL — vLLM :8000
                                                               or Unsloth Studio :8889)
```

## Choosing a generation backend

The gateway only ever calls `GET /v1/models` and `POST /v1/chat/completions`, so
**any OpenAI-compatible server works**. Set `GEN_URL` (+ `GEN_API_KEY` if the
backend wants one) and restart. Both options live on the gpu-node and share one
16 GB card, so run one at a time.

| | vLLM (`:8000`) | Unsloth Studio (`:8889`) |
|---|---|---|
| Model swap | Fixed at `docker run`; re-create the container | **Load a different model in the web UI** — the gateway follows on the next message |
| Context length | `--max-model-len` at launch | Per-model setting in the UI |
| Auth | none as we run it → leave `GEN_API_KEY` blank | **bearer key required** (`sk-unsloth-…`) |
| Speed | faster steady-state (prefix caching reuses the KV for the repeated system prompt + retrieved chunks) | slower per token, far faster to change models |

Use Unsloth when the question is *"which model answers my notes best?"*; use vLLM
once you've picked one and want throughput.

## Files

| File | Role |
|------|------|
| `app.py` | The FastAPI gateway: `/health`, `/v1/models`, `/v1/chat/completions` (streaming). |
| `Dockerfile` / `docker-compose.yml` | Build + run as a container on the gateway host. |
| `.env` | Connection details + retrieval knobs (copy from `.env.example`). |

## Deploy on the gateway host

### 1. Get the files onto the container
From the Mac (if SSH is enabled on the LXC):
```bash
scp -r "…/Homelab:AI/paradedb-rag-api" root@gateway-host:/root/
```
Or from the Proxmox host, push into the container (replace <ctid>):
```bash
pct push <ctid> /path/to/paradedb-rag-api.tar.gz /root/paradedb-rag-api.tar.gz
```

### 2. Install Docker inside the LXC
```bash
pct enter <ctid>                    # from the Proxmox host
apt update && apt install -y docker.io docker-compose-v2
systemctl enable --now docker
docker run --rm hello-world         # confirm Docker-in-LXC works
```

### 3. Configure and launch
```bash
cd /root/paradedb-rag-api
cp .env.example .env
nano .env                           # DB_PASSWORD + GEN_URL / GEN_API_KEY
docker compose up -d --build
```

> Changing only `.env` needs `docker compose up -d` (no `--build`); changing
> `app.py` needs `--build`.

### 4. Verify — this is the moment of truth
```bash
curl -s http://localhost:8100/health | python3 -m json.tool
```
You want all three green:
```json
{ "paradedb": "ok (1903 chunks)", "embeddings": "ok",
  "generation": "ok (http://gen-host:8889/v1 — model ...)" }
```
If `generation` fails, read the message — it distinguishes the three real causes:

- **`401` / `Unauthorized`** → Unsloth Studio wants `GEN_API_KEY`, or the key is stale.
- **`returned no models`** → Studio is up but has **nothing loaded**. Load a model in
  the UI (it only advertises loaded models), or pin `GEN_MODEL`.
- **connection refused** → the backend isn't running. Studio is a tmux foreground
  process with no restart backstop (a gpu-node reboot kills it); vLLM stays down
  after a manual `docker stop` until you `docker start vllm`.

Test the backend directly to isolate the gateway from the backend:
```bash
curl -s http://gen-host:8889/v1/models -H "Authorization: Bearer sk-unsloth-…"
```

### 5. Connect Open WebUI
In Open WebUI → **Admin Settings → Connections → OpenAI API → +**:
- **Base URL:** `http://gateway-host:8100/v1`
- **API Key:** anything non-empty (this gateway ignores it)

Save, then pick **paradedb-rag** from the model dropdown and ask a question. Answers
come back grounded in your notes with a **Sources** list appended.

## Tuning (all in `.env`, no code changes)

- `MIN_SIM` — the vector-leg cosine floor. `0.0` (default) = no floor, the whole
  point of leaving Omni. Raise it only if you want to *tighten* recall on purpose.
- `TOP_K` — how many chunks are fed to the model (default 6). More context vs. more
  focus. Note the budget: 6 chunks × ~1200 chars + the system prompt ≈ 3–4K tokens
  of prompt *before* the answer, so the backend's context window needs headroom
  above that — 8K minimum, 16K comfortable.
- `GEN_URL` / `GEN_API_KEY` — which backend answers. This is the one-line flip
  between vLLM and Unsloth Studio.
- `GEN_MODEL` — leave blank to auto-detect whatever the backend is serving; pin it
  if it advertises several and you want a specific one.
- `DISCLAIM_GUARD` — `warn` (default) · `suppress` · `off`. See below.

## Provenance guard — `DISCLAIM_GUARD` (added 2026-08-09)

**The problem it fixes.** The Sources list is built from what *retrieval* returned.
It has no relationship to what the model actually wrote. So when the model says
*"this came from my general knowledge, not the provided context"*, the gateway was
still printing real, page-accurate citations directly above that sentence —
observed 2026-08-09 on a NIST AI 100-1 turn, where the block was **topically
perfect and provenance-false**. A plausible citation is more dangerous than an
absurd one.

**What it does.** After the answer is complete (both streaming and non-streaming),
the gateway scans it for the model's own statements about sourcing and reconciles
the block with them. Two tiers, because *ungrounded* and *incomplete* are different
claims and only one makes the citations wrong:

| Tier | The model said | `warn` (default) | `suppress` |
|---|---|---|---|
| **external** | this came from training / general knowledge | Warning banner, block relabelled **Retrieved (not cited)** | Block dropped, one line explains why |
| **absence** | part of what you asked isn't in the passages | Softer "partially grounded" note, **citations kept** | **Citations kept** — never suppressed |

The absence tier exists because of a near-miss: the first version of this guard was
single-tier, and it classified the **best** logged turn in the project — a refusal
that delivered two RMF functions with real citations and explicitly declined the
other two — as a violation, and would have stripped its perfectly good citations.
`test_guard.py` locks that case in.

**Limits, stated plainly.** This is a guard, not a groundedness detector. It fires
only when the model *tells you* the answer is ungrounded. It cannot catch ungrounded
output that arrives with no disclaimer, and it is not trying to — that needs a real
groundedness scorer (Ragas-style faithfulness, offline first). It also does not
catch the 2026-08-05 *"you will need to obtain the full document"* failure, where
the model stated a true fact about its context as a false fact about the world;
that case is in `test_guard.py` as a known gap.

**Every trigger is logged** — `docker logs paradedb-rag-api | grep '\[guard\]'`
gives a countable rate. That number is the point: it is the baseline for the
`check: disclaim-then-deliver` eval type, and it is the same detector, deliberately,
so the thing that gates the UI is the thing that gets scored.

```bash
python3 test_guard.py     # no deps, no running gateway, no DB
```

## Model-authored citation lists — `STRIP_MODEL_SOURCES` (added 2026-08-09)

`DISCLAIM_GUARD` governs the block **the gateway appends**. It has no authority over
text the model generates — and the model generates citation lists too.

Observed the same day, asked how long a PowerPoint presentation should be (a question
with zero support anywhere in an ATF/NIST corpus): the model wrote **two** `Sources:`
lists of its own, one attributing presentation-length advice to an *ATF Paperwork
Reduction Act notice*, and printed both **above** the gateway's correctly-labelled
block. It learned the format from `SYSTEM_PROMPT`'s *"cite the note titles you used"*
and now reproduces it whether or not it used anything.

Net effect before this fix: the honest, guarded block sat underneath two dishonest
ones, and the dishonest ones came first.

Two changes, belt and braces:

1. **`STRIP_MODEL_SOURCES=true`** (default) — the gateway discards any `Sources:`
   heading and the run of list items following it. The run ends at the first
   non-list, non-blank line, which is load-bearing: on the observed turn the model
   put its honest *"not sourced from the provided context"* sentence **between** the
   two fake blocks, and that sentence must survive — it is what the provenance guard
   matches on.
2. **A `SYSTEM_PROMPT` line** telling it not to write one. Inline locators
   (`§3.2, p. 17`) are still requested; only the trailing block is banned.

**Trade-off:** streaming becomes **line-buffered** rather than token-buffered, since a
citation line can only be recognised once complete. At most one incomplete line is
ever held. Set `STRIP_MODEL_SOURCES=false` to get token-by-token streaming back.

`test_guard.py` verifies streaming and non-streaming produce byte-identical output at
1, 3, 7, 13 and 64-character chunk boundaries — token boundaries are arbitrary and
will land mid-heading.

## Abstention floor — `ABSTAIN_MIN_SIM` / `ABSTAIN_MIN_RERANK` (added 2026-08-09)

`MIN_SIM=0.0` plus RRF fusion means the pipeline **always** returns a top-k. There is
no "nothing matched" state. Asked *how long should a PowerPoint presentation be* —
a question with zero support anywhere in an ATF/NIST corpus — retrieval returned ten
chunks of Paperwork Reduction Act notices and firewall audience sections, and the
gateway printed them as Sources. Three separate turns failed this way in one day.

**This does not filter retrieval.** `MIN_SIM` stays `0.0`; the model still receives
every chunk it would have received. What the floor gates is the **claim of
grounding** — below it, the Sources block is replaced with an honest note and the
system prompt tells the model its passages are probably irrelevant. Retrieval
behaviour is unchanged; only the assertion about it changes.

### Which score, and which one is useless

| Signal | Range | Usable as a threshold? |
|---|---|---|
| `top_rerank` — cross-encoder | model-specific, calibrated | **Best.** Actually reads query against passage. Wins when set. |
| `top_sim` — best cosine | `[-1, 1]`, comparable across queries | **Yes.** Always available; the vector leg always runs. |
| RRF fused score | `1/(60+rank)` sums | **No — and this is the trap.** It encodes *position*, not relevance. The top hit on a hopeless query scores identically to the top hit on a perfect one. Thresholding it would look reasonable and measure nothing. |

### Ship the instrument before the threshold

**Both floors default to off**, and every turn logs its scores anyway:

```
[floor] top_sim=0.2841 top_rerank=None n=10
```

Collect real queries for a few days, compare the numbers on turns that worked
against turns that didn't, then pick a floor from the distribution. Setting a
threshold by taste on day one would silently break good answers, and you would have
no baseline to notice it.

```bash
docker logs --since 24h paradedb-rag-api | grep '\[floor\]'
```

## Retrieval-only search — `POST /v1/search` (added 2026-10-16)

For tool-calling clients that write their own answer, such as the local model harness
in the harness (its `rag_search` tool). It runs exactly what a chat turn runs — hybrid
retrieve, dedup, rerank, `RAG_LAB` scope and the abstention floor — and returns the
passages instead of generating:

```bash
curl -s http://gateway-host:8100/v1/search -H "Authorization: Bearer $SEARCH_TOKEN" \
  -H "Content-Type: application/json" -d '{"query": "sequential file", "collection": "nist"}'
```

Body: `query` (1–1000 chars), optional `collection` (an indexed tag, see below), optional
`source` (`vault` or `dropzone`), optional `top_k` (1–`TOP_K`). Valid collections are the tags present in the index
(each approved folder's lowercased name, or its manifest's `collection:`), read from
the database and cached for five minutes, so a new folder needs no `.env` change;
`COLLECTIONS` only controls the Open WebUI model variants. Each result has `rank`,
`title`, `source_path`, `locator` (the same `locator()` the Sources block uses),
`collection`, `type`, `status`, `text` (ingest label stripped, capped at
`SEARCH_TEXT_CHARS`) and `truncated`. The response adds `weak` (the floor's verdict),
`top_similarity`, `top_rerank` and the indexed `collections`.

The endpoint is **off until `SEARCH_TOKEN` is set** and then requires it as a bearer
token. Chat is unauthenticated on the LAN, but search returns raw passages to any
caller, so it is not left open. Errors are fixed codes (`unauthorized`,
`invalid_query`, `unknown_collection`, `invalid_top_k`, `retrieval_failed`). Logs carry
counts and scores, never the query or passage text. Tests: `python3 -m pytest -q test_search.py`.

## Notes

- **Docker-in-LXC AppArmor:** if the container dies with exit-243, uncomment the
  `security_opt: apparmor=unconfined` block in `docker-compose.yml` — the same gotcha
  documented for the ai-frontend LXC.
- **Native alternative (no Docker):** `pip install -r requirements.txt` then
  `uvicorn app:app --host 0.0.0.0 --port 8100`, wrapped in a systemd unit. Lighter,
  but Docker matches the rest of the lab.
- This gateway is stateless — destroy/recreate freely. All data lives in ParadeDB.
