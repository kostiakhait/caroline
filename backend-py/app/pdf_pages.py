"""Per-page PDF text extraction -- the ONE place in this backend allowed to
turn a PDF's bytes into model-visible content.

Per explicit instruction (2026-09-15): "Большие многостраничные документы
должны анализировать по частям. Каждая страница сначала парситься отдельно
и только ее текстовое описание передаваться в модель. Никогда документ
целиком" -- a multi-page document must never reach the model as raw
document/image bytes (the native Claude Code Read tool, or an SDK
"document" content block, both hand it the whole thing at once, no
per-page control at all -- exactly the mechanism that produced the
41-image/~48.5MB context-bloat incident this same day, for images rather
than documents, but the identical failure shape). Every page is parsed
separately here and only its extracted TEXT is ever handed onward.

Uses PyMuPDF (imported as `fitz` upstream, `pymupdf` is the modern import
name -- see PYTHON_PACKAGES/PythonInstaller.cs, this must stay listed there
for any real install to have it) purely for text extraction -- never asked
to render a page as an image, which would just reintroduce the same
bloat/whole-document problem one layer down.
"""

from __future__ import annotations

import pymupdf


class PdfPageError(Exception):
    pass


def pdf_page_count(pdf_bytes: bytes) -> int:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc.page_count
    finally:
        doc.close()


def extract_pdf_page_texts(pdf_bytes: bytes, page_start: int = 1, page_count: int | None = None) -> tuple[list[str], int]:
    """Extracts plain text from a 1-indexed inclusive range of pages,
    starting at page_start. page_count=None means "the rest of the
    document" -- callers that want the WHOLE document's text (still never
    the raw bytes/images -- text only) pass page_start=1, page_count=None
    and are responsible for their own budget/truncation decision on the
    result, same as any other large tool output.

    Returns (page_texts, total_page_count) -- page_texts[i] is page
    (page_start + i)'s own extracted text, "" if that page has no
    extractable text (a scanned/image-only page -- not OCR'd here, out of
    scope for this fix)."""
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise PdfPageError(f"Could not open PDF: {exc}") from exc
    try:
        total = doc.page_count
        if page_start < 1:
            page_start = 1
        end = total if page_count is None else min(total, page_start - 1 + page_count)
        texts = [doc.load_page(i - 1).get_text().strip() for i in range(page_start, end + 1)]
        return texts, total
    finally:
        doc.close()


def format_pages_for_model(page_texts: list[str], page_start: int, total_pages: int) -> str:
    """The one shared rendering of "here's what was on these pages" --
    used by both the attachment-ingestion path and the read_document_pages
    tool, so the model sees the same page-labeled shape either way."""
    parts = [f"[PDF pages {page_start}-{page_start + len(page_texts) - 1} of {total_pages}, text only -- extracted per-page, not sent as document bytes.]"]
    for offset, text in enumerate(page_texts):
        page_num = page_start + offset
        parts.append(f"--- Page {page_num} ---\n{text or '[no extractable text on this page]'}")
    return "\n\n".join(parts)
