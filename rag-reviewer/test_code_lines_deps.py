"""The reviewer image must keep code_lines.py's PDF text-layer recovery.

code_lines.py turns itself into a silent no-op when PyMuPDF is missing, so an
image without it ingests code listings flattened onto one line again. Run these
inside the candidate container as well as locally.
"""
import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock


HERE = pathlib.Path(__file__).resolve().parent
# /app in the container; paradedb-personal-rag/ in a checkout.
CODE_LINES = next(
    p for p in (HERE / "code_lines.py", HERE.parent / "paradedb-personal-rag" / "code_lines.py")
    if p.is_file()
)
SPEC = importlib.util.spec_from_file_location("code_lines", CODE_LINES)
code_lines = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(code_lines)


class CodeLineRecoveryDependencyTests(unittest.TestCase):
    def test_pymupdf_is_installed(self):
        self.assertIsNotNone(
            code_lines.pymupdf,
            "PyMuPDF missing: ingested code items lose their line breaks",
        )

    def test_fonttools_is_installed(self):
        # Needed to decode cipher fonts with no ToUnicode map (IN254's FreeMono).
        from fontTools.ttLib import TTFont  # noqa: F401

    def test_requirements_declare_both(self):
        requirements = HERE / "requirements.txt"
        if not requirements.is_file():
            self.skipTest("requirements.txt is not copied into the image")
        names = {
            line.split("#")[0].strip().split(">")[0].split("=")[0].split("<")[0].lower()
            for line in requirements.read_text(encoding="utf-8").splitlines()
        }
        self.assertIn("pymupdf", names)
        self.assertIn("fonttools", names)

    def test_paths_resolve_from_the_reviewer_store(self):
        with tempfile.TemporaryDirectory() as temp:
            documents = pathlib.Path(temp).resolve() / "rag-store" / "documents"
            export = documents / "approved" / "coding" / "in254.json"
            export.parent.mkdir(parents=True)
            export.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"RAW_DIR": "", "CODE_REF_FONT_DIR": ""}):
                self.assertEqual(code_lines.raw_root_for(export), documents / "raw")


if __name__ == "__main__":
    unittest.main()
