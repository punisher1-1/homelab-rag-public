import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


PROJECT = pathlib.Path(__file__).resolve().parents[1]
PARSER = PROJECT / "paradedb-personal-rag" / "parse_check.py"
SPEC = importlib.util.spec_from_file_location("parse_check", PARSER)
parse_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parse_check)
sys.modules["parse_check"] = parse_check
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from reviewer_core import ReviewError, ReviewerStore
from replay_review import replay_failed_review


def payload(text="This is clean readable text."):
    return {
        "schema_name": "DoclingDocument",
        "origin": {"filename": "source.pdf"},
        "texts": [{"label": "text", "text": text, "prov": [{"page_no": 1}]}],
    }


class ReviewerStoreTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"N8N_FORM_ORIGIN": "http://192.0.2.10:5678"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReviewerStore(pathlib.Path(self.temp.name) / "rag-store")
        self.store.ensure_layout()

    def tearDown(self):
        self.temp.cleanup()

    def add(self, identifier="general/test.json", text=None):
        path = self.store.incoming / identifier
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload(text or "This is clean readable text.")), encoding="utf-8")
        return path

    def test_incoming_requires_two_matching_scans(self):
        self.add()
        self.assertEqual(self.store.list_incoming(), [])
        self.assertEqual(len(self.store.list_incoming()), 1)

    def test_path_escape_is_rejected(self):
        with self.assertRaises(ReviewError):
            self.store.resolve("incoming", "../secret.json", must_exist=False)

    def test_stale_sha_is_rejected(self):
        self.add()
        with self.assertRaises(ReviewError):
            self.store.preflight("general/test.json", "0" * 64)

    def test_pass_review_can_promote_and_replay(self):
        path = self.add()
        digest = self.store.preflight("general/test.json")["sha256"]
        self.store.record_review("general/test.json", digest, {
            "deterministic_status": "PASS",
            "model_status": "PASS",
            "final_status": "PASS",
            "summary": "clean",
        })
        first = self.store.move_reviewed("general/test.json", digest, "approved")
        second = self.store.move_reviewed("general/test.json", digest, "approved")
        self.assertEqual(first["status"], "moved")
        self.assertEqual(second["status"], "unchanged")
        self.assertFalse(path.exists())

    def test_warn_requires_human_approval(self):
        self.add(text="图SE")
        preflight = self.store.preflight("general/test.json")
        with self.assertRaises(ReviewError):
            self.store.record_review("general/test.json", preflight["sha256"], {
                "deterministic_status": "WARN",
                "model_status": "PASS",
                "final_status": "PASS",
            })

    def test_verified_display_text_routes_to_human_approval(self):
        self.add(text="G L O B A L  S T A N D A R D")
        preflight = self.store.preflight("general/test.json")
        self.assertEqual(preflight["status"], "WARN")
        self.assertEqual(preflight["checks"]["text_integrity"], "WARN")
        with self.assertRaises(ReviewError):
            self.store.record_review("general/test.json", preflight["sha256"], {
                "deterministic_status": "WARN",
                "model_status": "PASS",
                "final_status": "PASS",
            })

    def test_human_can_approve_warn_then_promote(self):
        self.add(text="图SE")
        preflight = self.store.preflight("general/test.json")
        stored = self.store.record_review("general/test.json", preflight["sha256"], {
            "deterministic_status": "WARN",
            "model_status": "PASS",
            "final_status": "PASS",
            "human_approved": True,
            "human_decision": "Approve",
            "reviewer_notes": "Reviewed in n8n.",
        })
        moved = self.store.move_reviewed(
            "general/test.json", preflight["sha256"], "approved"
        )
        self.assertTrue(stored["human_approved"])
        self.assertEqual(moved["status"], "moved")

    def test_repeated_review_returns_existing_record(self):
        self.add()
        digest = self.store.preflight("general/test.json")["sha256"]
        request = {
            "deterministic_status": "PASS",
            "model_status": "PASS",
            "final_status": "PASS",
            "summary": "clean",
        }
        first = self.store.record_review("general/test.json", digest, request)
        second = self.store.record_review("general/test.json", digest, request)
        self.assertEqual(first, second)

    def test_replay_archives_old_failure_without_approving(self):
        self.add(text="G L O B A L  S T A N D A R D")
        digest = self.store.preflight("general/test.json")["sha256"]
        receipt = self.store.reviews / f"{digest}.json"
        old = {"document": "general/test.json", "sha256": digest,
               "deterministic_status": "FAIL", "final_status": "FAIL"}
        receipt.write_text(json.dumps(old), encoding="utf-8")
        self.store.list_incoming()
        self.assertEqual(self.store.list_incoming(), [])

        result = replay_failed_review(self.store, "general/test.json", digest)

        self.assertEqual(result["preflight_status"], "WARN")
        self.assertFalse(receipt.exists())
        self.assertEqual(json.loads(pathlib.Path(result["archived_receipt"]).read_text()), old)
        self.assertEqual(self.store.list_incoming()[0]["sha256"], digest)
        self.assertFalse((self.store.approved / "general/test.json").exists())

    def test_replay_refuses_a_document_that_still_fails(self):
        self.add(text="The agency accepts no liabilitv for this.")
        digest = self.store.preflight("general/test.json")["sha256"]
        receipt = self.store.reviews / f"{digest}.json"
        receipt.write_text(json.dumps({"document": "general/test.json",
                                       "sha256": digest, "final_status": "FAIL"}),
                           encoding="utf-8")
        with self.assertRaises(ReviewError):
            replay_failed_review(self.store, "general/test.json", digest)
        self.assertTrue(receipt.exists())

    def test_replay_pending_warn_requires_explicit_mode(self):
        self.add(text="G L O B A L  S T A N D A R D")
        digest = self.store.preflight("general/test.json")["sha256"]
        receipt = self.store.reviews / f"{digest}.json"
        old = {"document": "general/test.json", "sha256": digest,
               "final_status": "WARN", "human_approved": False,
               "human_decision": None}
        receipt.write_text(json.dumps(old), encoding="utf-8")
        with self.assertRaises(ReviewError):
            replay_failed_review(self.store, "general/test.json", digest)
        self.assertTrue(receipt.exists())
        result = replay_failed_review(
            self.store, "general/test.json", digest, allow_pending_warn=True
        )
        self.assertEqual(result["preflight_status"], "WARN")
        self.assertFalse(receipt.exists())

    def test_replay_never_archives_an_approved_review(self):
        self.add(text="G L O B A L  S T A N D A R D")
        digest = self.store.preflight("general/test.json")["sha256"]
        receipt = self.store.reviews / f"{digest}.json"
        receipt.write_text(json.dumps({"document": "general/test.json",
                                       "sha256": digest, "final_status": "PASS",
                                       "human_approved": True,
                                       "human_decision": "Approve"}), encoding="utf-8")
        with self.assertRaises(ReviewError):
            replay_failed_review(
                self.store, "general/test.json", digest, allow_pending_warn=True
            )
        self.assertTrue(receipt.exists())

    def test_warning_replay_clears_stale_approval_link(self):
        self.add(text="G L O B A L  S T A N D A R D")
        digest = self.store.preflight("general/test.json")["sha256"]
        self.store.record_review("general/test.json", digest, {
            "deterministic_status": "WARN", "model_status": "WARN",
            "final_status": "WARN", "human_approved": False,
        })
        url = "http://192.0.2.10:5678/form-waiting/12345"
        self.store.publish_approval_link("general/test.json", digest, url)
        replay_failed_review(
            self.store, "general/test.json", digest, allow_pending_warn=True
        )
        self.assertFalse((self.store.pending_approvals / f"{digest}.json").exists())
        self.assertIn("No pending approvals", (self.store.pending_approvals / "index.html").read_text())

    def test_warning_replay_mode_does_not_archive_fail(self):
        self.add(text="G L O B A L  S T A N D A R D")
        digest = self.store.preflight("general/test.json")["sha256"]
        receipt = self.store.reviews / f"{digest}.json"
        receipt.write_text(json.dumps({"document": "general/test.json",
                                       "sha256": digest, "final_status": "FAIL"}),
                           encoding="utf-8")
        with self.assertRaises(ReviewError):
            replay_failed_review(
                self.store, "general/test.json", digest, allow_pending_warn=True
            )
        self.assertTrue(receipt.exists())

    def test_private_approval_inbox_and_human_cleanup(self):
        identifier = "general/test&name.json"
        self.add(identifier, text="G L O B A L  S T A N D A R D")
        digest = self.store.preflight(identifier)["sha256"]
        self.store.record_review(identifier, digest, {
            "deterministic_status": "WARN", "model_status": "WARN",
            "final_status": "WARN", "human_approved": False,
            "summary": "Title lettering",
        })
        url = "http://192.0.2.10:5678/form-waiting/12345"
        result = self.store.publish_approval_link(identifier, digest, url)
        inbox = self.store.pending_approvals / "index.html"
        self.assertEqual(result["final_status"], "WARN")
        self.assertIn("test&amp;name.json", inbox.read_text())
        self.assertIn(url, inbox.read_text())
        self.assertEqual(
            self.store.publish_approval_link(identifier, digest, url), result
        )
        self.store.record_review(identifier, digest, {
            "deterministic_status": "WARN", "model_status": "WARN",
            "final_status": "PASS", "human_approved": True,
            "human_decision": "Approve", "summary": "Title lettering",
        })
        self.assertFalse((self.store.pending_approvals / f"{digest}.json").exists())
        self.assertIn("No pending approvals", inbox.read_text())

    def test_missing_approval_cleanup_refreshes_stale_html(self):
        inbox = self.store.pending_approvals / 'index.html'
        inbox.write_text('Old pending link')
        self.store.remove_approval_link('a' * 64)
        self.store.remove_approval_link('a' * 64)
        self.assertIn('No pending approvals', inbox.read_text())

    def test_inbox_tolerates_entry_disappearing_after_listing(self):
        from unittest.mock import patch
        stale = self.store.pending_approvals / ('a' * 64 + '.json')
        with patch.object(pathlib.Path, 'glob', return_value=iter([stale])):
            self.store._render_approval_inbox()
        self.assertIn('No pending approvals',
                      (self.store.pending_approvals / 'index.html').read_text())

    def test_inbox_does_not_hide_permission_or_invalid_json_errors(self):
        from unittest.mock import patch
        entry = self.store.pending_approvals / ('a' * 64 + '.json')
        entry.write_text('not JSON')
        with self.assertRaises(json.JSONDecodeError):
            self.store._render_approval_inbox()
        with patch.object(pathlib.Path, 'unlink', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.store.remove_approval_link('a' * 64)

    def test_repeating_saved_approval_without_pending_entry_succeeds(self):
        identifier = 'general/test.json'
        self.add(identifier)
        digest = self.store.preflight(identifier)['sha256']
        decision = {'deterministic_status':'PASS', 'model_status':'WARN',
                    'final_status':'PASS', 'human_approved':True,
                    'human_decision':'Approve'}
        first = self.store.record_review(identifier, digest, decision)
        second = self.store.record_review(identifier, digest, decision)
        self.assertEqual(first, second)
        self.assertEqual(second['final_status'], 'PASS')
        self.assertTrue((self.store.incoming / identifier).is_file())

    def test_approval_inbox_rejects_public_or_stale_links(self):
        self.add(text="G L O B A L  S T A N D A R D")
        digest = self.store.preflight("general/test.json")["sha256"]
        self.store.record_review("general/test.json", digest, {
            "deterministic_status": "WARN", "model_status": "WARN",
            "final_status": "WARN", "human_approved": False,
        })
        for url in (
            "https://n8n.example.com/form-waiting/12345",
            "http://192.0.2.10:5678/webhook-waiting/12345",
            "http://192.0.2.10:5678/form-waiting/12345#fragment",
        ):
            with self.assertRaises(ReviewError):
                self.store.publish_approval_link("general/test.json", digest, url)
        with self.assertRaises(ReviewError):
            self.store.publish_approval_link(
                "general/test.json", "0" * 64,
                "http://192.0.2.10:5678/form-waiting/12345"
            )


if __name__ == "__main__":
    unittest.main()
