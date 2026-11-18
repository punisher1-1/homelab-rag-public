#!/usr/bin/env python3
"""Regression tests for manifest SHA-256 provenance checks."""

import hashlib
import pathlib
import tempfile
import unittest

import yaml

import corpus_manifest
from corpus_manifest import Manifest, ManifestError


class FileSha256Tests(unittest.TestCase):
    def test_returns_complete_digest_for_multiblock_file(self):
        content = (b"a" * (1 << 20)) + b"second block"
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "document.bin"
            path.write_bytes(content)

            actual = corpus_manifest.file_sha256(path)

        self.assertEqual(actual, hashlib.sha256(content).hexdigest())
        self.assertEqual(len(actual), 64)


class ManifestSha256Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.tempdir.name)
        self.document = self.folder / "document.json"
        self.document.write_bytes(b'{"text": "verified source"}\n')
        self.digest = corpus_manifest.file_sha256(self.document)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_manifest(self, declared_hash):
        manifest = {
            "collection": "payroll",
            "documents": [{
                "file": self.document.name,
                "sha256": declared_hash,
                "jurisdiction": "TX",
            }],
        }
        (self.folder / corpus_manifest.MANIFEST_NAME).write_text(
            yaml.safe_dump(manifest), encoding="utf-8"
        )

    def test_accepts_matching_full_digest(self):
        self.write_manifest(self.digest)

        metadata = Manifest(require=True).metadata_for(
            self.document, corpus_manifest.file_sha256(self.document)
        )

        self.assertEqual(metadata["collection"], "payroll")
        self.assertEqual(metadata["jurisdiction"], "TX")
        self.assertNotIn("sha256", metadata)

    def test_accepts_uppercase_digest(self):
        self.write_manifest(self.digest.upper())

        metadata = Manifest(require=True).metadata_for(
            self.document, corpus_manifest.file_sha256(self.document)
        )

        self.assertEqual(metadata["jurisdiction"], "TX")

    def test_rejects_changed_document(self):
        self.write_manifest(self.digest)
        self.document.write_bytes(b'{"text": "changed after approval"}\n')

        with self.assertRaisesRegex(ManifestError, "sha256 MISMATCH"):
            Manifest(require=True).metadata_for(
                self.document, corpus_manifest.file_sha256(self.document)
            )

    def test_rejects_legacy_short_digest(self):
        self.write_manifest(self.digest[:16])

        with self.assertRaisesRegex(ManifestError, "sha256 MISMATCH"):
            Manifest(require=True).metadata_for(
                self.document, corpus_manifest.file_sha256(self.document)
            )


if __name__ == "__main__":
    unittest.main()
