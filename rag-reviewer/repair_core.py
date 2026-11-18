"""Hash-bound, human-approved repair batches. No automatic content mutation."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import pathlib
import re
import threading
import os
from concurrent.futures import ThreadPoolExecutor

import requests
import parse_check
import verified_findings
from conversion_core import ConversionManager
from page_exclusions import remove_items, reference
from reviewer_core import ReviewError, resource_lock, sha256_file, utc_now


CONFIRM = "APPLY REVIEWED REPAIR BATCH"


def digest_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ReviewError("invalid repair batch hash")
    return value


def overlap(a, b):
    if a.get("coord_origin") != b.get("coord_origin"):
        return 0
    ax, ay = sorted([a['l'], a['r']]), sorted([a['t'], a['b']])
    bx, by = sorted([b['l'], b['r']]), sorted([b['t'], b['b']])
    area = max(0, min(ax[1], bx[1])-max(ax[0], bx[0])) * max(0, min(ay[1], by[1])-max(ay[0], by[0]))
    return area / max(1, (bx[1]-bx[0])*(by[1]-by[0]))


class RepairManager:
    def __init__(self, store, runner=None):
        self.store = store
        self.root = store.reviews / 'repairs'
        self.runner = runner or self._retry_page
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rag-repair')
        self.lock = threading.Lock()
        self.jobs = {}

    def directory(self, digest):
        return self.root / digest_id(digest)

    def status(self, digest):
        return json.loads((self.directory(digest) / 'batch.json').read_text())

    def source(self, identifier, digest, document):
        # Only conversion receipts can bind a raw PDF to parsed output. Never
        # follow origin filenames, URLs, or arbitrary paths supplied by a document.
        ancestors = {digest}
        for history in document.get('rag_curation', {}).values():
            if isinstance(history, list):
                for x in history:
                    previous = x.get('source_sha256') if isinstance(x, dict) else None
                    if not isinstance(previous, str) or not re.fullmatch(r'[0-9a-f]{64}', previous):
                        continue
                    archives = [self.store.reviews/'page-exclusions'/previous/'original.json',
                                self.root/previous/'original.json']
                    if any(p.is_file() and sha256_file(p) == previous for p in archives):
                        ancestors.add(previous)
        for receipt in sorted((self.store.root / 'conversions').glob('*.json')):
            r = json.loads(receipt.read_text())
            if (r.get('status') == 'succeeded' and r.get('output_document') == identifier
                    and r.get('output_sha256') in ancestors):
                p = ConversionManager.resolve_raw(self._conversion_resolver(), r['document'])
                if p.suffix.lower() == '.pdf' and sha256_file(p) == r['sha256']:
                    return {'document': r['document'], 'sha256': r['sha256']}
        return None

    def _conversion_resolver(self):
        # No executor required for path resolution.
        resolver = object.__new__(ConversionManager)
        resolver.raw = self.store.documents / 'raw'
        return resolver

    def prepare(self, identifier, digest):
        digest_id(digest)
        with resource_lock(f'{self.store.root}/{identifier}'):
            path = self.store.resolve('incoming', identifier)
            if sha256_file(path) != digest:
                raise ReviewError('stale document hash')
            folder = self.directory(digest)
            if (folder / 'batch.json').exists():
                batch = self.status(digest)
                self.render(batch)
                return batch
            document = json.loads(path.read_text())
            items = []
            accepted = verified_findings.accepted_items(self.store.reviews, identifier, digest, document)
            for index, item in enumerate(document.get('texts', [])):
                finding = parse_check.inspect_item(item)
                if finding and index not in accepted:
                    items.append({'ref': f'#/texts/{index}', 'text': item.get('text', ''),
                                  'label': item.get('label'), 'provenance': item.get('prov', []),
                                  'finding': finding.as_dict(), 'candidate': None,
                                  'context': [t.get('text', '') for t in document['texts'][max(0,index-1):index+2]],
                                  'retry_status': 'pending'})
            batch = {'document': identifier, 'sha256': digest, 'status': 'prepared',
                     'created_at': utc_now(), 'items': items,
                     'source': self.source(identifier, digest, document),
                     'note': 'Text-integrity findings only; this is not table/figure completeness validation.'}
            folder.mkdir(parents=True, exist_ok=True)
            self.store._atomic_json(folder / 'batch.json', batch)
            self.render(batch)
            return batch

    def start(self, identifier, digest):
        self.prepare(identifier, digest)
        with self.lock:
            if digest not in self.jobs or self.jobs[digest].done():
                if self.status(digest)['status'] not in {'ready', 'applied'}:
                    self.jobs[digest] = self.pool.submit(self.retry, digest)
        return self.status(digest)

    def retry(self, digest):
        # One bounded attempt per page, sequentially. Failures remain visible;
        # restart can safely retry an interrupted batch without changing input.
        with resource_lock(f'{self.store.root}/repair-job/{digest}'):
            batch = self.status(digest)
            if batch['status'] == 'applied':
                return batch
            batch['status'] = 'retrying'
            self._save(batch)
            source = batch['source']
            pages = sorted({p['page_no'] for i in batch['items'] for p in i['provenance']
                            if type(p.get('page_no')) is int})
            attempted = batch.setdefault('attempted_pages', [])
            pending_pages = [p for p in pages if p not in attempted]
            batch['deferred_pages'] = pending_pages[8:]
            for page in pending_pages[:8]:
                affected = [i for i in batch['items'] if any(p.get('page_no') == page for p in i['provenance'])]
                try:
                    if not source:
                        raise ReviewError('No hash-bound raw PDF receipt; manual source verification required')
                    path = ConversionManager.resolve_raw(self._conversion_resolver(), source['document'])
                    if sha256_file(path) != source['sha256']:
                        raise ReviewError('Raw PDF changed; retry refused')
                    candidate = self.runner(path, page)
                    if sha256_file(path) != source['sha256']:
                        raise ReviewError('Raw PDF changed during retry')
                    self.store._atomic_json(self.directory(digest) / f'page-{page}.json', candidate)
                    for i in affected:
                        if len(i['provenance']) != 1:
                            i['retry_status'] = 'manual: cross-page item'
                            continue
                        bbox = i['provenance'][0].get('bbox', {})
                        matches = [t for t in candidate.get('texts', []) if any(
                            p.get('page_no') == page and overlap(bbox, p.get('bbox', {})) >= .5
                            for p in t.get('prov', []))]
                        text = ' '.join(t.get('text', '') for t in matches).strip()
                        hit = parse_check.inspect_item({**i, 'text': text}) if text else None
                        i['candidate'] = text or None
                        i['retry_status'] = 'candidate-check-pass' if text and not hit else 'manual: retry incomplete or still flagged'
                        i['candidate_findings'] = hit.as_dict() if hit else None
                    image = candidate.get('pages', {}).get(str(page), {}).get('image', {}).get('uri', '')
                    # Do not let a document inject external network fetches or SVG.
                    if re.fullmatch(r'data:image/png;base64,[A-Za-z0-9+/=\s]+', image):
                        self.store._atomic_json(self.directory(digest) / f'image-{page}.json', {'uri': image})
                except Exception as exc:
                    for i in affected:
                        i['retry_status'] = f'retry error: {type(exc).__name__}: {str(exc)[:200]}'
                attempted.append(page)
                self._save(batch)
            batch['status'] = 'ready'
            self._save(batch)
            return batch

    def _retry_page(self, path, page):
        with path.open('rb') as stream:
            response = requests.post(ConversionManager._base_url() + '/v1/convert/file',
                headers=ConversionManager._headers(), files={'files': (path.name, stream, 'application/pdf')},
                data=[('to_formats','json'), ('page_range',str(page)), ('page_range',str(page)),
                      ('do_ocr','true'), ('force_ocr','true'), ('ocr_engine','rapidocr'),
                      ('include_page_images','true'), ('image_export_mode','embedded'), ('images_scale','2')],
                timeout=(10, 600))
        response.raise_for_status()
        result = response.json()
        if result.get('status') != 'success':
            raise ReviewError('Targeted Docling retry was not successful')
        doc = result.get('document', {}).get('json_content')
        if isinstance(doc, str):
            doc = json.loads(doc)
        if not isinstance(doc, dict) or doc.get('schema_name') != 'DoclingDocument':
            raise ReviewError('Retry did not return Docling JSON')
        if set(map(int, doc.get('pages', {}))) != {page}:
            raise ReviewError('Retry page indices differ from requested original PDF page')
        return doc

    def _save(self, batch):
        self.store._atomic_json(self.directory(batch['sha256']) / 'batch.json', batch)
        self.render(batch)

    def apply(self, digest, decisions, reason, confirmation):
        if confirmation != CONFIRM or not reason.strip() or len(reason) > 1000:
            raise ReviewError('Explicit confirmation and a reason (1..1000 characters) required')
        batch = self.status(digest)
        identifier = batch['document']
        with resource_lock(f'{self.store.root}/{identifier}'):
            path = self.store.resolve('incoming', identifier)
            original = path.read_bytes()
            if hashlib.sha256(original).hexdigest() != digest:
                raise ReviewError('stale repair batch; input changed')
            if batch['status'] not in {'prepared', 'ready'}:
                raise ReviewError('Wait for retries to finish before applying decisions')
            document = json.loads(original)
            curated = copy.deepcopy(document)
            allowed = {i['ref']: i for i in batch['items']}
            seen, removed, changes, verified = set(), set(), [], []
            for decision in decisions:
                ref, action = decision.get('ref'), decision.get('action')
                if ref not in allowed or ref in seen or action not in {'replace', 'exclude', 'keep', 'verify'}:
                    raise ReviewError('Unknown/duplicate item or action')
                seen.add(ref)
                item = curated['texts'][int(ref.rsplit('/',1)[1])]
                if action == 'keep':
                    continue
                if action == 'verify':
                    finding = parse_check.inspect_item(item)
                    if finding is None or finding.as_dict() != allowed[ref]['finding']:
                        raise ReviewError('Checker findings changed; prepare a fresh review')
                    verified.append(int(ref.rsplit('/', 1)[1]))
                    changes.append({'ref': ref, 'action': 'verify', 'before': item['text'],
                                    'provenance': item.get('prov', []), 'finding': finding.as_dict()})
                    continue
                if item.get('children'):
                    raise ReviewError('Items owning children require structural review')
                parent = reference(item.get('parent', {}))
                visited = set()
                while isinstance(parent, str) and parent not in visited:
                    visited.add(parent)
                    if parent.startswith(('#/tables/', '#/form_items/', '#/key_value_items/')):
                        raise ReviewError('Table/form cell repairs require structured review; text-only changes refused')
                    if not parent.startswith('#/groups/'):
                        break
                    parent = reference(curated['groups'][int(parent.rsplit('/',1)[1])].get('parent', {}))
                change = {'ref': ref, 'action': action, 'before': item['text'], 'provenance': item.get('prov', [])}
                if action == 'exclude':
                    removed.add(ref)
                else:
                    text = decision.get('text')
                    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                        raise ReviewError('Replacement must be nonempty and at most 20000 characters')
                    if len(item.get('prov', [])) != 1:
                        raise ReviewError('Cross-page replacements require region review; item exclusion is explicit')
                    item['text'] = text
                    # Original OCR stays in orig; geometry/page remain unchanged.
                    item['prov'][0]['charspan'] = [0, len(text)]
                    change['after'] = text
                changes.append(change)
            if not changes:
                raise ReviewError('No repairs or verified-correct decisions selected')
            if seen != set(allowed):
                raise ReviewError('Every flagged item needs an explicit decision, including keep')
            curated = remove_items(curated, removed)
            history = curated.setdefault('rag_curation', {}).setdefault('repairs', [])
            history.append({'source_sha256': digest, 'reason': reason.strip(),
                            'recorded_at': utc_now(), 'changes': changes})
            backup = self.directory(digest) / 'original.json'
            if backup.exists() and backup.read_bytes() != original:
                raise ReviewError('Backup conflict')
            if not backup.exists():
                with backup.open('xb') as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
            if sha256_file(path) != digest:
                raise ReviewError('Input changed during backup')
            # Match the atomic writer's exact serialization. Publish the trusted
            # receipt before exposing the new input to discovery.
            new = hashlib.sha256((json.dumps(curated, ensure_ascii=False, indent=2) + '\n').encode()).hexdigest()
            if verified:
                entries = []
                removed_indices = {int(ref.rsplit('/', 1)[1]) for ref in removed}
                for old_index in verified:
                    index = old_index - sum(i < old_index for i in removed_indices)
                    item = curated['texts'][index]
                    finding = parse_check.inspect_item(item)
                    entries.append({'ref': f'#/texts/{index}',
                                    'binding': verified_findings.binding(f'#/texts/{index}', item, finding)})
                receipts = self.store.reviews / 'verified-correct'
                receipts.mkdir(parents=True, exist_ok=True)
                self.store._atomic_json(receipts / (new + '.json'), {
                    'document': identifier, 'sha256': new, 'source_sha256': digest,
                    'reason': reason.strip(), 'recorded_at': utc_now(), 'items': entries})
            self.store._atomic_json(path, curated)
            new = sha256_file(path)
            batch.update(status='applied', derived_sha256=new, reason=reason.strip(),
                         applied_at=utc_now(), decisions=decisions)
            self._save(batch)
            self.store.remove_approval_link(digest)
            self.store.resolve_failure(digest, f'Reviewed repairs applied; fresh review required: {new}')
            self.store._scan_state.pop(identifier, None)
            return {'sha256': new, 'status': 'awaiting_fresh_review', 'document': identifier}

    def render(self, batch):
        esc = lambda value: html.escape(str(value), quote=True)
        rows = []
        pages = sorted({p['page_no'] for i in batch['items'] for p in i['provenance'] if type(p.get('page_no')) is int})
        for page in pages:
            image_path = self.directory(batch['sha256']) / f'image-{page}.json'
            if image_path.exists():
                uri = json.loads(image_path.read_text())['uri']
                rows.append(f'<details><summary>Original PDF page {page} image</summary><img width="900" src="{esc(uri)}"></details>')
        for i in batch['items']:
            rows.append(f'<article data-ref="{esc(i["ref"])}"><h2>{esc(i["ref"])} — PDF page {esc(i["finding"]["page"])}</h2>'
                f'<p>{esc(i["finding"]["reasons"])}</p><pre>{esc(i["text"])}</pre>'
                f'<details><summary>Coordinates and surrounding provenance</summary><pre>{esc(i["provenance"])}</pre></details>'
                f'<details><summary>Surrounding extraction</summary><pre>{esc(i.get("context", []))}</pre></details>'
                f'<p>{esc(i["retry_status"])}</p><select><option value="keep">Keep blocked / unchanged</option>'
                '<option value="verify">Verified correct — accept original as-is</option>'
                '<option value="replace">Replace with reviewed text</option><option value="exclude">Exclude this entire item</option></select>'
                f'<textarea rows="4" cols="100">{esc(i.get("candidate") or i["text"])}</textarea></article>')
        script = '''document.getElementById('export').onclick=()=>{const decisions=[...document.querySelectorAll('article')].map(e=>({ref:e.dataset.ref,action:e.querySelector('select').value,text:e.querySelector('textarea').value}));const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify({sha256:document.body.dataset.sha,reason:document.getElementById('reason').value,decisions},null,2)],{type:'application/json'}));a.download='repair-decisions.json';a.click();URL.revokeObjectURL(a.href);};'''
        content = ('<!doctype html><html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" '
            'content="default-src \'none\'; img-src data:; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'">'
            '<title>Review repair batch</title><style>body{font:16px system-ui;max-width:1100px;margin:2em auto}pre{white-space:pre-wrap}article{border:1px solid #aaa;padding:1em;margin:1em 0}textarea{display:block;max-width:100%}</style>'
            f'<body data-sha="{esc(batch["sha256"])}"><h1>{esc(batch["document"])}</h1><p>Status: {esc(batch["status"])}</p>'
            '<p>All suggestions need human verification. Keep leaves content unchanged and may keep ingestion blocked. '
            'Verified correct accepts the ORIGINAL text and current findings only, ignoring the replacement box. '
            'Changed documents or findings require verification again. '
            'Exporting does NOT apply edits or approve ingestion. Reopen/reload after retries finish.</p>'
            f'<p>{esc(batch["note"])}</p><p>Deferred PDF pages (run another retry batch): {esc(batch.get("deferred_pages", []))}</p>' + ''.join(rows) +
            '<label>Reason: <input id="reason" maxlength="1000"></label><button id="export">Export reviewed decisions</button>'
            f'<script>{script}</script></body></html>')
        path = self.directory(batch['sha256']) / 'index.html'
        temp = path.with_suffix('.tmp')
        temp.write_text(content)
        temp.replace(path)
