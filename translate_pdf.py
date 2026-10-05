"""Infinity Translate - rendering core.

Translates a text-based PDF from English to Telugu or Hindi by editing the PDF in
place: original text is removed with redactions (images and vector graphics are
kept) and each translated paragraph is written back into its original box.

Usage:
    python translate_pdf.py input.pdf output.pdf --target te|hi [--max-pages N]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import statistics
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import pymupdf

from providers import TranslationProvider, get_provider, nfc

FONT_DIR = Path(__file__).resolve().parent / "fonts"
TARGET_FONTS = {
    "te": ("NotoSansTelugu-Regular.ttf", "NotoSansTelugu-Bold.ttf"),
    "hi": ("NotoSansDevanagari-Regular.ttf", "NotoSansDevanagari-Bold.ttf"),
}
LATIN_FONTS = ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf")
SUPPORTED_TARGETS = tuple(TARGET_FONTS)

MIN_SCALE = 0.6
BATCH_SEGMENTS = 100
BATCH_CHARS = 20_000

URL_RE = re.compile(r"^(https?://|www\.)\S+$", re.I)
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
BULLET_RE = re.compile(r"^([\u2022\u00b7\u25aa\u25a0\u25cf\u25cb\u25e6\u2023\u2043\u27a2\u2713\u2013\u2014*-]|"
                       r"\(?\d{1,2}[.)]|\(?[a-z][.)]|\(?[ivx]{1,4}[.)])\s+")


class PdfRejected(Exception):
    """The PDF cannot be processed (encrypted, scanned, no text...)."""


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Line:
    bbox: pymupdf.Rect
    text: str
    spans: list[dict]          # {"bbox", "size", "bold", "italic", "color", "chars"}

    @property
    def size(self) -> float:
        return _weighted_mode([(round(s["size"], 1), s["chars"]) for s in self.spans])

    @property
    def bold(self) -> bool:
        return _weighted_mode([(s["bold"], s["chars"]) for s in self.spans])

    @property
    def color(self) -> int:
        return _weighted_mode([(s["color"], s["chars"]) for s in self.spans])


@dataclass
class Block:
    page: int
    lines: list[Line] = field(default_factory=list)
    translation: str = ""
    skip: bool = False

    @property
    def bbox(self) -> pymupdf.Rect:
        r = pymupdf.Rect(self.lines[0].bbox)
        for ln in self.lines[1:]:
            r |= ln.bbox
        return r

    @property
    def prefix(self) -> str:
        """A list bullet / number at the start of the block, kept out of translation."""
        m = BULLET_RE.match(self.lines[0].text.strip())
        return m.group(1) if m else ""

    @property
    def text(self) -> str:
        """Text to translate (without the bullet prefix)."""
        full = self.full_text
        prefix = self.prefix
        return full[len(prefix):].strip() if prefix else full

    @property
    def full_text(self) -> str:
        out = ""
        for ln in self.lines:
            t = ln.text.strip()
            if not out:
                out = t
            elif out.endswith("-") and len(out) > 1 and out[-2].isalpha() and t[:1].islower():
                out = out[:-1] + t          # de-hyphenate a word split across lines
            else:
                out += " " + t
        return re.sub(r"\s+", " ", out).strip()

    def style(self) -> dict:
        spans = [s for ln in self.lines for s in ln.spans]
        w = [s["chars"] for s in spans]
        return {
            "size": _weighted_mode([(round(s["size"], 1), c) for s, c in zip(spans, w)]),
            "bold": _weighted_mode([(s["bold"], c) for s, c in zip(spans, w)]),
            "italic": _weighted_mode([(s["italic"], c) for s, c in zip(spans, w)]),
            "color": _weighted_mode([(s["color"], c) for s, c in zip(spans, w)]),
        }


@dataclass
class Report:
    pages_processed: int = 0
    blocks_translated: int = 0
    blocks_skipped: int = 0
    overflow_blocks: dict[int, int] = field(default_factory=dict)   # 1-based page -> count
    overflow_details: list[dict] = field(default_factory=list)
    characters_sent: int = 0
    seconds: float = 0.0

    @property
    def overflow_total(self) -> int:
        return sum(self.overflow_blocks.values())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["overflow_total"] = self.overflow_total
        return d


def _weighted_mode(pairs):
    acc: dict = {}
    for value, weight in pairs:
        acc[value] = acc.get(value, 0) + max(weight, 1)
    return max(acc.items(), key=lambda kv: kv[1])[0]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def page_is_scanned(page: pymupdf.Page) -> bool:
    """A page with image(s) covering much of it but (almost) no extractable text."""
    text_chars = len(re.sub(r"\s", "", page.get_text("text")))
    if text_chars >= 40:
        return False
    page_area = abs(page.rect) or 1
    covered = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        covered += abs(r)
    return covered / page_area >= 0.3


def check_document(doc: pymupdf.Document, max_pages: int | None = None) -> None:
    """Raise PdfRejected if the document cannot be translated."""
    if doc.needs_pass or doc.is_encrypted:
        raise PdfRejected("The PDF is encrypted or password protected. Remove the protection and try again.")
    if not doc.is_pdf:
        raise PdfRejected("The file is not a PDF.")
    if doc.page_count == 0:
        raise PdfRejected("The PDF has no pages.")
    n = min(doc.page_count, max_pages) if max_pages else doc.page_count
    pages = [doc[i] for i in range(n)]
    scanned = sum(1 for p in pages if page_is_scanned(p))
    total_text = sum(len(re.sub(r"\s", "", p.get_text("text"))) for p in pages)
    if scanned and scanned >= max(1, n / 2):
        raise PdfRejected(
            "This looks like a scanned PDF (pages are images with almost no extractable text). "
            "Only text-based PDFs are supported."
        )
    if total_text < 10:
        raise PdfRejected("The PDF contains no extractable text to translate.")


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def _span_style(span: dict) -> dict:
    font = span.get("font", "")
    flags = span.get("flags", 0)
    return {
        "bbox": pymupdf.Rect(span["bbox"]),
        "size": float(span["size"]),
        "bold": bool(flags & 16) or bool(re.search(r"bold|black|heavy|semibold", font, re.I)),
        "italic": bool(flags & 2) or bool(re.search(r"italic|oblique", font, re.I)),
        "color": int(span.get("color", 0)),
        "chars": len(span["text"].strip()),
        "text": span["text"],
    }


def _extract_lines(page: pymupdf.Page) -> list[Line]:
    flags = pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
    data = page.get_text("dict", flags=flags)
    lines: list[Line] = []
    for block in data["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            dx, dy = line.get("dir", (1, 0))
            if abs(dy) > 0.01 or dx < 0:          # rotated / vertical text is left untouched
                continue
            spans = [_span_style(s) for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            # Split a line where spans are far apart (e.g. table cells on one baseline).
            group = [spans[0]]
            for prev, cur in zip(spans, spans[1:]):
                gap = cur["bbox"].x0 - prev["bbox"].x1
                if gap > max(prev["size"], cur["size"]) * 1.5:
                    lines.append(_make_line(group))
                    group = []
                group.append(cur)
            lines.append(_make_line(group))
    return lines


def _make_line(spans: list[dict]) -> Line:
    bbox = pymupdf.Rect(spans[0]["bbox"])
    for s in spans[1:]:
        bbox |= s["bbox"]
    text = "".join(s["text"] for s in spans)
    return Line(bbox=bbox, text=text, spans=spans)


@dataclass
class Rules:
    """Ruling lines / rectangle edges from vector graphics. Used to avoid merging
    text across table borders and to stop box extension at them."""
    horizontal: list[tuple[float, float, float]] = field(default_factory=list)   # (x0, x1, y)
    vertical: list[tuple[float, float, float]] = field(default_factory=list)     # (y0, y1, x)

    def add_rect(self, r: pymupdf.Rect) -> None:
        if r.width > 3:
            self.horizontal += [(r.x0, r.x1, r.y0), (r.x0, r.x1, r.y1)]
        if r.height > 3:
            self.vertical += [(r.y0, r.y1, r.x0), (r.y0, r.y1, r.x1)]

    def h_between(self, x0, x1, y_top, y_bottom) -> bool:
        return any(y_top - 0.5 <= sy <= y_bottom + 0.5 and min(x1, sx1) - max(x0, sx0) > 2
                   for sx0, sx1, sy in self.horizontal)

    def v_between(self, y0, y1, x_left, x_right) -> bool:
        return any(x_left - 0.5 <= sx <= x_right + 0.5 and min(y1, sy1) - max(y0, sy0) > 2
                   for sy0, sy1, sx in self.vertical)


def find_rules(page: pymupdf.Page) -> Rules:
    rules = Rules()
    try:
        drawings = page.get_drawings()
    except Exception:
        return rules
    for d in drawings:
        for item in d.get("items", []):
            kind = item[0]
            if kind == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 1.0 and abs(p1.x - p2.x) > 3:
                    rules.horizontal.append((min(p1.x, p2.x), max(p1.x, p2.x), (p1.y + p2.y) / 2))
                elif abs(p1.x - p2.x) < 1.0 and abs(p1.y - p2.y) > 3:
                    rules.vertical.append((min(p1.y, p2.y), max(p1.y, p2.y), (p1.x + p2.x) / 2))
            elif kind == "re":
                rules.add_rect(pymupdf.Rect(item[1]))
            elif kind == "qu":
                rules.add_rect(item[1].rect)
    return rules


def _merge_same_baseline(lines: list[Line], rules: Rules) -> list[Line]:
    """MuPDF splits lines with wide word gaps (justified text) into several lines.
    Re-join pieces that share a baseline and are close, unless a table rule separates them."""
    order = sorted(range(len(lines)), key=lambda i: lines[i].bbox.x0)
    merged: list[tuple[int, Line]] = []
    for i in order:
        ln = lines[i]
        for k, (idx, m) in enumerate(merged):
            size = max(m.size, ln.size)
            if (abs(m.bbox.y1 - ln.bbox.y1) <= 0.2 * size and abs(m.bbox.y0 - ln.bbox.y0) <= 0.3 * size
                    and abs(m.size - ln.size) <= 0.12 * size
                    and -1 <= ln.bbox.x0 - m.bbox.x1 <= 1.5 * size
                    and not rules.v_between(m.bbox.y0, m.bbox.y1, m.bbox.x1, ln.bbox.x0)):
                sep = "" if m.text.endswith(" ") or ln.text.startswith(" ") else " "
                merged[k] = (idx, Line(bbox=m.bbox | ln.bbox, text=m.text + sep + ln.text,
                                       spans=m.spans + ln.spans))
                break
        else:
            merged.append((i, ln))
    merged.sort(key=lambda t: t[0])
    return [ln for _, ln in merged]


def _can_join(block: Block, line: Line, rules: Rules) -> bool:
    last = block.lines[-1]
    size = max(last.size, line.size)
    if abs(last.size - line.size) > 0.12 * size:
        return False
    if last.bold != line.bold or last.color != line.color:
        return False
    gap = line.bbox.y0 - last.bbox.y1
    if gap < -0.35 * size or gap > 0.45 * size:
        return False
    if BULLET_RE.match(line.text.strip()):
        return False
    b = block.bbox
    overlap = min(b.x1, line.bbox.x1) - max(b.x0, line.bbox.x0)
    if overlap <= 0:
        return False
    left_aligned = abs(line.bbox.x0 - b.x0) <= size * 1.2
    if len(block.lines) == 1 and line.bbox.x0 < b.x0 and b.x0 - line.bbox.x0 <= size * 3:
        left_aligned = True                     # first-line indent
    if len(block.lines) >= 2 and abs(line.bbox.x0 - block.lines[-1].bbox.x0) <= 1.0:
        left_aligned = True
    centered = abs((line.bbox.x0 + line.bbox.x1) / 2 - (b.x0 + b.x1) / 2) <= max(2.0, size * 0.3)
    if not (left_aligned or centered):
        return False
    # In a left-aligned paragraph, if the first word of this line would have fit at
    # the end of the previous line, the previous line ended its paragraph.
    if len(block.lines) >= 2 and not centered:
        first_word = line.text.strip().split(" ")[0]
        char_w = line.bbox.width / max(len(line.text.strip()), 1)
        if last.bbox.x1 + (len(first_word) + 1) * char_w + size * 1.5 < b.x1:
            return False
    if rules.h_between(max(b.x0, line.bbox.x0), min(b.x1, line.bbox.x1), last.bbox.y1, line.bbox.y0):
        return False
    return True


def extract_blocks(page: pymupdf.Page, page_no: int) -> tuple[list[Block], Rules]:
    rules = find_rules(page)
    lines = _merge_same_baseline(_extract_lines(page), rules)
    blocks: list[Block] = []
    open_blocks: list[Block] = []
    for line in lines:
        target = None
        # Prefer the most recently touched paragraph whose last line sits right above.
        for b in reversed(open_blocks):
            if _can_join(b, line, rules):
                target = b
                break
        if target is None:
            target = Block(page=page_no)
            blocks.append(target)
        target.lines.append(line)
        open_blocks = [b for b in open_blocks if b is not target][-12:] + [target]
    for b in blocks:
        b.skip = should_skip(b.full_text)
    return blocks, rules


def should_skip(text: str) -> bool:
    t = text.strip()
    if sum(1 for c in t if c.isalpha()) < 2:
        return True                        # only numbers / symbols
    if URL_RE.match(t) or EMAIL_RE.match(t):
        return True
    words = t.split()
    if len(words) == 1 and any(c.isdigit() for c in t):
        return True                        # codes / ids such as "#e76f51", "A4-2024"
    if words and all(URL_RE.match(w) or EMAIL_RE.match(w) or not any(c.isalpha() for c in w) for w in words):
        return True
    return False


def guess_alignment(block: Block, page_rect: pymupdf.Rect, content: pymupdf.Rect) -> str:
    lines = block.lines
    size = block.style()["size"]
    tol = max(1.5, size * 0.25)
    b = block.bbox
    if len(lines) >= 2:
        lefts = [ln.bbox.x0 for ln in lines]
        rights = [ln.bbox.x1 for ln in lines]
        centers = [(ln.bbox.x0 + ln.bbox.x1) / 2 for ln in lines]
        left_ok = max(lefts[1:]) - min(lefts[1:]) <= tol if len(lefts) > 2 else abs(lefts[0] - lefts[1]) <= tol
        right_ok = max(rights[:-1]) - min(rights[:-1]) <= tol and len(lines) > 2
        if left_ok and right_ok:
            return "justify"
        if left_ok:
            return "left"
        if max(centers) - min(centers) <= tol:
            return "center"
        if max(rights) - min(rights) <= tol:
            return "right"
        return "left"
    # single line: compare with the page's text area
    cx = (b.x0 + b.x1) / 2
    page_cx = (content.x0 + content.x1) / 2
    if abs(cx - page_cx) <= max(3.0, content.width * 0.02) and b.x0 - content.x0 > size * 2:
        return "center"
    if abs(b.x1 - content.x1) <= tol and b.x0 > page_cx:
        return "right"
    return "left"


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #
def translate_texts(texts: list[str], provider: TranslationProvider, target: str,
                    source: str = "en") -> tuple[dict[str, str], int]:
    """Translate unique texts in batches. Returns (mapping, characters sent)."""
    unique = list(dict.fromkeys(texts))
    mapping: dict[str, str] = {}
    sent = 0
    batch: list[str] = []
    size = 0

    def flush():
        nonlocal sent
        if not batch:
            return
        result = provider.translate_batch(list(batch), source, target)
        if len(result) != len(batch):
            raise RuntimeError("translation provider returned wrong number of results")
        for src, dst in zip(batch, result):
            mapping[src] = nfc(dst).strip()
        sent += sum(len(t) for t in batch)
        batch.clear()

    for t in unique:
        if batch and (len(batch) >= BATCH_SEGMENTS or size + len(t) > BATCH_CHARS):
            flush()
            size = 0
        batch.append(t)
        size += len(t)
    flush()
    return mapping, sent


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
_FONT_CACHE: dict[str, pymupdf.Font] = {}


def _target_font(target: str) -> pymupdf.Font:
    if target not in _FONT_CACHE:
        _FONT_CACHE[target] = pymupdf.Font(fontfile=str(FONT_DIR / TARGET_FONTS[target][0]))
    return _FONT_CACHE[target]


def _font_css(target: str) -> str:
    reg, bold = TARGET_FONTS[target]
    lreg, lbold = LATIN_FONTS
    return (
        f"@font-face {{font-family: tgt; src: url({reg});}}\n"
        f"@font-face {{font-family: tgt; src: url({bold}); font-weight: bold;}}\n"
        f"@font-face {{font-family: lat; src: url({lreg});}}\n"
        f"@font-face {{font-family: lat; src: url({lbold}); font-weight: bold;}}\n"
        "body, p, div, span {margin: 0; padding: 0;}\n"
        "body {font-family: tgt;}\n"
        ".l {font-family: lat;}\n"
    )


def _to_html(text: str, target: str) -> str:
    """Escape text and wrap runs the Indic font cannot draw (Latin letters, some
    symbols) in a span that uses the bundled Latin Noto font."""
    font = _target_font(target)
    out: list[tuple[str, bool]] = []
    run, run_latin = "", None
    for ch in text:
        if ch.isspace():
            run += ch
            continue
        latin = not font.has_glyph(ord(ch))
        if run_latin is None or latin == run_latin:
            run += ch
            run_latin = latin
        else:
            out.append((run, run_latin))
            run, run_latin = ch, latin
    if run:
        out.append((run, bool(run_latin)))
    return "".join(
        f'<span class="l">{html.escape(t)}</span>' if lat else html.escape(t) for t, lat in out
    )


def _block_html(block: Block, align: str, target: str) -> str:
    st = block.style()
    size = st["size"]
    if len(block.lines) >= 2:
        tops = [ln.bbox.y0 for ln in block.lines]
        pitch = statistics.median([b - a for a, b in zip(tops, tops[1:])])
        line_height = min(max(pitch / size, 1.05), 1.8)
    else:
        line_height = 1.2
    color = f"#{st['color']:06x}"
    styles = [
        f"font-size: {size:.2f}px",
        f"line-height: {line_height:.3f}",
        f"color: {color}",
        f"text-align: {align}",
        f"font-weight: {'bold' if st['bold'] else 'normal'}",
    ]
    if st["italic"]:
        styles.append("font-style: italic")
    text = f"{block.prefix} {block.translation}" if block.prefix else block.translation
    return f'<p style="{"; ".join(styles)}">{_to_html(text, target)}</p>'


def _free_space_below(rect: pymupdf.Rect, obstacles: list[pymupdf.Rect], rules: Rules, page_rect: pymupdf.Rect) -> float:
    """Lowest y the box can extend to without touching another block, image or rule."""
    limit = page_rect.y1 - 6
    for o in obstacles:
        if o.y0 >= rect.y1 - 0.5 and min(o.x1, rect.x1) - max(o.x0, rect.x0) > 0.5:
            limit = min(limit, o.y0 - 1)
    for sx0, sx1, sy in rules.horizontal:
        if sy >= rect.y1 + 0.5 and min(sx1, rect.x1) - max(sx0, rect.x0) > 0.5:
            limit = min(limit, sy - 1)
    return limit


def _widen_single_line(rect: pymupdf.Rect, align: str, obstacles: list[pymupdf.Rect],
                       rules: Rules, content: pymupdf.Rect) -> pymupdf.Rect:
    """A single-line block's box is exactly as wide as the English text. Let it grow
    sideways into empty space (never past neighbours, images, table rules or the
    page's text area) so a longer translation is not shrunk needlessly."""
    left_lim, right_lim = content.x0, content.x1
    for o in obstacles:
        if min(o.y1, rect.y1) - max(o.y0, rect.y0) <= 0.5:
            continue
        if o.x0 >= rect.x1 - 0.5:
            right_lim = min(right_lim, o.x0 - 2)
        elif o.x1 <= rect.x0 + 0.5:
            left_lim = max(left_lim, o.x1 + 2)
    for sy0, sy1, sx in rules.vertical:
        if min(sy1, rect.y1) - max(sy0, rect.y0) <= 0.5:
            continue
        if sx >= rect.x1 - 0.5:
            right_lim = min(right_lim, sx - 2)
        elif sx <= rect.x0 + 0.5:
            left_lim = max(left_lim, sx + 2)
    left_lim, right_lim = min(left_lim, rect.x0), max(right_lim, rect.x1)
    if align == "center":
        grow = min(rect.x0 - left_lim, right_lim - rect.x1)
        return pymupdf.Rect(rect.x0 - grow, rect.y0, rect.x1 + grow, rect.y1)
    if align == "right":
        return pymupdf.Rect(left_lim, rect.y0, rect.x1, rect.y1)
    return pymupdf.Rect(rect.x0, rect.y0, right_lim, rect.y1)


def _redaction_rect(span_bbox: pymupdf.Rect) -> pymupdf.Rect:
    """Shrink a span box so neighbouring lines/cells are not caught by the redaction."""
    r = pymupdf.Rect(span_bbox)
    h = r.height
    return pymupdf.Rect(r.x0 + 0.3, r.y0 + h * 0.3, r.x1 - 0.3, r.y1 - h * 0.3)


def render_page(page: pymupdf.Page, blocks: list[Block], rules: Rules, target: str,
                archive: pymupdf.Archive, css: str, report: Report) -> None:
    todo = [b for b in blocks if not b.skip and b.translation]
    if not todo:
        return
    all_lines = [ln.bbox for b in blocks for ln in b.lines]
    content = pymupdf.Rect(all_lines[0])
    for r in all_lines[1:]:
        content |= r
    aligns = {id(b): guess_alignment(b, page.rect, content) for b in todo}

    for b in todo:
        for ln in b.lines:
            for s in ln.spans:
                page.add_redact_annot(_redaction_rect(s["bbox"]), fill=False, cross_out=False)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )

    image_rects = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
    block_rects = [b.bbox for b in blocks]

    for b in todo:
        rect = pymupdf.Rect(b.bbox)
        rect.x1 += 1.0                      # tiny tolerance for rounding in layout
        others = [r for r in block_rects if r != b.bbox] + image_rects
        if len(b.lines) == 1:
            rect = _widen_single_line(rect, aligns[id(b)], others, rules, content)
        html_text = _block_html(b, aligns[id(b)], target)
        spare, _ = page.insert_htmlbox(rect, html_text, css=css, archive=archive, scale_low=MIN_SCALE)
        if spare >= 0:
            report.blocks_translated += 1
            continue
        # Extend downward into free space only.
        limit = _free_space_below(rect, others, rules, page.rect)
        if limit > rect.y1 + 1:
            bigger = pymupdf.Rect(rect.x0, rect.y0, rect.x1, limit)
            spare, _ = page.insert_htmlbox(bigger, html_text, css=css, archive=archive, scale_low=MIN_SCALE)
            if spare >= 0:
                report.blocks_translated += 1
                continue
            rect = bigger
        # Still does not fit: write it anyway (smaller than the minimum scale) and flag it.
        page.insert_htmlbox(rect, html_text, css=css, archive=archive, scale_low=0)
        report.blocks_translated += 1
        pno = b.page + 1
        report.overflow_blocks[pno] = report.overflow_blocks.get(pno, 0) + 1
        report.overflow_details.append({"page": pno, "bbox": [round(v, 1) for v in b.bbox],
                                        "text": b.text[:80]})


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def translate_document(input_path: str | os.PathLike, output_path: str | os.PathLike, target: str,
                       max_pages: int | None = None, provider: TranslationProvider | None = None,
                       on_stage: Callable[[str], None] | None = None) -> Report:
    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"target must be one of {SUPPORTED_TARGETS}")
    stage = on_stage or (lambda s: None)
    provider = provider or get_provider()
    t0 = time.monotonic()
    report = Report()

    stage("extracting")
    try:
        doc = pymupdf.open(input_path)
    except Exception as e:
        raise PdfRejected(f"Could not open the PDF: {e}") from e
    try:
        check_document(doc, max_pages)
        if max_pages and doc.page_count > max_pages:
            doc.select(list(range(max_pages)))
        pages_blocks = []
        for page in doc:
            if page.rotation:
                page.remove_rotation()
            pages_blocks.append(extract_blocks(page, page.number))

        stage("translating")
        texts = [b.text for blocks, _ in pages_blocks for b in blocks if not b.skip]
        mapping, sent = translate_texts(texts, provider, target)
        report.characters_sent = sent
        for blocks, _ in pages_blocks:
            for b in blocks:
                if b.skip:
                    report.blocks_skipped += 1
                else:
                    b.translation = mapping.get(b.text, "")

        stage("rebuilding")
        archive = pymupdf.Archive(str(FONT_DIR))
        css = _font_css(target)
        for page, (blocks, rules) in zip(doc, pages_blocks):
            render_page(page, blocks, rules, target, archive, css, report)
            report.pages_processed += 1

        doc.subset_fonts()
        doc.save(str(output_path), garbage=4, deflate=True, clean=True)
    finally:
        doc.close()
    report.seconds = round(time.monotonic() - t0, 2)
    return report


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="Translate a text-based PDF from English to Telugu or Hindi.")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--target", required=True, choices=SUPPORTED_TARGETS)
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args(argv)
    try:
        report = translate_document(args.input, args.output, args.target, args.max_pages,
                                    on_stage=lambda s: print(f"[{s}]", file=sys.stderr))
    except PdfRejected as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
