"""PDF page text (app/pdf_pages.py): pages in order, a broken file as PdfPageError, pymupdf loaded on first use
(2026-11-22). Run: python -m pytest test_pdf_pages.py -q"""

import subprocess
import sys
from pathlib import Path

import pytest

from app import pdf_pages as pp

pymupdf = pytest.importorskip("pymupdf")


def make_pdf(*texts):
    doc = pymupdf.open()
    for text in texts:
        doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def test_pages_come_back_in_order_with_the_total():
    data = make_pdf("first page", "second page", "third page")
    assert pp.pdf_page_count(data) == 3
    texts, total = pp.extract_pdf_page_texts(data, 2, 1)
    assert texts == ["second page"] and total == 3
    assert pp.extract_pdf_page_texts(data)[0] == ["first page", "second page", "third page"]


def test_a_broken_file_is_a_pdf_page_error():
    with pytest.raises(pp.PdfPageError):
        pp.extract_pdf_page_texts(b"not a pdf at all")


def test_importing_the_module_does_not_load_pymupdf():
    backend = str(Path(__file__).resolve().parent)
    code = f"import sys; sys.path.insert(0, {backend!r}); import app.pdf_pages; print('pymupdf' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "False", out.stderr[-500:]
