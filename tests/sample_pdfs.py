"""Generate sample PDFs for manual checks and tests.

    python tests/sample_pdfs.py out_dir
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pymupdf

LOREM = (
    "Machine translation helps people read documents written in other languages. "
    "This paragraph is long enough to wrap across several lines so that the "
    "translator has to merge the lines back into a single paragraph before sending "
    "it to the translation service, and then fit the result into the same box."
)


def _image_pixmap(w=240, h=150) -> pymupdf.Pixmap:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    samples = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 3
            samples[i] = int(255 * x / w)
            samples[i + 1] = int(255 * y / h)
            samples[i + 2] = 160
    return pymupdf.Pixmap(pymupdf.csRGB, w, h, bytes(samples), False)


def _write_lines(page, x, y, text, width_chars, size=10, font="helv", leading=1.25, color=(0, 0, 0)):
    """Write a paragraph line by line (each line is its own text object)."""
    for line in textwrap.wrap(text, width_chars):
        page.insert_text((x, y), line, fontsize=size, fontname=font, color=color)
        y += size * leading
    return y


def make_sample(path: str | Path) -> Path:
    doc = pymupdf.open()
    W, H = pymupdf.paper_size("a4")

    # ---------------- page 1: headings, paragraphs, image -----------------
    p = doc.new_page(width=W, height=H)
    p.insert_htmlbox(pymupdf.Rect(50, 50, W - 50, 85),
                     "<h1 style='text-align:center;font-size:22px'>Annual Report 2024</h1>")
    p.insert_htmlbox(pymupdf.Rect(50, 88, W - 50, 108),
                     "<p style='text-align:center;font-size:12px;color:#555555'>"
                     "A short summary of our work this year</p>")
    p.insert_htmlbox(pymupdf.Rect(50, 125, W - 50, 145),
                     "<h2 style='font-size:15px;color:#1a4d8f'>1. Introduction</h2>")
    p.insert_htmlbox(pymupdf.Rect(50, 150, W - 50, 260),
                     f"<p style='text-align:justify;font-size:11px'>{LOREM} {LOREM}</p>")
    p.insert_image(pymupdf.Rect(50, 280, 290, 430), pixmap=_image_pixmap())
    p.insert_htmlbox(pymupdf.Rect(50, 434, 290, 450),
                     "<p style='font-size:9px;font-style:italic'>Figure 1: Colour gradient sample.</p>")
    y = _write_lines(p, 305, 292, "The image on the left must stay exactly where it is. "
                     "Only the text around it should change language, and this text is "
                     "written one line at a time to test paragraph merging.", 42, size=10)
    p.insert_htmlbox(pymupdf.Rect(50, 470, W - 50, 490),
                     "<h2 style='font-size:15px;color:#1a4d8f'>2. Goals</h2>")
    p.insert_htmlbox(pymupdf.Rect(50, 495, W - 50, 600),
                     f"<p style='font-size:11px'>{LOREM}</p>")
    p.insert_text((W / 2 - 3, H - 30), "1", fontsize=9)
    p.insert_text((50, H - 30), "https://example.com/report", fontsize=8, color=(0, 0, 0.8))
    p.insert_text((W - 160, H - 30), "info@example.com", fontsize=8)

    # ---------------- page 2: two columns, table, chart -------------------
    p = doc.new_page(width=W, height=H)
    p.insert_htmlbox(pymupdf.Rect(50, 50, W - 50, 75),
                     "<h2 style='font-size:16px'>Two Column Section</h2>")
    colw = (W - 100 - 20) / 2
    p.insert_htmlbox(pymupdf.Rect(50, 80, 50 + colw, 260),
                     f"<p style='font-size:10px;text-align:justify'>Left column. {LOREM}</p>")
    p.insert_htmlbox(pymupdf.Rect(70 + colw, 80, W - 50, 260),
                     f"<p style='font-size:10px;text-align:justify'>Right column. {LOREM}</p>")
    # table with borders
    p.insert_htmlbox(pymupdf.Rect(50, 275, W - 50, 293),
                     "<h3 style='font-size:13px'>Table 1: Results by region</h3>")
    rows = [("Region", "Customers", "Growth"), ("North", "1,200", "12%"),
            ("South", "980", "8%"), ("East and Central", "1,540", "15%"), ("West", "760", "4%")]
    x0, top, cw, rh = 50, 300, [180, 120, 100], 22
    tw = sum(cw)
    p.draw_rect(pymupdf.Rect(x0, top, x0 + tw, top + rh), color=None, fill=(0.85, 0.9, 1))
    for r in range(len(rows) + 1):
        p.draw_line((x0, top + r * rh), (x0 + tw, top + r * rh), color=(0, 0, 0), width=0.8)
    cx = x0
    for c in range(len(cw) + 1):
        p.draw_line((cx, top), (cx, top + rh * len(rows)), color=(0, 0, 0), width=0.8)
        if c < len(cw):
            cx += cw[c]
    for r, row in enumerate(rows):
        cx = x0
        for c, cell in enumerate(row):
            p.insert_text((cx + 5, top + r * rh + 15), cell, fontsize=10,
                          fontname="hebo" if r == 0 else "helv")
            cx += cw[c]
    # bar chart
    p.insert_htmlbox(pymupdf.Rect(50, 430, W - 50, 448),
                     "<h3 style='font-size:13px'>Chart 1: Growth</h3>")
    base = 600
    p.draw_line((60, base), (360, base), color=(0, 0, 0))
    p.draw_line((60, base), (60, 460), color=(0, 0, 0))
    for i, (name, val) in enumerate([("North", 12), ("South", 8), ("East", 15), ("West", 4)]):
        bx = 80 + i * 70
        p.draw_rect(pymupdf.Rect(bx, base - val * 8, bx + 40, base), color=None, fill=(0.2, 0.5, 0.3))
        p.insert_text((bx + 5, base + 14), name, fontsize=9)
    p.insert_text((W / 2 - 3, H - 30), "2", fontsize=9)

    # ---------------- page 3: list, shaded box, line-by-line text ---------
    p = doc.new_page(width=W, height=H)
    p.insert_htmlbox(pymupdf.Rect(50, 50, W - 50, 75),
                     "<h2 style='font-size:16px'>Next Steps</h2>")
    y = 95
    for item in ["Translate every document into Telugu and Hindi.",
                 "Keep tables, charts and images in their original position.",
                 "Review the output pages for overlapping text."]:
        p.insert_text((60, y), "• " + item, fontsize=11, fontname="helv")
        y += 18
    p.draw_rect(pymupdf.Rect(50, 160, W - 50, 260), color=(0.6, 0.4, 0), fill=(1, 0.95, 0.8))
    p.insert_htmlbox(pymupdf.Rect(60, 168, W - 60, 255),
                     "<p style='font-size:11px'><b>Note:</b> This shaded box has a coloured "
                     "background that must remain visible after the original text is removed. "
                     "The border must also survive.</p>")
    y = _write_lines(p, 50, 290, LOREM + " " + LOREM, 95, size=10)
    p.insert_text((50, y + 20), "Contact: support@example.com", fontsize=10)
    p.insert_text((W / 2 - 3, H - 30), "3", fontsize=9)

    path = Path(path)
    doc.save(path)
    doc.close()
    return path


def make_scanned(path: str | Path, pages: int = 2) -> Path:
    """Pages that are only an image (like a scan), no text layer."""
    doc = pymupdf.open()
    W, H = pymupdf.paper_size("a4")
    pix = _image_pixmap(200, 280)
    for _ in range(pages):
        p = doc.new_page(width=W, height=H)
        p.insert_image(p.rect, pixmap=pix)
    path = Path(path)
    doc.save(path)
    doc.close()
    return path


def make_encrypted(path: str | Path) -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Secret text in an encrypted document.")
    path = Path(path)
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    doc.close()
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    print(make_sample(out / "sample.pdf"))
    print(make_scanned(out / "scanned.pdf"))
    print(make_encrypted(out / "encrypted.pdf"))
