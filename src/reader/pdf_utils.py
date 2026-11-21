"""Backend-only PDF utilities using PyMuPDF (`pymupdf`).

Never imported by sandbox packages (`workload/`, `sim/`, `rules/`, `controllers/`, `evolve/`).
"""

from __future__ import annotations

from pathlib import Path

import pymupdf


def get_pdf_page_count(pdf_source: str | Path | bytes) -> int:
    """Return total number of pages in a PDF file or bytes buffer."""
    if isinstance(pdf_source, (bytes, bytearray)):
        doc = pymupdf.open(stream=bytes(pdf_source), filetype="pdf")
    else:
        doc = pymupdf.open(str(pdf_source))
    try:
        return len(doc)
    finally:
        doc.close()


def render_pdf_page_png(
    pdf_source: str | Path | bytes,
    page_1indexed: int,
    dpi: int = 150,
) -> bytes:
    """Render a 1-indexed PDF page to PNG bytes."""
    if page_1indexed < 1:
        raise ValueError(f"page_1indexed must be >= 1, got {page_1indexed}")

    if isinstance(pdf_source, (bytes, bytearray)):
        doc = pymupdf.open(stream=bytes(pdf_source), filetype="pdf")
    else:
        doc = pymupdf.open(str(pdf_source))

    try:
        if page_1indexed > len(doc):
            raise ValueError(f"Requested page {page_1indexed} exceeds PDF page count ({len(doc)})")
        page = doc[page_1indexed - 1]
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def render_text_to_png(text: str, width: int = 1000, height: int = 1200, fontsize: int = 16) -> bytes:
    """Create a clean PNG page image containing plain text (useful for tests & camera simulation)."""
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=width, height=height)
        rect = pymupdf.Rect(50, 50, width - 50, height - 50)
        page.insert_textbox(rect, text, fontsize=fontsize, fontname="helv", color=(0, 0, 0))
        pix = page.get_pixmap(dpi=150, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()
