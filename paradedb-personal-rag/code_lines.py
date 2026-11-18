"""Code items with their line breaks, read from the raw PDF's text layer.

Docling joins the lines of every text cluster with a space and strips each
line's indentation (`sanitize_text` in page_assemble_model.py, still in
2.140), so a Docling "code" item arrives as one long line that can't be
pasted into an interpreter. The raw PDF still has one text-layer line per
code line. For each code item this module reads the characters inside the
item's `prov` bbox, groups them into lines by baseline, and rebuilds each
line's indentation from its x position.

Nothing is guessed. When a code font is a Type0/Identity-H subset with no
ToUnicode map (the text layer reads as glyph IDs, e.g. `GSPN ULJOUFS` for
`from tkinter`), its glyphs are decoded only by IDENTICAL outline against a
reference font of the same family in CODE_REF_FONT_DIR. A glyph that matches
no reference character, or more than one, makes that whole item fall back to
Docling's text. So does a page with no text layer, a font with no reference,
or a text layer that doesn't look like the same listing Docling saw.

The raw PDF is found by the export's `origin.filename` under the raw store
(RAW_DIR, default: the `raw/` folder next to `approved/`) and is used only if
its sha256 matches the export's `origin.binary_hash` (Docling stores the low
64 bits of the file's sha256 there). The approved JSON is never modified.

See CODE_LINES_2026-10-16.md for the measurements behind this.
"""
from __future__ import annotations

import collections
import difflib
import hashlib
import io
import os
import pathlib
import re
import statistics

try:
    import pymupdf
except ImportError:            # ingest.py still works without it, just no lines
    pymupdf = None

# Below this, the text layer inside the bbox is not the listing Docling read
# (wrong region, or an undetected cipher font). Docling's own text for these
# books is OCR, so the bar is "same listing", not "same characters".
MIN_SIMILARITY = 0.6

PAGE_NUMBER = re.compile(r"\[?\s*(\d+|[ivxlcdm]+)\s*\]?", re.I)


def _sha_low64(path: pathlib.Path) -> int:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return int(h.hexdigest(), 16) & 0xFFFFFFFFFFFFFFFF


def raw_root_for(json_path: pathlib.Path) -> pathlib.Path | None:
    """RAW_DIR, else `<documents>/raw` for an export under `<documents>/approved/`."""
    env = os.environ.get("RAW_DIR", "").strip()
    if env:
        return pathlib.Path(env).expanduser()
    for parent in json_path.resolve().parents:
        if parent.name == "approved":
            return parent.parent / "raw"
    return None


def find_raw_pdf(json_path: pathlib.Path, doc: dict) -> pathlib.Path | None:
    """The raw PDF this export was converted from, verified by hash, or None.

    Collections don't line up between approved/ and raw/ (IN254's PDF is in
    raw/coding/), so every raw collection folder is searched by file name.
    """
    origin = doc.get("origin") or {}
    name, want = origin.get("filename"), origin.get("binary_hash")
    root = raw_root_for(json_path)
    if not name or want is None or root is None or not root.is_dir():
        return None
    name = pathlib.Path(name).name          # a bare file name, never a path
    for cand in [root / name, *sorted(root.glob(f"*/{glob_escape(name)}"))]:
        if cand.is_file() and _sha_low64(cand) == int(want):
            return cand
    return None


def glob_escape(name: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", name)


# --------------------------------------------------------------------------
# Exact glyph decode for Type0 fonts without ToUnicode
# --------------------------------------------------------------------------
def _outline_key(font, glyph_name):
    glyph = font["glyf"][glyph_name]
    if glyph.numberOfContours == 0:
        return ("empty", font["hmtx"][glyph_name][0])
    if glyph.isComposite():
        return None
    coords, ends, _ = glyph.getCoordinates(font["glyf"])
    return (tuple(coords), tuple(ends))


def _family_files(family: str):
    """Candidate reference file names for an embedded base font name."""
    yield family
    yield family.replace("-", "")


class _GlyphDecoder:
    def __init__(self, ref_dir: pathlib.Path | None):
        self.ref_dir = ref_dir
        self.refs = {}

    def _reference(self, family: str):
        if self.ref_dir is None:
            return None
        from fontTools.ttLib import TTFont
        for stem in _family_files(family):
            for ext in (".ttf", ".otf"):
                path = self.ref_dir / f"{stem}{ext}"
                if path in self.refs:
                    return self.refs[path]
                if path.is_file():
                    ref = TTFont(path)
                    if "glyf" not in ref:
                        self.refs[path] = None
                        return None
                    table = collections.defaultdict(set)
                    for cp, name in ref.getBestCmap().items():
                        if 0x20 <= cp < 0x7F:
                            key = _outline_key(ref, name)
                            if key is not None:
                                table[key].add(chr(cp))
                    self.refs[path] = table
                    return table
        return None

    def glyph_map(self, font_bytes: bytes, family: str) -> dict | None:
        """{chr(gid): char} for every glyph with exactly one outline match."""
        try:
            from fontTools.ttLib import TTFont
        except ImportError:
            return None
        table = self._reference(family)
        if table is None:
            return None
        try:
            emb = TTFont(io.BytesIO(font_bytes))
        except Exception:
            return None
        if "glyf" not in emb:
            return None
        out = {}
        for gid, name in enumerate(emb.getGlyphOrder()):
            hit = table.get(_outline_key(emb, name), ())
            if len(hit) == 1:
                out[chr(gid)] = next(iter(hit))
        return out


# --------------------------------------------------------------------------
# Reading one code item
# --------------------------------------------------------------------------
def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


class CodeLineReader:
    """Rebuild Docling code items from one raw PDF. Use as a context manager."""

    def __init__(self, pdf_path: pathlib.Path, ref_dir: pathlib.Path | None = None,
                 doc: dict | None = None):
        self.pdf = pymupdf.open(pdf_path)
        self.decoder = _GlyphDecoder(ref_dir)
        self.font_maps = {}         # page index -> {font name: map | None | "asis"}
        # Page headers/footers Docling found. A code bbox at the foot of a page
        # can take in the page number ("[ 120 ]"); those chars are not code.
        self.furniture = collections.defaultdict(list)
        for t in (doc or {}).get("texts") or []:
            if t.get("content_layer") == "furniture" or t.get("label") in (
                    "page_header", "page_footer"):
                for prov in t.get("prov") or []:
                    if prov.get("bbox") and prov.get("page_no"):
                        self.furniture[prov["page_no"]].append(prov["bbox"])

    def close(self):
        self.pdf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _fonts(self, pno: int) -> dict:
        """Per page: font name -> "asis" (readable), a glyph map, or None."""
        if pno not in self.font_maps:
            fonts, seen = {}, collections.Counter()
            for xref, _ext, ftype, base, *_ in self.pdf[pno].get_fonts(full=True):
                name = base.split("+")[-1]
                seen[name] += 1
                # A ToUnicode that is missing or null leaves glyph IDs as text.
                if ftype == "Type0" and self.pdf.xref_get_key(xref, "ToUnicode")[0] != "xref":
                    try:
                        data = self.pdf.extract_font(xref)[3]
                    except Exception:
                        data = b""
                    fonts[name] = self.decoder.glyph_map(data, name) if data else None
                else:
                    fonts[name] = "asis"
            for name, count in seen.items():
                if count > 1 and fonts[name] != "asis":
                    fonts[name] = None      # two subsets, one name: can't tell them apart
            self.font_maps[pno] = fonts
        return self.font_maps[pno]

    def _rect(self, page, bbox, pad=0.0):
        h = page.rect.height
        if bbox.get("coord_origin", "BOTTOMLEFT") == "BOTTOMLEFT":
            top, bottom = h - bbox["t"], h - bbox["b"]
        else:
            top, bottom = bbox["t"], bbox["b"]
        return pymupdf.Rect(bbox["l"] - pad, min(top, bottom) - pad,
                            bbox["r"] + pad, max(top, bottom) + pad)

    def _chars(self, page, rect, fonts, skip=()):
        """Decoded (x0, x1, baseline, size, font, char) inside rect, or None."""
        out = []
        # Unmapped glyphs come back as their glyph ID, not U+FFFD, so the
        # outline map can decode them; ligatures are expanded (code has none).
        flags = (pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
                 | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE)
        raw = page.get_text("rawdict", clip=rect, flags=flags)
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                if line.get("dir", (1, 0)) != (1, 0):
                    return None             # rotated text: not a listing
                for span in line["spans"]:
                    fmap = fonts.get(span["font"], "asis")
                    if fmap is None:
                        return None
                    for ch in span["chars"]:
                        x0, y0, x1, y1 = ch["bbox"]
                        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                        if not (rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1):
                            continue
                        if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in skip):
                            continue
                        c = ch["c"]
                        if fmap != "asis":
                            if c not in fmap:
                                return None  # an undecodable glyph: don't guess
                            c = fmap[c]
                        out.append((x0, x1, ch["origin"][1], span["size"],
                                    span["font"], c))
        return out

    @staticmethod
    def _lines(chars):
        """Group chars into baselines; indentation from x, in character cells.

        The cell is the code font's advance: in a monospaced listing every
        glyph has the same width, so it is the most common glyph width even
        when prose is mixed in. Indentation is measured only on monospaced
        rows. A prose row Docling merged into the listing keeps its words but
        gets no indentation, and a bare page number ("[ 120 ]") at the top or
        bottom of the box is dropped.
        """
        ink = [t for t in chars if not t[5].isspace() and t[1] > t[0]]
        if not ink:
            return []
        cell = collections.Counter(round(x1 - x0, 1) for x0, x1, *_ in ink).most_common(1)[0][0]
        rows = []
        for x0, x1, base, size, _font, c in sorted(chars, key=lambda t: (t[2], t[0])):
            if rows and abs(base - rows[-1][0]) < 0.5 * size:
                rows[-1][1].append((x0, x1, c))
            else:
                rows.append([base, [(x0, x1, c)]])
        rows = [sorted(cs) for _, cs in rows]
        rows = [cs for cs in rows if any(not c.isspace() for *_, c in cs)]

        def mono(cs):
            ws = [x1 - x0 for x0, x1, c in cs if not c.isspace()]
            return sum(abs(w - cell) <= 0.15 * cell for w in ws) >= 0.9 * len(ws)

        def page_number(cs):
            return not mono(cs) and PAGE_NUMBER.fullmatch("".join(c for *_, c in cs).strip())

        while rows and page_number(rows[-1]):
            rows.pop()
        while rows and page_number(rows[0]):
            rows.pop(0)
        code_rows = [cs for cs in rows if mono(cs)]
        if not code_rows:
            return []
        left = min(next(x0 for x0, _, c in cs if not c.isspace()) for cs in code_rows)
        lines = []
        for cs in rows:
            first = next(i for i, (*_, c) in enumerate(cs) if not c.isspace())
            indent = round((cs[first][0] - left) / cell) if mono(cs) else 0
            text, prev = " " * max(0, indent), None
            for x0, x1, c in cs[first:]:
                # Some PDFs position words instead of drawing a space glyph.
                if prev is not None and x0 - prev > 0.5 * cell and not text.endswith(" "):
                    text += " " * max(1, round((x0 - prev) / cell))
                text += c
                prev = x1
            lines.append(text.rstrip())
        return lines

    def code_text(self, item: dict) -> str | None:
        """The item's text with one line per PDF line, or None to keep Docling's."""
        lines = []
        for prov in item.get("prov") or []:
            bbox, page_no = prov.get("bbox"), prov.get("page_no")
            if not bbox or not page_no or not 1 <= page_no <= len(self.pdf):
                return None
            page = self.pdf[page_no - 1]
            skip = [self._rect(page, b) for b in self.furniture.get(page_no, ())]
            chars = self._chars(page, self._rect(page, bbox, pad=1.0),
                                self._fonts(page_no - 1), skip)
            if not chars:
                return None
            lines.extend(self._lines(chars))
        text = "\n".join(lines)
        if "�" in text or not text.strip():
            return None
        docling = _squash(item.get("text") or "")
        if docling:
            ratio = difflib.SequenceMatcher(None, _squash(text), docling,
                                            autojunk=False).ratio()
            if ratio < MIN_SIMILARITY:
                return None
        return text


def reader_for(json_path: pathlib.Path, doc: dict) -> CodeLineReader | None:
    """A reader for this export's raw PDF, or None (no PDF, no PyMuPDF, no code)."""
    if pymupdf is None or not any((t.get("label") or "").lower() == "code"
                                  for t in doc.get("texts") or []):
        return None
    pdf = find_raw_pdf(json_path, doc)
    if pdf is None:
        return None
    env = os.environ.get("CODE_REF_FONT_DIR", "").strip()
    ref_dir = pathlib.Path(env).expanduser() if env else None
    if ref_dir is None:
        default = raw_root_for(json_path)
        ref_dir = default.parent / "reference-fonts" if default else None
    return CodeLineReader(pdf, ref_dir if ref_dir and ref_dir.is_dir() else None, doc)
