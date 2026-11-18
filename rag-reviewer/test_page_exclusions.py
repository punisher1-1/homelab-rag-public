import copy
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "paradedb-personal-rag"))
sys.path.insert(0, str(PROJECT / "rag-reviewer"))
from page_exclusions import exclude_pdf_pages
from reviewer_core import ReviewerStore, ReviewError, sha256_file
import parse_check


def document():
    return {
        "schema_name": "DoclingDocument", "name": "Example",
        "origin": {"filename": "source.pdf"},
        "pages": {str(p): {"page_no": p} for p in (1, 67, 76, 77, 78)},
        "body": {"self_ref": "#/body", "children": [
            {"$ref": "#/texts/0"}, {"$ref": "#/groups/0"},
            {"$ref": "#/tables/0"}, {"$ref": "#/pictures/0"}, {"$ref": "#/texts/4"}]},
        "groups": [{"self_ref": "#/groups/0", "children": [
            {"$ref": "#/texts/1"}, {"$ref": "#/texts/2"}, {"$ref": "#/texts/3"}]}],
        "texts": [
            {"self_ref": f"#/texts/{i}", "label": "text", "text": text,
             "parent": {"$ref": "#/body"}, "prov": [{"page_no": p}]}
            for i, (p, text) in enumerate([
                (1, "Useful reference information."),
                (67, "B r o k e n a b c d e f g h i j k l m n"),
                (76, "Fake address and example form."),
                (77, "20φ.55"), (78, "Retained instructions after the sample forms.")])],
        "tables": [{"self_ref": "#/tables/0", "label": "table", "prov": [{"page_no": 76}],
                    "data": {"num_rows": 1, "num_cols": 1, "table_cells": [
                        {"text": "EXCLUDED TABLE", "start_row_offset_idx": 0,
                         "end_row_offset_idx": 1, "start_col_offset_idx": 0,
                         "end_col_offset_idx": 1}]}}],
        "pictures": [{"self_ref": "#/pictures/0", "label": "picture",
                      "prov": [{"page_no": 77}]}],
    }


class PageExclusionTests(unittest.TestCase):
    def test_filters_all_item_types_and_rewrites_references_without_renumbering_pages(self):
        original = document()
        saved = copy.deepcopy(original)
        curated = exclude_pdf_pages(original, [67, 76, 77])
        self.assertEqual(original, saved)
        self.assertEqual([t["prov"][0]["page_no"] for t in curated["texts"]], [1, 78])
        self.assertEqual(curated["texts"][1]["self_ref"], "#/texts/1")
        self.assertEqual(curated["body"]["children"][-1], {"$ref": "#/texts/1"})
        self.assertEqual(curated["groups"][0]["children"], [])
        self.assertEqual(curated["tables"], [])
        self.assertEqual(curated["pictures"], [])
        self.assertEqual(curated["pages"], saved["pages"])

    def test_rejects_bad_pages_and_ambiguous_cross_page_items(self):
        for pages in ([], [0], [-1], [True], ["67"], [999]):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                exclude_pdf_pages(document(), pages)
        original = document()
        original["tables"][0]["prov"].append({"page_no": 78})
        with self.assertRaisesRegex(ValueError, "spans excluded and retained"):
            exclude_pdf_pages(original, [76])

    def test_modern_cref_references(self):
        original = json.loads(json.dumps(document()).replace('"$ref"', '"cref"'))
        curated = exclude_pdf_pages(original, [67, 76, 77])
        self.assertEqual(curated["body"]["children"][-1], {"cref": "#/texts/1"})
        self.assertEqual(curated["groups"][0]["children"], [])

    def test_source_flags_do_not_bypass_quality_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            source = document()
            source["rag_curation"] = {"page_exclusions": [{"pdf_pages": [67, 76, 77]}]}
            path = pathlib.Path(folder) / "source.json"
            path.write_text(json.dumps(source))
            self.assertEqual(parse_check.assess_file(path)["status"], "FAIL")

    def test_rejects_excluding_all_content(self):
        with self.assertRaisesRegex(ValueError, "all document content"):
            exclude_pdf_pages(document(), [1, 67, 76, 77, 78])

    def test_unpaginated_children_cannot_leak(self):
        original = document()
        original["pictures"][0]["children"] = [{"$ref": "#/texts/2"}]
        original["texts"][2]["prov"] = []
        curated = exclude_pdf_pages(original, [77])
        self.assertNotIn("Fake address", json.dumps(curated))
        original["texts"][2]["prov"] = [{"page_no": 78}]
        with self.assertRaisesRegex(ValueError, "retained content inside"):
            exclude_pdf_pages(original, [77])

    def test_operator_curation_archives_bytes_invalidates_receipt_and_rechecks_retained_content(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReviewerStore(root)
            store.ensure_layout()
            path = store.incoming / "payroll/example.json"
            path.parent.mkdir()
            path.write_text(json.dumps(document()))
            original = path.read_bytes()
            digest = sha256_file(path)
            self.assertEqual(store.preflight("payroll/example.json")["status"], "FAIL")
            store.record_review("payroll/example.json", digest, {
                "deterministic_status": "FAIL", "model_status": "FAIL", "final_status": "FAIL",
                "summary": "Review failed", "findings": [{"page": 67, "snippet": "<script>bad</script>"}]})
            self.assertEqual(len(store.list_failures()), 1)
            self.assertIn("&lt;script&gt;", (store.failures / "index.html").read_text())
            for bad_sha, confirmation in [("0" * 64, "EXCLUDE PDF PAGES"), (digest, "yes")]:
                with self.assertRaises(ReviewError):
                    store.exclude_pages("payroll/example.json", bad_sha, [67], "Example", confirmation)
                self.assertEqual(path.read_bytes(), original)
            result = store.exclude_pages("payroll/example.json", digest, [67, 76, 77],
                                         "Illustrative example forms", "EXCLUDE PDF PAGES")
            self.assertNotEqual(result["sha256"], digest)
            self.assertEqual(pathlib.Path(result["archived_original"]).read_bytes(), original)
            self.assertTrue((store.reviews / f"{digest}.json").is_file())
            self.assertFalse((store.reviews / f"{result['sha256']}.json").exists())
            self.assertEqual(store.list_failures(), [])
            self.assertEqual(store.list_incoming(), [])
            self.assertEqual(len(store.list_incoming()), 1)
            self.assertEqual(store.preflight("payroll/example.json")["status"], "PASS")
            self.assertEqual(parse_check.assess_file(path)["text_items_scored"], 2)
            # Exercise the actual production chunker, not a stand-in writer.
            with patch.dict(os.environ, {"DB_HOST": "unused", "DB_PORT": "5432",
                        "DB_NAME": "unused", "DB_USER": "unused", "DB_PASSWORD": "unused",
                        "OLLAMA_URL": "http://unused", "EMBED_MODEL": "unused"}):
                import ingest
                _, records = ingest.read_docling_json(path)
            self.assertEqual({r["page"] for r in records}, {1, 78})
            self.assertNotIn("EXCLUDED TABLE", str(records))
            self.assertNotIn("Fake address", str(records))

    def test_failure_inbox_backfills_only_current_failed_documents(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReviewerStore(root)
            store.ensure_layout()
            path = store.incoming / "payroll/example.json"
            path.parent.mkdir()
            path.write_text(json.dumps(document()))
            digest = sha256_file(path)
            receipt = {"document": "payroll/example.json", "sha256": digest,
                       "final_status": "FAIL", "summary": "Blocked", "findings": []}
            (store.reviews / f"{digest}.json").write_text(json.dumps(receipt))
            store.backfill_failures()
            store.backfill_failures()
            self.assertEqual(len(store.list_failures()), 1)


if __name__ == "__main__":
    unittest.main()
