"""code_lines rebuilds code items from the raw PDF's text layer.

Each test writes a small PDF with PyMuPDF and a Docling-shaped JSON whose code
item is space-joined, as Docling emits it. No database or embedder.
"""
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
            "OLLAMA_URL", "EMBED_MODEL"):
    os.environ.setdefault(key, "unused")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import code_lines  # noqa: E402
import ingest  # noqa: E402

try:
    import pymupdf
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
except ImportError:                     # pragma: no cover
    pymupdf = None

LISTING = [
    "class App(tk.Tk):",
    "    def __init__(self):",
    "        super().__init__()",
    "if __name__ == \"__main__\":",
    "    App().mainloop()",
]
PAGE_H = 400


def binary_hash(path):
    """What Docling stores in origin.binary_hash: the sha256's low 64 bits."""
    return int(hashlib.sha256(path.read_bytes()).hexdigest(), 16) & 0xFFFFFFFFFFFFFFFF


def mono_font(chars, *, clash=None):
    """A monospaced TrueType font, one distinct box outline per char.

    `clash` gives two chars the same outline, which the decoder must refuse.
    """
    names = [".notdef", "space"] + [f"g{ord(c)}" for c in chars]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({0x20: "space", **{ord(c): f"g{ord(c)}" for c in chars}})
    glyphs = {".notdef": TTGlyphPen(None).glyph(), "space": TTGlyphPen(None).glyph()}
    for i, c in enumerate(chars):
        n = i if clash is None or c not in clash else chars.index(clash[0])
        pen = TTGlyphPen(None)
        pen.moveTo((50, 0)); pen.lineTo((50, 100 + 7 * n))
        pen.lineTo((550, 100 + 7 * n)); pen.lineTo((550, 0)); pen.closePath()
        glyphs[f"g{ord(c)}"] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (600, 0) for n in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "TestMono", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    out = io.BytesIO()
    fb.save(out)
    return out.getvalue()


@unittest.skipIf(pymupdf is None, "needs PyMuPDF and fontTools")
class CodeLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for var in ("RAW_DIR", "CODE_REF_FONT_DIR"):
            self.addCleanup(os.environ.pop, var, None)
            os.environ.pop(var, None)

    # -- fixtures -----------------------------------------------------------
    def pdf(self, lines, *, font="cour", fontfile=None, spaces=True,
            footer=None, prose_after=None, collection="coding"):
        """Write the raw PDF; returns (path, Docling code bbox)."""
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=PAGE_H)
        if fontfile is not None:
            page.insert_font(fontname="F0", fontbuffer=fontfile)
            font = "F0"
        x, y, size = 72, 80, 10
        cell = pymupdf.Font("cour").text_length(" ", size) if fontfile is None else 6.0
        for i, line in enumerate(lines):
            lead = len(line) - len(line.lstrip(" "))
            if spaces:
                page.insert_text((x, y + 12 * i), line, fontname=font, fontsize=size)
            else:                   # indentation by position, no space glyphs
                page.insert_text((x + lead * cell, y + 12 * i), line.lstrip(" "),
                                 fontname=font, fontsize=size)
        bottom = y + 12 * (len(lines) - 1)
        if prose_after:
            bottom += 12
            page.insert_text((x, bottom), prose_after, fontname="helv", fontsize=size)
        if footer:
            bottom += 12
            page.insert_text((200, bottom), footer, fontname="helv", fontsize=size)
        if fontfile is not None:        # make it read as glyph IDs, like IN254
            for xref, _, ftype, *_ in page.get_fonts(full=True):
                if ftype == "Type0":
                    doc.xref_set_key(xref, "ToUnicode", "null")
        folder = self.root / "documents" / "raw" / collection
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "book.pdf"
        doc.save(path)
        bbox = {"l": x - 2, "r": 390, "t": PAGE_H - (y - 12),
                "b": PAGE_H - (bottom + 4), "coord_origin": "BOTTOMLEFT"}
        return path, bbox

    def export(self, pdf_path, bbox, text=None, *, hash_value=None, extra=()):
        doc = {
            "schema_name": "DoclingDocument", "name": "book",
            "origin": {"mimetype": "application/pdf", "filename": pdf_path.name,
                       "binary_hash": binary_hash(pdf_path) if hash_value is None
                       else hash_value},
            "texts": [{"self_ref": "#/texts/0", "label": "code", "content_layer": "body",
                       "text": text if text is not None else " ".join(
                           ln.strip() for ln in LISTING),
                       "prov": [{"page_no": 1, "bbox": bbox}]}, *extra],
            "tables": [],
        }
        doc["body"] = {"children": [{"$ref": t["self_ref"]} for t in doc["texts"]]}
        folder = self.root / "documents" / "approved" / "IN254"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "book.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path, doc

    def code_text(self, json_path, doc):
        reader = code_lines.reader_for(json_path, doc)
        self.assertIsNotNone(reader)
        with reader:
            return reader.code_text(doc["texts"][0])

    # -- readable text layer ------------------------------------------------
    def test_lines_and_indentation_from_space_glyphs(self):
        pdf, bbox = self.pdf(LISTING)
        self.assertEqual(self.code_text(*self.export(pdf, bbox)), "\n".join(LISTING))

    def test_indentation_from_position_when_no_space_glyphs(self):
        pdf, bbox = self.pdf(LISTING, spaces=False)
        self.assertEqual(self.code_text(*self.export(pdf, bbox)), "\n".join(LISTING))

    def test_page_number_dropped_and_merged_prose_kept_unindented(self):
        pdf, bbox = self.pdf(LISTING, prose_after="Then run it.", footer="[ 120 ]")
        text = self.code_text(*self.export(pdf, bbox))
        self.assertEqual(text, "\n".join(LISTING + ["Then run it."]))

    def test_unrelated_text_layer_falls_back(self):
        pdf, bbox = self.pdf(LISTING)
        json_path, doc = self.export(pdf, bbox, text="Lorem ipsum dolor sit amet " * 4)
        self.assertIsNone(self.code_text(json_path, doc))

    # -- finding the raw PDF ------------------------------------------------
    def test_raw_pdf_found_across_collections_by_verified_hash(self):
        pdf, bbox = self.pdf(LISTING, collection="coding")     # approved/IN254 vs raw/coding
        json_path, doc = self.export(pdf, bbox)
        self.assertEqual(code_lines.find_raw_pdf(json_path, doc), pdf.resolve())

    def test_raw_pdf_with_other_bytes_is_not_used(self):
        pdf, bbox = self.pdf(LISTING)
        json_path, doc = self.export(pdf, bbox, hash_value=binary_hash(pdf) ^ 1)
        self.assertIsNone(code_lines.find_raw_pdf(json_path, doc))
        self.assertIsNone(code_lines.reader_for(json_path, doc))

    def test_origin_filename_is_a_name_not_a_path(self):
        pdf, bbox = self.pdf(LISTING)
        json_path, doc = self.export(pdf, bbox)
        doc["origin"]["filename"] = "../../raw/coding/book.pdf"
        # Only the bare name is looked up inside the raw store.
        self.assertEqual(code_lines.find_raw_pdf(json_path, doc), pdf.resolve())
        os.environ["RAW_DIR"] = str(self.root / "elsewhere")
        self.assertIsNone(code_lines.find_raw_pdf(json_path, doc))

    # -- Type0 / Identity-H without ToUnicode -------------------------------
    def glyph_id_pdf(self):
        chars = sorted(set("".join(LISTING)) - {" "})
        font = mono_font(chars)
        pdf, bbox = self.pdf(LISTING, fontfile=font)
        with pymupdf.open(pdf) as d:            # the fixture really is a cipher
            plain = d[0].get_text()
            self.assertNotIn("class App", plain)
        return chars, font, pdf, bbox

    def test_glyph_ids_decoded_by_exact_outline_match(self):
        chars, font, pdf, bbox = self.glyph_id_pdf()
        json_path, doc = self.export(pdf, bbox)
        with code_lines.reader_for(json_path, doc) as reader:
            name = next(n for n, m in reader._fonts(0).items() if m != "asis")
        # The reference is looked up by the embedded font's name, minus the
        # subset tag: AMMKMG+FreeMono -> reference-fonts/FreeMono.ttf.
        refs = self.root / "documents" / "reference-fonts"
        refs.mkdir(parents=True)
        (refs / f"{name}.ttf").write_bytes(font)
        self.assertEqual(self.code_text(json_path, doc), "\n".join(LISTING))

    def test_no_reference_font_falls_back(self):
        _, _, pdf, bbox = self.glyph_id_pdf()
        self.assertIsNone(self.code_text(*self.export(pdf, bbox)))

    def test_ambiguous_outline_falls_back(self):
        chars, font, pdf, bbox = self.glyph_id_pdf()
        json_path, doc = self.export(pdf, bbox)
        with code_lines.reader_for(json_path, doc) as reader:
            name = next(n for n, m in reader._fonts(0).items() if m != "asis")
        refs = self.root / "documents" / "reference-fonts"
        refs.mkdir(parents=True)
        # "a" and "s" share an outline in the reference: neither can be decoded.
        (refs / f"{name}.ttf").write_bytes(mono_font(chars, clash=("a", "s")))
        self.assertIsNone(self.code_text(json_path, doc))

    # -- through ingest.read_docling_json -----------------------------------
    def test_ingest_indexes_code_with_line_breaks(self):
        fragment = ["        return x", "    def b(self):", "        pass"]
        pdf, bbox = self.pdf(fragment)
        json_path, _ = self.export(pdf, bbox, text="return x def b(self): pass")
        _, records = ingest.read_docling_json(json_path)
        # First-line indentation survives packing.
        self.assertIn("    return x\ndef b(self):\n    pass", records[0]["text"])

    def test_ingest_without_raw_pdf_keeps_docling_text(self):
        pdf, bbox = self.pdf(LISTING)
        json_path, _ = self.export(pdf, bbox)
        pdf.unlink()
        _, records = ingest.read_docling_json(json_path)
        self.assertNotIn("\n", records[0]["text"])
        self.assertIn("class App(tk.Tk): def __init__(self):", records[0]["text"])


if __name__ == "__main__":
    unittest.main()
