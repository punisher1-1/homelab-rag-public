#!/usr/bin/env python3
"""
corpus_manifest.py — read manifests that describe an approved document collection.

WHY THIS EXISTS
---------------
Originally the only metadata an approved reference document carried was its COLLECTION,
that came from the name of the folder it happened to sit in. That is enough for
`nist` vs `atf`. It falls apart for payroll, for two reasons:

1. **Jurisdiction has to be a FILTER, not a folder.** `dropzone/payroll/Maine/`
   does not work — `gather_drop()` only reads the FIRST subfolder, so "Maine"
   is invisible. Fifty collections (`payroll-me`, `payroll-tx`, …) would work
   but makes a cross-state comparison impossible, which is the main question
   type this corpus exists to answer.

   And jurisdiction genuinely must be a filter rather than a ranking signal.
   Measured 2026-08-02: a two-state comparison retrieved 8 Colorado chunks and
   2 Texas ones from documents with 33 sections each, because Texas law is
   largely written as ABSENCE and preemption, which is semantically thin and
   retrieves badly. With 50 states in one collection, a Maine question would
   never see Maine. No reranker fixes that — a cross-encoder can only reorder
   candidates, and the right chunks were never candidates.

2. **A folder convention is not portable.** Hand `dropzone/` to another deployment or
   a client and the semantics live only in this repo's `ingest.py`. A manifest
   travels WITH the documents, so the corpus describes itself.

It also turns two manual preflight checks into code: the sha256 in the manifest
is verified against the file on disk at ingest (catching the filename/content
swaps that cost four days in July), and `source_url` / `landing_url` / `retrieved`
preserve where a document came from — which matters most for state payroll law,
where re-finding a document means searching 50 state websites again.

FORMAT
------
One `manifest.yaml` per collection folder, sitting next to the documents:

    dropzone/payroll/manifest.yaml
    dropzone/payroll/US-TX-payroll-2026.json
    dropzone/payroll/US-CO-payroll-2026.json

    collection: payroll          # authoritative — overrides the folder name
    defaults:                    # merged into every document below
      level: state
      authority: secondary
    documents:
      - file: US-TX-payroll-2026.json
        sha256: 3f9a...          # optional; VERIFIED when present
        jurisdiction: TX         # 2-letter, or US for federal
        jurisdiction_name: Texas
        source_url: https://...  # the PDF itself
        landing_url: https://... # the page you found the link on
        retrieved: 2026-08-02
        effective: 2026-01-01
        authority: primary       # overrides defaults
        notes: free text

Every recognised field is stamped onto every chunk of that document in
`chunks.metadata` (jsonb — no schema change needed, same as `page`/`section`/`sha`).

Unknown keys are kept and stamped too, so you can add a field without touching
this file. `file` and `sha256` are consumed here and not stamped.
"""
import datetime
import hashlib
import pathlib

try:
    import yaml
except ImportError:  # pyyaml is in requirements.txt; fail with a useful message
    yaml = None

MANIFEST_NAME = "manifest.yaml"

# Consumed by the loader, never stamped onto chunks.
#
# `profile` is here deliberately. Unknown keys ARE stamped (see the docstring),
# which is right for scalars like `publisher` and wrong for a nested block of
# regexes: it would put the whole profile into every chunk's metadata, thousands
# of times, describing the document rather than the chunk. It is read by
# corpus_profile.py, which verifies the claims it makes.
_CONTROL_KEYS = {"file", "sha256", "profile"}

# Stamped as-is when present. Anything not listed here is ALSO stamped (so you
# can add fields freely) — this list only documents the intended vocabulary.
KNOWN_FIELDS = (
    "jurisdiction",       # "TX", "CO", "US" (federal), "US-TX-AUSTIN" for local
    "jurisdiction_name",  # "Texas" — the human form, so BM25 can match the word
    "level",              # federal | state | local
    "authority",          # primary (the law/regulator) | secondary (a summary)
    "source_url",         # direct link to the document
    "landing_url",        # the page the link was found on — the expensive part to re-find
    "retrieved",          # ISO date you downloaded it
    "effective",          # ISO date the law took effect
    "supersedes",         # filename of the version this replaces
    "publisher",
    "notes",
    "profile",            # per-source CONVENTIONS (page/section/citation shapes).
                          # Not stamped — see _CONTROL_KEYS. Verified by
                          # corpus_profile.py; run it before every ingest.
)


class ManifestError(Exception):
    """Raised for a malformed manifest, or a sha256 that does not match."""


def file_sha256(path) -> str:
    """Return the complete SHA-256 digest for a file.

    This is shared by ingestion and standalone manifest validation so the
    incremental-ingest key and the provenance check cannot silently use
    different hash lengths or algorithms.
    """
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _norm(value):
    """YAML turns bare dates into date objects; Postgres jsonb wants strings."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def load(folder) -> dict:
    """Read `folder/manifest.yaml`.

    Returns {filename: {field: value}} with `defaults` already merged in, or {}
    if the folder has no manifest. Results are NOT cached here — `Manifest`
    below does that, because ingest walks a folder one file at a time.
    """
    folder = pathlib.Path(folder)
    path = folder / MANIFEST_NAME
    if not path.is_file():
        return {}
    if yaml is None:
        raise ManifestError(
            f"{path} exists but PyYAML is not installed — pip install pyyaml"
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"{path}: not valid YAML — {e}") from e
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: top level must be a mapping")

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ManifestError(f"{path}: `defaults:` must be a mapping")

    docs = raw.get("documents") or []
    if not isinstance(docs, list):
        raise ManifestError(f"{path}: `documents:` must be a list")

    collection = raw.get("collection")

    out = {}
    for i, entry in enumerate(docs):
        if not isinstance(entry, dict):
            raise ManifestError(f"{path}: documents[{i}] must be a mapping")
        name = entry.get("file")
        if not name:
            raise ManifestError(f"{path}: documents[{i}] is missing `file:`")
        merged = {k: _norm(v) for k, v in defaults.items()}
        merged.update({k: _norm(v) for k, v in entry.items()})
        # The manifest's `collection:` is authoritative over the folder name.
        # This is deliberate: a folder called `Payroll` yields the tag `payroll`
        # via .lower(), but a folder renamed in Finder silently re-tags a whole
        # collection and every scoped query starts missing. Stating it here
        # makes the tag a decision rather than a side effect.
        if collection:
            merged.setdefault("collection", str(collection).strip().lower())
        out[name] = merged
    return out


class Manifest:
    """Per-folder manifest cache with sha verification.

    Used by ingest.py:

        man = Manifest(require=args.require_manifest)
        extra = man.metadata_for(path, sha)    # raises ManifestError on mismatch
    """

    def __init__(self, require: bool = False, verify_sha: bool = True):
        self.require = require          # error on a file with no manifest entry
        self.verify_sha = verify_sha
        self._cache = {}
        self.warnings = []

    def _for_folder(self, folder: pathlib.Path) -> dict:
        key = str(folder)
        if key not in self._cache:
            self._cache[key] = load(folder)
        return self._cache[key]

    def has_manifest(self, folder) -> bool:
        return bool(self._for_folder(pathlib.Path(folder)))

    def metadata_for(self, path, sha: str = None) -> dict:
        """Return the stamped metadata for one document.

        Raises ManifestError if `require` is set and the file is unlisted, or if
        a declared sha256 does not match the bytes on disk.
        """
        path = pathlib.Path(path)
        entries = self._for_folder(path.parent)

        if not entries:
            if self.require:
                raise ManifestError(
                    f"{path.parent}/{MANIFEST_NAME} not found and --require-manifest is set"
                )
            return {}

        entry = entries.get(path.name)
        if entry is None:
            msg = f"{path.name} is not listed in {path.parent}/{MANIFEST_NAME}"
            if self.require:
                raise ManifestError(msg)
            self.warnings.append(msg)
            return {}

        declared = entry.get("sha256")
        if declared and sha and self.verify_sha:
            if str(declared).strip().lower() != sha.lower():
                # This is the automated form of preflight §1 "does each file
                # contain what it claims?" Two filename/content swaps in a
                # six-document corpus (2026-08-01) say this must be an error,
                # not a warning — a mislabelled document poisons every metric
                # computed downstream and looks like a model failure.
                raise ManifestError(
                    f"sha256 MISMATCH for {path.name}\n"
                    f"    manifest: {declared}\n"
                    f"    on disk:  {sha}\n"
                    "    The file changed, or the manifest describes a different document. "
                    "Fix one of them before ingesting."
                )

        return {k: v for k, v in entry.items() if k not in _CONTROL_KEYS}


def unlisted_files(folder, exts) -> list:
    """Files present in `folder` that the manifest does not mention.

    Preflight helper — the inverse of the sha check. A document sitting in a
    collection with no manifest entry will be ingested with no jurisdiction,
    which means a scoped query can never retrieve it. Silent, and it looks
    exactly like a retrieval failure.
    """
    folder = pathlib.Path(folder)
    entries = load(folder)
    if not entries:
        return []
    return sorted(
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in exts
        and not p.name.startswith(".") and p.name not in entries
    )


if __name__ == "__main__":
    # Standalone check:  python corpus_manifest.py dropzone/payroll
    import sys

    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "dropzone")
    folders = [target] if (target / MANIFEST_NAME).is_file() else sorted(
        p for p in target.rglob("*") if (p / MANIFEST_NAME).is_file()
    )
    if not folders:
        print(f"no {MANIFEST_NAME} found under {target}")
        raise SystemExit(1)

    bad = 0
    for folder in folders:
        entries = load(folder)
        print(f"\n{folder}  —  {len(entries)} document(s)")
        for name, meta in entries.items():
            f = folder / name
            if not f.is_file():
                print(f"  ✗ {name}: listed in manifest but NOT ON DISK")
                bad += 1
                continue
            line = f"  ✓ {name}"
            declared = meta.get("sha256")
            if declared:
                if file_sha256(f).lower() != str(declared).strip().lower():
                    line = f"  ✗ {name}: SHA MISMATCH"
                    bad += 1
            jur = meta.get("jurisdiction", "—")
            print(f"{line}   [{meta.get('collection','?')}/{jur}] "
                  f"{meta.get('authority','?')}  retrieved={meta.get('retrieved','?')}")
        missing = unlisted_files(folder, {".pdf", ".json", ".md", ".txt", ".docx"})
        for m in missing:
            print(f"  ! {m}: on disk but NOT in the manifest (would ingest unscoped)")
            bad += 1
    raise SystemExit(1 if bad else 0)
