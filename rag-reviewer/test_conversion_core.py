import importlib.util
import json
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
from conversion_core import ConversionError, ConversionManager
from reviewer_core import ReviewerStore, sha256_file


def converted_document(filename="source.pdf"):
    return {
        "schema_name": "DoclingDocument",
        "origin": {"filename": filename},
        "texts": [{"label": "text", "text": "Readable text", "prov": [{"page_no": 1}]}],
    }


class ConversionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReviewerStore(pathlib.Path(self.temp.name) / "rag-store")
        self.store.ensure_layout()
        self.calls = []

        def runner(path):
            self.calls.append(path)
            return {
                "status": "success",
                "document": {"json_content": converted_document(path.name)},
                "processing_time": 1.25,
                "errors": [],
            }

        self.manager = ConversionManager(self.store, runner=runner)
        self.manager.ensure_layout()

    def tearDown(self):
        self.manager.shutdown()
        self.temp.cleanup()

    def add(self, identifier="general/test.pdf", content=b"%PDF-test"):
        path = self.manager.raw / identifier
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def wait(self, digest):
        for _ in range(100):
            receipt = self.manager.status(digest)
            if receipt["status"] in {"succeeded", "failed"}:
                return receipt
            time.sleep(0.01)
        self.fail("conversion did not finish")

    def test_raw_requires_two_matching_scans(self):
        self.add()
        self.assertEqual(self.manager.list_raw(), [])
        items = self.manager.list_raw()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["document"], "general/test.pdf")
        self.assertEqual(len(items[0]["sha256"]), 64)

    def test_conversion_writes_json_and_receipt(self):
        path = self.add()
        digest = sha256_file(path)
        queued = self.manager.start("general/test.pdf", digest)
        receipt = self.wait(digest)
        output = self.store.incoming / "general/test.json"
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["output_document"], "general/test.json")
        self.assertEqual(receipt["output_sha256"], sha256_file(output))
        self.assertEqual(json.loads(output.read_text())["schema_name"], "DoclingDocument")
        self.assertEqual(len(self.calls), 1)

    def test_success_is_idempotent(self):
        path = self.add()
        digest = sha256_file(path)
        self.manager.start("general/test.pdf", digest)
        first = self.wait(digest)
        second = self.manager.start("general/test.pdf", digest)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)

    def test_markdown_runbook_converts_into_the_it_collection(self):
        path = self.add("it/pocket-id-oidc.md", b"# Pocket ID\n\nSign-in steps.\n")
        self.manager.list_raw()
        (item,) = self.manager.list_raw()
        self.assertEqual(item["collection"], "it")
        self.manager.start("it/pocket-id-oidc.md", item["sha256"])
        receipt = self.wait(sha256_file(path))
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["output_document"], "it/pocket-id-oidc.json")
        self.assertTrue((self.store.incoming / "it/pocket-id-oidc.json").is_file())

    def test_path_escape_and_unsupported_type_are_rejected(self):
        with self.assertRaises(ConversionError):
            self.manager.resolve_raw("../secret.pdf", must_exist=False)
        with self.assertRaises(ConversionError):
            self.manager.resolve_raw("general/tool.exe", must_exist=False)

    def test_stale_sha_is_rejected(self):
        self.add()
        with self.assertRaises(ConversionError):
            self.manager.start("general/test.pdf", "0" * 64)

    def test_failure_requires_explicit_retry(self):
        self.manager.shutdown()
        attempts = []

        def broken_then_ok(path):
            attempts.append(path)
            if len(attempts) == 1:
                raise RuntimeError("converter unavailable")
            return {
                "status": "success",
                "document": {"json_content": converted_document(path.name)},
                "processing_time": 1,
                "errors": [],
            }

        self.manager = ConversionManager(self.store, runner=broken_then_ok)
        self.manager.ensure_layout()
        path = self.add()
        digest = sha256_file(path)
        self.manager.start("general/test.pdf", digest)
        failed = self.wait(digest)
        unchanged = self.manager.start("general/test.pdf", digest)
        retried = self.manager.start("general/test.pdf", digest, retry=True)
        completed = self.wait(digest)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(unchanged["status"], "failed")
        self.assertEqual(retried["attempt"], 2)
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(len(attempts), 2)

    def test_existing_different_output_fails_closed(self):
        path = self.add()
        output = self.store.incoming / "general/test.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"schema_name":"DoclingDocument","texts":[]}', encoding="utf-8")
        digest = sha256_file(path)
        self.manager.start("general/test.pdf", digest)
        receipt = self.wait(digest)
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("different content", receipt["error"])

    def test_restart_recovers_interrupted_job_for_automatic_retry(self):
        path = self.add()
        digest = sha256_file(path)
        self.store._atomic_json(
            self.manager.receipts / f"{digest}.json",
            {
                "conversion_id": digest,
                "document": "general/test.pdf",
                "sha256": digest,
                "status": "running",
                "attempt": 1,
            },
        )

        recovered = self.manager.recover_interrupted()
        self.assertEqual(recovered, 1)
        self.assertEqual(self.manager.list_raw(), [])
        ready = self.manager.list_raw()
        self.assertEqual(len(ready), 1)
        self.assertTrue(ready[0]["retry"])
        self.assertEqual(ready[0]["previous_status"], "interrupted")

        queued = self.manager.start(
            ready[0]["document"], ready[0]["sha256"], retry=ready[0]["retry"]
        )
        completed = self.wait(digest)
        self.assertEqual(queued["attempt"], 2)
        self.assertEqual(completed["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
