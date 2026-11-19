"""Lab tags come from RAG_LAB_FOLDERS, not from names in the code. No database or embedder."""
import os
import pathlib
import sys
import unittest

for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
            "OLLAMA_URL", "EMBED_MODEL"):
    os.environ.setdefault(key, "unused")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ingest  # noqa: E402


class LabFolderTests(unittest.TestCase):
    def test_parse_pairs_ignores_blanks_and_bad_items(self):
        rules = ingest.parse_lab_folders(" Work Project = Work , ,nolab=, =x, Network Team=netops")
        self.assertEqual(rules, [("/work project/", "work"), ("/network team/", "netops")])

    def test_folder_rule_tags_note(self):
        rules = ingest.parse_lab_folders("Work Project=work")
        path = pathlib.Path("/vault/Work Project/Runbook.md")
        self.assertEqual(ingest.lab_for(path, {}, rules), "work")

    def test_default_is_home_and_frontmatter_wins(self):
        rules = ingest.parse_lab_folders("Work Project=work")
        self.assertEqual(ingest.lab_for(pathlib.Path("/vault/Other/x.md"), {}, rules), "home")
        self.assertEqual(ingest.lab_for(pathlib.Path("/vault/Work Project/x.md"), {"lab": "Lab2"}, rules), "lab2")

    def test_no_rules_means_home(self):
        self.assertEqual(ingest.lab_for(pathlib.Path("/vault/Work Project/x.md"), {}, []), "home")


if __name__ == "__main__":
    unittest.main()
