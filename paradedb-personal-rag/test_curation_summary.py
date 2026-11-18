"""Chunks carry a small curation summary, not the full repair history."""
import json
import os
import pathlib
import sys
import unittest

for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
            "OLLAMA_URL", "EMBED_MODEL"):
    os.environ.setdefault(key, "unused")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ingest  # noqa: E402


class CurationSummaryTests(unittest.TestCase):
    def test_summarises_repairs_and_exclusions(self):
        curation = {
            "page_exclusions": [
                {"pdf_pages": [9, 3], "source_sha256": "a" * 64, "recorded_at": "2026-10-01T00:00:00"},
                {"pdf_pages": [3, 12], "source_sha256": "b" * 64, "recorded_at": "2026-10-02T00:00:00"},
            ],
            "repairs": [{
                "source_sha256": "c" * 64, "recorded_at": "2026-10-03T00:00:00", "reason": "x",
                "changes": [{"action": "replace", "before": "θInterface", "after": "Interface",
                             "provenance": [{"bbox": {"l": 1}}]} for _ in range(5)]
                           + [{"action": "verify", "before": "H L O O L"}] * 2,
            }],
        }
        summary = ingest.curation_summary(curation)
        self.assertEqual(summary["repair_batches"], 1)
        self.assertEqual(summary["repair_actions"], {"replace": 5, "verify": 2})
        self.assertEqual(summary["excluded_pdf_pages"], [3, 9, 12])
        self.assertEqual(summary["latest_source_sha256"], "c" * 64)
        text = json.dumps(summary)
        self.assertNotIn("Interface", text)
        self.assertNotIn("bbox", text)
        self.assertLess(len(text), len(json.dumps(curation)))

    def test_absent_or_malformed_history(self):
        self.assertIsNone(ingest.curation_summary(None))
        self.assertIsNone(ingest.curation_summary({}))
        self.assertIsNone(ingest.curation_summary("junk"))
        summary = ingest.curation_summary({"repairs": ["junk", {"changes": None}]})
        self.assertEqual(summary["repair_batches"], 1)
        self.assertEqual(summary["repair_actions"], {})
        self.assertIsNone(summary["latest_source_sha256"])


if __name__ == "__main__":
    unittest.main()
