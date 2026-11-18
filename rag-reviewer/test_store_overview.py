import json
import pathlib
import tempfile
import unittest

import store_overview


def put(root: pathlib.Path, rel: str, data) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


class StoreOverviewTests(unittest.TestCase):
    def test_statuses_and_needs_you(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            a, b, c, d = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
            # Ingested, reviewed twice.
            put(root, "documents/approved/it/good.json", "{}")
            put(root, f"reviews/{'0' * 64}.json", {"document": "it/good.json", "sha256": "0" * 64,
                                                  "final_status": "FAIL", "recorded_at": "2026-01-01"})
            put(root, f"reviews/{a}.json", {"document": "it/good.json", "sha256": a,
                                           "final_status": "PASS", "recorded_at": "2026-01-02"})
            put(root, f"ingestions/{a}.json", {"document": "it/good.json", "status": "succeeded",
                                              "chunk_count": 7, "requested_at": "2026-01-02"})
            # Blocked with a repair page.
            put(root, "documents/incoming/it/bad.json", "{}")
            put(root, f"reviews/{b}.json", {"document": "it/bad.json", "sha256": b,
                                           "final_status": "FAIL", "recorded_at": "2026-01-03"})
            put(root, f"reviews/failures/{b}.json", {"sha256": b, "status": "open"})
            put(root, f"reviews/repairs/{b}/index.html", "<p>")
            # Waiting for approval.
            put(root, "documents/incoming/it/warn.json", "{}")
            put(root, f"reviews/{c}.json", {"document": "it/warn.json", "sha256": c,
                                           "final_status": "WARN", "human_decision": None})
            # Approved but ingest failed.
            put(root, "documents/approved/it/broke.json", "{}")
            put(root, f"reviews/{d}.json", {"document": "it/broke.json", "sha256": d, "final_status": "PASS"})
            put(root, f"ingestions/{d}.json", {"document": "it/broke.json", "status": "failed"})
            # Legacy approved file and a Docling picture folder.
            put(root, "documents/approved/docker/old.md", "x")
            put(root, "documents/approved/docker/artifacts/pic.png", "x")

            data = store_overview.collect(root)
            status = {r["name"]: r for r in data["rows"]}
            self.assertEqual(status["good.json"]["status"], "Ingested (7 chunks)")
            self.assertEqual(status["good.json"]["tries"], 2)
            self.assertEqual(status["bad.json"]["status"], "Blocked: needs repair")
            self.assertEqual(status["bad.json"]["link"][1], f"reviews/repairs/{b}/index.html")
            self.assertEqual(status["warn.json"]["status"], "Waiting for your approval")
            self.assertEqual(status["broke.json"]["status"], "Approved, ingest FAILED")
            self.assertEqual({r["name"] for r in data["rows"] if r["needs_you"]},
                             {"bad.json", "warn.json", "broke.json"})
            self.assertEqual(data["legacy"], [("docker", 1)])

            page = store_overview.render(data)
            self.assertIn("Needs you", page)
            self.assertNotIn("<script", page)


if __name__ == "__main__":
    unittest.main()
