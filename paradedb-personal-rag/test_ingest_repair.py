"""read_docling_json indexes repaired code text. No database or embedder."""
import json
import os
import pathlib
import sys
import tempfile
import unittest

# ingest.py reads its connection settings at import time.
for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
            "OLLAMA_URL", "EMBED_MODEL"):
    os.environ.setdefault(key, "unused")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ingest  # noqa: E402


def item(ref, label, text, page=1, **extra):
    return dict({"self_ref": ref, "label": label, "text": text,
                 "prov": [{"page_no": page}]}, **extra)


class ReadDoclingRepairTests(unittest.TestCase):
    def read(self, texts, tables=()):
        doc = {
            "schema_name": "DoclingDocument",
            "name": "book",
            "texts": texts,
            "tables": list(tables),
            "body": {"children": [{"$ref": t["self_ref"]} for t in texts]
                     + [{"$ref": t["self_ref"]} for t in tables]},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "book.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            return ingest.read_docling_json(path)

    def test_code_items_are_indexed_repaired(self):
        _, records = self.read([
            item("#/texts/0", "section_header", "Creating your first Tkinter app", level=1),
            item("#/texts/1", "text", "The modification to the _init__ method is similar."),
            item("#/texts/2", "code",
                 'class App(tk.Tk): def init__(self): super().__init_() '
                 'if _name_ II __main___": ( ) dd∀ = dde app.mainloop()'),
        ])
        body = "\n".join(str(r["text"]) for r in records)
        self.assertIn('def __init__(self): super().__init__() '
                      'if __name__ == "__main__": app = App() app.mainloop()', body)
        # Prose is left as printed; only code items get dunder repair.
        self.assertIn("the _init__ method", body)

    def test_caret_lookalike_is_fixed_in_sections_body_and_tables(self):
        table = {
            "self_ref": "#/tables/0", "label": "table", "prov": [{"page_no": 2}],
            "data": {"grid": [[{"text": "Option"}, {"text": "Format"}],
                              [{"text": "serial[n]"}, {"text": "(/dev/[ˆ,]+|socket)"}]]},
        }
        _, records = self.read([
            item("#/texts/0", "section_header", "--directory ˆhttps?://.*", level=1),
            item("#/texts/1", "list_item", "--escape \\ˆ?[a-z] ( default = ˆa )"),
        ], tables=[table])
        self.assertIn("directory ^https?://", records[0]["section"])
        everything = "\n".join(f"{r['text']}{r['section'] or ''}" for r in records)
        self.assertNotIn("ˆ", everything)
        self.assertIn("--escape \\^?[a-z] ( default = ^a )", everything)
        self.assertIn("(/dev/[^,]+/socket)", everything)   # table cells: | -> /


class PackItemsSplitTests(unittest.TestCase):
    """Oversized items split at line breaks; overlaps carry whole lines."""

    def listing(self, n=80):
        return "\n".join(f"    value_{i:03d} = compute({i}, scale=2)  # step {i}"
                         for i in range(n))

    def assert_whole_lines(self, text, piece):
        start = text.index(piece)
        end = start + len(piece)
        self.assertTrue(start == 0 or text[start - 1] == "\n", piece[:40])
        self.assertTrue(end == len(text) or text[end] == "\n", piece[-40:])

    def test_code_listing_splits_between_lines(self):
        code = self.listing()
        pieces = ingest.split_oversized(code)
        self.assertGreater(len(pieces), 2)
        for piece in pieces:
            self.assertLessEqual(len(piece), ingest.CHUNK_CHARS)
            self.assert_whole_lines(code, piece)
        # Every line survives intact somewhere, indentation included.
        for line in code.split("\n"):
            self.assertTrue(any(line in p.split("\n") for p in pieces), line)

    def test_code_pieces_still_overlap(self):
        pieces = ingest.split_oversized(self.listing())
        for a, b in zip(pieces, pieces[1:]):
            shared = b.split("\n")[0]
            self.assertIn(shared, a.split("\n"))

    def test_text_without_line_breaks_keeps_the_character_split(self):
        prose = "".join(f"word{i} " for i in range(600))
        step = ingest.CHUNK_CHARS - ingest.CHUNK_OVERLAP
        self.assertEqual(ingest.split_oversized(prose)[:2],
                         [prose[:ingest.CHUNK_CHARS],
                          prose[step:step + ingest.CHUNK_CHARS]])
        self.assertTrue("".join(p[:step] for p in ingest.split_oversized(prose))
                        .startswith(prose[:2 * step]))

    def test_one_very_long_line_falls_back_to_characters(self):
        text = "short\n" + "x" * 3000
        pieces = ingest.split_oversized(text)
        for piece in pieces:
            self.assertLessEqual(len(piece), ingest.CHUNK_CHARS)
        self.assertGreater(len(pieces[0]), ingest.CHUNK_CHARS // 2)
        self.assertTrue(pieces[-1].endswith("x" * 10))

    def test_pack_items_never_cuts_or_carries_part_of_a_code_line(self):
        code = self.listing()
        intro = "The loop below computes every value. " * 3
        chunks = ingest.pack_items([(intro, 1), (code, 1), ("short tail", 2)], "s")
        lines = set(code.split("\n"))
        for chunk in chunks:
            for part in chunk["text"].split("\n\n"):
                for line in part.split("\n"):
                    if "compute(" in line:
                        self.assertIn(line, lines)
        self.assertEqual(chunks[-1]["page"], 2)
        self.assertTrue(chunks[-1]["text"].endswith("short tail"))

    def test_overlap_tail_starts_at_a_line(self):
        code = self.listing(10)
        tail = ingest.overlap_tail(code)
        self.assertTrue(code.endswith(tail))
        self.assertEqual(code[len(code) - len(tail) - 1], "\n")
        prose = "no line breaks here at all " * 20
        self.assertEqual(ingest.overlap_tail(prose), prose[-ingest.CHUNK_OVERLAP:])


if __name__ == "__main__":
    unittest.main()
