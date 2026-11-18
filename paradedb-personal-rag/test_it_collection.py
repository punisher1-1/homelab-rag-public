"""The `it` collection: folder tagging and the docs/it-seed runbook drafts.
No database or embedder."""
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

# ingest.py reads its connection settings at import time.
for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
            "OLLAMA_URL", "EMBED_MODEL"):
    os.environ.setdefault(key, "unused")
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ingest  # noqa: E402

SEED = HERE.parent / "docs" / "it-seed"
RUNBOOKS = sorted(p for p in SEED.glob("*.md") if p.name.lower() != "readme.md")


# The runbook drafts stay in the private repository; the public snapshot has no docs/it-seed.
@unittest.skipUnless(SEED.is_dir(), "docs/it-seed is not in this copy")
class ItCollectionTests(unittest.TestCase):
    def test_approved_it_folder_tags_runbooks_and_skips_readme(self):
        with tempfile.TemporaryDirectory() as folder:
            approved = pathlib.Path(folder) / "approved"
            (approved / "it").mkdir(parents=True)
            (approved / "it301").mkdir()
            for runbook in RUNBOOKS:
                shutil.copy(runbook, approved / "it" / runbook.name)
            shutil.copy(SEED / "README.md", approved / "it" / "README.md")
            (approved / "it301" / "week4.md").write_text("# Week 4\n\nFiles.\n")

            found, superseded = ingest.gather_drop(str(approved))

        tags = sorted((p.name, c) for p, c in found)
        self.assertEqual(tags, sorted([(r.name, "it") for r in RUNBOOKS]
                                      + [("week4.md", "it301")]))
        self.assertEqual(superseded, [])

    def test_seed_runbooks_chunk_by_section(self):
        self.assertGreaterEqual(len(RUNBOOKS), 3)
        for runbook in RUNBOOKS:
            with self.subTest(runbook=runbook.name):
                title, body, meta = ingest.read_markdown(runbook)
                records = ingest.chunk_plain_with_sections(title, body)
                self.assertEqual(meta.get("status"), "draft")
                self.assertTrue(title and title != runbook.stem)
                self.assertTrue(records)
                sections = {r["section"] for r in records}
                self.assertGreaterEqual(len(sections - {""}), 3)
                self.assertTrue(all(r["page"] is None for r in records))


if __name__ == "__main__":
    unittest.main()
