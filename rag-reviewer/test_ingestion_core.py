import json
import importlib.util
import pathlib
import sys
import tempfile
import time
import unittest


PROJECT = pathlib.Path(__file__).resolve().parents[1]
PARSER = PROJECT / "paradedb-personal-rag" / "parse_check.py"
SPEC = importlib.util.spec_from_file_location("parse_check", PARSER)
parse_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parse_check)
sys.modules["parse_check"] = parse_check
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from ingestion_core import REBUILD_CONFIRMATION, IngestionError, IngestionManager
from reviewer_core import ReviewerStore, sha256_file


class IngestionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReviewerStore(pathlib.Path(self.temp.name) / "rag-store")
        self.store.ensure_layout()
        self.calls = []

        def runner(path, collection, digest, canonical_source):
            self.calls.append((path, collection, digest, canonical_source))
            return {
                "chunk_count": 7,
                "collection": collection,
                "embed_model": "test-embed",
                "embed_dim": 3,
                "reason": None,
            }

        self.runner = runner
        self.manager = IngestionManager(self.store, runner=self.runner)
        self.manager.ensure_layout()

    def tearDown(self):
        self.manager.shutdown()
        self.temp.cleanup()

    def approve(self, identifier="general/test.json"):
        path = self.store.approved / identifier
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema_name":"DoclingDocument","texts":[]}', encoding="utf-8")
        digest = sha256_file(path)
        (self.store.reviews / f"{digest}.json").write_text(
            json.dumps({
                "document": identifier,
                "sha256": digest,
                "final_status": "PASS",
            }),
            encoding="utf-8",
        )
        return path, digest

    def wait(self, digest):
        for _ in range(100):
            receipt = self.manager.status(digest)
            if receipt["status"] in {"succeeded", "failed"}:
                return receipt
            time.sleep(0.01)
        self.fail("ingestion did not finish")

    def wait_rebuild(self, rebuild_id):
        for _ in range(100):
            receipt = self.manager.rebuild_status(rebuild_id)
            if receipt["status"] in {"succeeded", "failed"}:
                return receipt
            time.sleep(0.01)
        self.fail("rebuild did not finish")

    def test_approved_document_gets_verified_receipt(self):
        _, digest = self.approve()
        queued = self.manager.start("general/test.json", digest)
        completed = self.wait(digest)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["chunk_count"], 7)
        self.assertEqual(completed["embed_model"], "test-embed")
        self.assertEqual(completed["source_path"], "rag://approved/general/test.json")
        self.assertEqual(len(self.calls), 1)

    def test_successful_sha_is_idempotent(self):
        _, digest = self.approve()
        self.manager.start("general/test.json", digest)
        first = self.wait(digest)
        second = self.manager.start("general/test.json", digest)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)

    def test_review_must_authorize_ingestion(self):
        path = self.store.approved / "general/test.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(IngestionError):
            self.manager.start("general/test.json", sha256_file(path))

    def test_stale_sha_is_rejected(self):
        _, digest = self.approve()
        with self.assertRaises(IngestionError):
            self.manager.start("general/test.json", "0" * 64)

    def test_runner_failure_is_durable(self):
        self.manager.shutdown()

        def broken(*_args):
            raise RuntimeError("embedding unavailable")

        self.manager = IngestionManager(self.store, runner=broken)
        _, digest = self.approve()
        self.manager.start("general/test.json", digest)
        receipt = self.wait(digest)
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("embedding unavailable", receipt["error"])

    def test_rebuild_requires_exact_confirmation(self):
        with self.assertRaisesRegex(IngestionError, "confirmation must exactly match"):
            self.manager.start_rebuild("yes")

    def test_rebuild_archives_receipts_and_uses_canonical_sources(self):
        self.manager.shutdown()
        reset_calls = []
        self.manager = IngestionManager(
            self.store,
            runner=self.runner,
            resetter=lambda: reset_calls.append("truncate"),
        )
        self.manager.ensure_layout()
        for identifier, content in (
            ("atf/one.json", b'{"schema_name":"DoclingDocument","texts":[]}'),
            ("nist/two.json", b'{"schema_name":"DoclingDocument","texts":[1]}'),
        ):
            path = self.store.approved / identifier
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        old_digest = "a" * 64
        old_receipt = self.manager.receipts / f"{old_digest}.json"
        old_receipt.write_text('{"status":"succeeded"}', encoding="utf-8")

        queued = self.manager.start_rebuild(REBUILD_CONFIRMATION)
        completed = self.wait_rebuild(queued["rebuild_id"])

        self.assertEqual(reset_calls, ["truncate"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["documents_total"], 2)
        self.assertEqual(completed["documents_succeeded"], 2)
        self.assertEqual(completed["chunk_count"], 14)
        self.assertFalse(old_receipt.exists())
        archive = self.store.root / completed["receipt_archive"]
        self.assertTrue((archive / f"{old_digest}.json").is_file())
        canonical_sources = {call[3] for call in self.calls}
        self.assertEqual(
            canonical_sources,
            {"rag://approved/atf/one.json", "rag://approved/nist/two.json"},
        )


    def test_it_collection_is_tagged_from_its_folder(self):
        _, digest = self.approve("it/dns-pihole-and-router.json")
        queued = self.manager.start("it/dns-pihole-and-router.json", digest)
        completed = self.wait(digest)
        self.assertEqual(queued["collection"], "it")
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["source_path"], "rag://approved/it/dns-pihole-and-router.json")
        self.assertEqual(self.calls[0][1], "it")

    def test_rebuild_tags_approved_it_markdown(self):
        self.manager.shutdown()
        self.manager = IngestionManager(self.store, runner=self.runner, resetter=lambda: None)
        self.manager.ensure_layout()
        path = self.store.approved / "it/harness-deploy-ct270.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Harness deploy\n\npct enter 270\n", encoding="utf-8")
        (self.store.approved / "it/README.md").write_text("instructions", encoding="utf-8")

        completed = self.wait_rebuild(self.manager.start_rebuild(REBUILD_CONFIRMATION)["rebuild_id"])

        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(
            [(call[1], call[3]) for call in self.calls],
            [("it", "rag://approved/it/harness-deploy-ct270.md")],
        )


if __name__ == "__main__":
    unittest.main()
