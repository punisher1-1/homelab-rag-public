"""Headings are not read out of fenced code blocks. No database or embedder."""
import os
import pathlib
import sys
import unittest

# ingest.py reads its connection settings at import time.
for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
            "OLLAMA_URL", "EMBED_MODEL"):
    os.environ.setdefault(key, "unused")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ingest  # noqa: E402

DOC = """Intro text.

## Install

Run this:

```bash
# Add Docker's official GPG key
sudo apt-get update
```

## Verify

```dockerfile
# syntax=docker/dockerfile:1
FROM alpine
```
"""


class FenceMaskTests(unittest.TestCase):
    def test_marks_fence_lines_and_contents(self):
        lines = ["a", "```sh", "# x", "```", "b"]
        self.assertEqual(ingest.fence_mask(lines), [False, True, True, True, False])

    def test_tilde_fence_and_indented_fence(self):
        lines = ["~~~", "# x", "~~~", "  ```", "# y", "  ```", "# z"]
        self.assertEqual(ingest.fence_mask(lines),
                         [True, True, True, True, True, True, False])

    def test_shorter_or_different_marker_does_not_close(self):
        lines = ["````", "```", "~~~~", "# x", "````", "# y"]
        self.assertEqual(ingest.fence_mask(lines),
                         [True, True, True, True, True, False])

    def test_unclosed_fence_runs_to_end(self):
        self.assertEqual(ingest.fence_mask(["```", "# x", "# y"]), [True, True, True])


class HeadingSplitTests(unittest.TestCase):
    def test_code_comments_are_not_sections(self):
        records = ingest.chunk_plain_with_sections("Doc", DOC)
        self.assertEqual({r["section"] for r in records}, {"", "Install", "Verify"})

    def test_code_comments_stay_in_the_chunk_text(self):
        text = "\n".join(r["text"] for r in ingest.chunk_plain_with_sections("Doc", DOC))
        self.assertIn("# Add Docker's official GPG key", text)
        self.assertIn("# syntax=docker/dockerfile:1", text)

    def test_real_headings_still_split(self):
        segments, last = ingest.split_by_heading("# A\none\n## B\ntwo", "")
        self.assertEqual(segments, [("A", "one"), ("B", "two")])
        self.assertEqual(last, "B")

    def test_carried_heading_survives_a_code_only_page(self):
        segments, last = ingest.split_by_heading("```\n# not a heading\n```", "Prev")
        self.assertEqual([h for h, _ in segments], ["Prev"])
        self.assertEqual(last, "Prev")

    def test_table_context_ignores_code_comments(self):
        md = "## Ports\n\n```\n# noise\n```\n\n| Name | Port |\n|---|---|\n| web | 80 |\n"
        chunks = ingest.chunk_markdown("Doc", md)
        self.assertIn("[Doc — Ports] Name: web; Port: 80", chunks)


if __name__ == "__main__":
    unittest.main()
