#!/usr/bin/env python3
"""Write a plain-English overview of the NAS rag-store.

Read-only except for the one page it writes: rag-store/OVERVIEW.html. It lists
every reviewed document by name (not SHA-256), where it is in the pipeline,
and what, if anything, needs a human. It is a snapshot: rerun it to refresh.

    python3 store_overview.py /Volumes/RAG_Files/rag-store      # Mac
    docker exec rag-reviewer python store_overview.py /data/rag-store

Standard library only, so it runs on the Mac without the reviewer's packages.
"""
from __future__ import annotations

import argparse
import html
import json
import pathlib
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone

BUCKETS = ("incoming", "approved", "rejected", "superseded")

FOLDER_GUIDE = [
    ("documents/raw/", "Original PDFs you drop in. n8n converts them with Docling."),
    ("documents/incoming/", "Docling .json conversions waiting for review."),
    ("documents/approved/", "Passed review. These are what gets ingested."),
    ("documents/rejected/", "You rejected them."),
    ("documents/superseded/", "Replaced by a newer version."),
    ("documents/reference-fonts/", "Fonts the parse check uses. Leave alone."),
    ("reviews/&lt;code&gt;.json", "One review record per document version. The reviewer uses these "
     "to remember what it already checked. Do not delete: the document would be reviewed again."),
    ("reviews/failures/", "The blocked-documents page and one alert per failure."),
    ("reviews/repairs/&lt;code&gt;/", "Repair batch page for an open failure."),
    ("reviews/pending/", "Inbox of documents waiting for your approval."),
    ("reviews/verified-correct/", "Lines you marked “verified correct”."),
    ("reviews/page-exclusions/", "Backups made when pages were excluded."),
    ("reviews/archive/", "Old review records set aside for a re-review."),
    ("conversions/, ingestions/, rebuilds/", "Job records for PDF→JSON conversion, ingest and rebuilds."),
    ("_attic-*/", "Leftovers moved out by hand. Nothing reads them; delete when you are happy."),
]
# A "code" is the SHA-256 fingerprint of one exact version of a file.


def load_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def records(folder: pathlib.Path) -> list[dict]:
    if not folder.is_dir():
        return []
    return [d for p in sorted(folder.glob("*.json")) if (d := load_json(p))]


def day(stamp: str | None) -> str:
    return (stamp or "")[:10] or "—"


def esc(value) -> str:
    return html.escape(str(value))


def collect(root: pathlib.Path) -> dict:
    docs = root / "documents"
    reviews = root / "reviews"

    open_failures = {f["sha256"] for f in records(reviews / "failures")
                     if f.get("status") == "open" and f.get("sha256")}
    ingest_by_doc: dict[str, dict] = {}
    for job in records(root / "ingestions"):
        name = job.get("document")
        if name and (name not in ingest_by_doc
                     or (job.get("requested_at") or "") > (ingest_by_doc[name].get("requested_at") or "")):
            ingest_by_doc[name] = job

    by_doc: dict[str, list[dict]] = defaultdict(list)
    for review in records(reviews):
        if review.get("document"):
            by_doc[review["document"]].append(review)

    rows = []
    for name, versions in by_doc.items():
        versions.sort(key=lambda r: r.get("recorded_at") or "")
        latest = versions[-1]
        where = next((b for b in BUCKETS if (docs / b / name).is_file()), None)
        final = latest.get("final_status")
        sha = latest.get("sha256", "")
        ingest = ingest_by_doc.get(name)
        link = None
        needs_you = False
        if final == "FAIL" and sha in open_failures:
            status, tone, needs_you = "Blocked: needs repair", "bad", True
            if (reviews / "repairs" / sha / "index.html").is_file():
                link = ("Repair batch", f"reviews/repairs/{sha}/index.html")
        elif where == "rejected":
            status, tone = "Rejected", "muted"
        elif where == "superseded":
            status, tone = "Superseded by a newer version", "muted"
        elif where == "incoming" and final == "WARN" and latest.get("human_decision") is None:
            status, tone, needs_you = "Waiting for your approval", "warn", True
            link = ("Approval inbox", "reviews/pending/index.html")
        elif where == "approved":
            ingest = ingest or {}
            state = ingest.get("status")
            if state == "succeeded":
                chunks = ingest.get("chunk_count")
                status, tone = f"Ingested ({chunks} chunks)" if chunks else "Ingested", "good"
            elif state in ("queued", "running"):
                status, tone = "Approved, ingest running", "warn"
            elif state == "failed":
                status, tone, needs_you = "Approved, ingest FAILED", "bad", True
            else:
                status, tone = "Approved, not ingested yet", "warn"
        elif where == "incoming":
            status, tone = f"In review ({final or 'no result yet'})", "warn"
        else:
            status, tone = f"Not in documents/ (last review {final})", "muted"
        collection, _, filename = name.rpartition("/")
        rows.append(dict(collection=collection or "—", name=filename, status=status, tone=tone,
                         needs_you=needs_you, link=link, date=day(latest.get("recorded_at")),
                         tries=len(versions), sha=sha[:8]))
    rows.sort(key=lambda r: (not r["needs_you"], r["collection"].lower(), r["name"].lower()))

    # Approved files that predate the reviewer (no review record). Count per
    # folder; Docling picture "artifacts" folders are not documents.
    reviewed = set(by_doc)
    legacy = Counter()
    approved = docs / "approved"
    if approved.is_dir():
        for path in approved.rglob("*"):
            rel = path.relative_to(approved)
            if (path.is_file() and not path.name.startswith(".")
                    and "artifacts" not in rel.parts and rel.as_posix() not in reviewed):
                legacy[rel.parent.as_posix() if rel.parent.parts else "(top level)"] += 1

    conversions = {c.get("document"): c for c in sorted(
        records(root / "conversions"), key=lambda c: c.get("requested_at") or "")}
    raw_rows = []
    raw = docs / "raw"
    if raw.is_dir():
        for path in sorted(raw.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                rel = path.relative_to(raw).as_posix()
                job = conversions.get(rel)
                raw_rows.append(dict(name=rel, state=(job or {}).get("status", "not converted by the pipeline"),
                                     date=day((job or {}).get("completed_at") or (job or {}).get("requested_at"))))
    return dict(rows=rows, legacy=sorted(legacy.items()), raw=raw_rows)


def render(data: dict) -> str:
    rows = data["rows"]
    attention = [r for r in rows if r["needs_you"]]
    counts = Counter(r["tone"] for r in rows)

    def doc_row(r: dict) -> str:
        link = f'<a href="{esc(r["link"][1])}">{esc(r["link"][0])}</a>' if r["link"] else ""
        return (f'<tr><td>{esc(r["collection"])}</td><td>{esc(r["name"])}</td>'
                f'<td><span class="pill {r["tone"]}">{esc(r["status"])}</span> {link}</td>'
                f'<td>{esc(r["date"])}</td><td class="num">{r["tries"]}</td>'
                f'<td><code>{esc(r["sha"])}</code></td></tr>')

    if attention:
        todo = "<ul>" + "".join(
            f'<li><b>{esc(r["name"])}</b> ({esc(r["collection"])}): {esc(r["status"])}'
            + (f' — <a href="{esc(r["link"][1])}">{esc(r["link"][0])}</a>' if r["link"] else "")
            + "</li>" for r in attention) + "</ul>"
    else:
        todo = "<p>Nothing is waiting on you.</p>"

    legacy = "".join(f"<tr><td>{esc(folder)}</td><td class='num'>{n}</td></tr>" for folder, n in data["legacy"])
    raw = "".join(f"<tr><td>{esc(r['name'])}</td><td>{esc(r['state'])}</td><td>{esc(r['date'])}</td></tr>"
                  for r in data["raw"])
    guide = "".join(f"<tr><td><code>{p}</code></td><td>{d}</td></tr>" for p, d in FOLDER_GUIDE)
    made = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>RAG store overview</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6b66;--line:#dedbd2;--card:#fff;
--good:#1f7a4a;--good-bg:#e3f3ea;--warn:#8a5a00;--warn-bg:#fdf1d6;--bad:#a8261c;--bad-bg:#fbe3e0;--mut-bg:#eeede8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#191917;--fg:#ecebe6;--muted:#a3a29b;--line:#3a3935;--card:#22221f;
--good:#7fd3a3;--good-bg:#1d3427;--warn:#f0c56b;--warn-bg:#3a2f15;--bad:#f2948a;--bad-bg:#3d1f1b;--mut-bg:#2c2c29}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:24px 16px}}
h1{{margin:0 0 4px}} h2{{margin-top:2em;border-bottom:1px solid var(--line);padding-bottom:4px}}
.sub{{color:var(--muted);margin:0}} a{{color:inherit}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:120px}}
.card b{{display:block;font-size:24px}}
.todo{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--bad);border-radius:8px;padding:4px 16px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--muted);font-weight:600;font-size:13px}} .num{{text-align:right}}
code{{font-size:12px;color:var(--muted)}}
.pill{{display:inline-block;border-radius:10px;padding:0 8px;font-size:13px;white-space:nowrap}}
.good{{background:var(--good-bg);color:var(--good)}} .warn{{background:var(--warn-bg);color:var(--warn)}}
.bad{{background:var(--bad-bg);color:var(--bad)}} .muted{{background:var(--mut-bg);color:var(--muted)}}
</style>
<h1>RAG store overview</h1>
<p class="sub">Snapshot made {esc(made)}. Rerun <code>store_overview.py</code> to refresh. Nothing here approves or changes anything.</p>
<div class="cards">
<div class="card"><b>{len(rows)}</b>reviewed documents</div>
<div class="card"><b>{counts['good']}</b>ingested</div>
<div class="card"><b>{counts['warn']}</b>in progress</div>
<div class="card"><b>{counts['bad']}</b>blocked / failed</div>
</div>
<h2>Needs you</h2><div class="todo">{todo}</div>
<h2>Every reviewed document</h2>
<p class="sub">One row per document, showing its latest version. “Tries” counts versions reviewed; “Code” is the start of the latest version’s SHA-256, matching folder names under <code>reviews/</code>.</p>
<div class="wrap"><table><tr><th>Collection</th><th>Document</th><th>Status</th><th>Last review</th><th class="num">Tries</th><th>Code</th></tr>
{"".join(doc_row(r) for r in rows)}</table></div>
<h2>Approved before the reviewer existed</h2>
<p class="sub">Files in <code>documents/approved/</code> with no review record (older or manual uploads). Docling picture folders are not counted.</p>
<div class="wrap"><table><tr><th>Folder</th><th class="num">Files</th></tr>{legacy or "<tr><td colspan=2>None</td></tr>"}</table></div>
<h2>Original PDFs (documents/raw)</h2>
<div class="wrap"><table><tr><th>File</th><th>Conversion</th><th>Date</th></tr>{raw or "<tr><td colspan=3>None</td></tr>"}</table></div>
<h2>What each folder is for</h2>
<p class="sub">Folder names are fixed by the reviewer code; renaming one breaks the pipeline. A “code” is the SHA-256 fingerprint of one exact file version.</p>
<div class="wrap"><table>{guide}</table></div>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Write rag-store/OVERVIEW.html, a plain-English store overview.")
    parser.add_argument("store", type=pathlib.Path, help="rag-store root, e.g. /Volumes/RAG_Files/rag-store")
    args = parser.parse_args()
    root = args.store
    if not (root / "documents").is_dir() or not (root / "reviews").is_dir():
        parser.error(f"{root} does not look like a rag-store (no documents/ and reviews/)")
    page = render(collect(root))
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root, suffix=".tmp", delete=False) as tmp:
        tmp.write(page)
    pathlib.Path(tmp.name).replace(root / "OVERVIEW.html")
    print(root / "OVERVIEW.html")


if __name__ == "__main__":
    main()
