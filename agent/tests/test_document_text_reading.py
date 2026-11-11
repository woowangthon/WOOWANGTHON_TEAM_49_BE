"""Native PDF reading and page-local OCR fallback, without any model calls."""

import io
import re
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from reportlab.pdfgen.canvas import Canvas

from agent.config import AgentSettings
from agent.schemas.public.errors import OCRUnavailable
from agent.services import local_ocr
from agent.services.document_ocr import recognize_document
from agent.tests.fixtures_support import pdf_bytes


def generated_pdf(draw):
    stream = io.BytesIO()
    canvas = Canvas(stream, pagesize=(595, 842), invariant=1)
    draw(canvas)
    canvas.save()
    return stream.getvalue()


def reject_ocr(*args):
    pytest.fail("A usable digital page must not run OCR")


async def test_native_layout_preserves_numbers_and_table_rows(monkeypatch):
    def draw(canvas):
        canvas.drawString(50, 780, "PAYROLL RECORD")
        for y, date, amount in ((750, "2026-10-23", "2,000,000"), (720, "2026-10-24", "888,000")):
            canvas.drawString(50, y, date)
            canvas.drawString(350, y, amount)

    monkeypatch.setattr(local_ocr, "recognize_pdf", reject_ocr)
    result = await recognize_document(generated_pdf(draw), AgentSettings())
    assert result["page_methods"] == ["pdf_text"]
    assert result["source_method"] == "pdf_text"
    assert result["image_pages"] == [False]
    assert re.search(r"2026-10-23\s+2,000,000", result["pages"][0])
    assert re.search(r"2026-10-24\s+888,000", result["pages"][0])
    assert all("confidence" not in line for line in result["ocr_pages"][0]["lines"])


@pytest.mark.parametrize("kind", ["hidden", "transparent", "white", "rotated", "off_page", "sparse"])
async def test_suspicious_text_layer_uses_ocr_even_when_extractable(monkeypatch, kind):
    def draw(canvas):
        if kind == "transparent":
            canvas.setFillAlpha(0)
        elif kind == "white":
            canvas.setFillColorRGB(1, 1, 1)
        elif kind == "rotated":
            canvas.rotate(90)
        text = canvas.beginText(50 if kind != "off_page" else -2000, 750)
        if kind == "hidden":
            text.setTextRenderMode(3)
        text.textLine("x" if kind == "sparse" else "Gross 3,120,000 KRW")
        canvas.drawText(text)

    calls = []

    def recognize(data, settings, indices):
        calls.append(indices)
        return [{"text": "OCR fallback 3,120,000 KRW", "lines": []}]

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    data = generated_pdf(draw)
    assert PdfReader(io.BytesIO(data)).pages[0].extract_text().strip()
    result = await recognize_document(data, AgentSettings())
    assert calls == [[0]]
    assert result["source_method"] == "python_ocr"
    assert result["pages"] == ["OCR fallback 3,120,000 KRW"]


async def test_white_table_header_over_dark_cell_remains_usable(monkeypatch):
    def draw(canvas):
        canvas.setFillColorRGB(0.1, 0.2, 0.3)
        canvas.rect(40, 740, 400, 30, fill=1, stroke=0)
        canvas.setFillColorRGB(1, 1, 1)
        canvas.drawString(50, 750, "PAYROLL SUMMARY")
        canvas.setFillColorRGB(0, 0, 0)
        canvas.drawString(50, 720, "Gross 3,120,000 KRW")

    monkeypatch.setattr(local_ocr, "recognize_pdf", reject_ocr)
    result = await recognize_document(generated_pdf(draw), AgentSettings())
    assert result["source_method"] == "pdf_text"
    assert "PAYROLL SUMMARY" in result["pages"][0]


async def test_dark_text_on_dark_background_uses_ocr(monkeypatch):
    def draw(canvas):
        canvas.setFillColorRGB(0, 0, 0)
        canvas.rect(40, 740, 400, 30, fill=1, stroke=0)
        canvas.drawString(50, 750, "Hidden net 9,999,999 KRW")
        canvas.drawString(50, 710, "Visible net 2,888,000 KRW")

    calls = []

    def recognize(data, settings, indices):
        calls.append(indices)
        return [{"text": "Visible net 2,888,000 KRW", "lines": []}]

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    data = generated_pdf(draw)
    assert "9,999,999" in PdfReader(io.BytesIO(data)).pages[0].extract_text()
    result = await recognize_document(data, AgentSettings())
    assert calls == [[0]]
    assert result["source_method"] == "python_ocr"
    assert result["pages"] == ["Visible net 2,888,000 KRW"]


async def test_later_opaque_rectangle_covering_text_uses_ocr(monkeypatch):
    def draw(canvas):
        canvas.drawString(50, 750, "Net 9,999,999 KRW")
        canvas.setFillColorRGB(1, 1, 1)
        # Cover the amount without covering the text's starting point.
        canvas.rect(72, 745, 80, 20, fill=1, stroke=0)
        canvas.setFillColorRGB(0, 0, 0)
        canvas.drawString(72, 750, "2,888,000 KRW")

    calls = []

    def recognize(data, settings, indices):
        calls.append(indices)
        return [{"text": "Net 2,888,000 KRW", "lines": []}]

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    data = generated_pdf(draw)
    assert "9,999,999" in PdfReader(io.BytesIO(data)).pages[0].extract_text()
    result = await recognize_document(data, AgentSettings())
    assert calls == [[0]]
    assert result["source_method"] == "python_ocr"
    assert result["pages"] == ["Net 2,888,000 KRW"]


async def test_broken_character_mapping_selects_ocr(monkeypatch):
    reader = PdfReader(io.BytesIO(pdf_bytes(["Gross 3,120,000 KRW"])))
    font = reader.pages[0]["/Resources"]["/Font"]["/F1"].get_object()
    mapping = DecodedStreamObject()
    mapping.set_data(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        b"/CMapName /Broken def /CMapType 2 def "
        b"1 begincodespacerange <00> <FF> endcodespacerange "
        b"1 beginbfchar <47> <FFFD> endbfchar endcmap end end"
    )
    font[NameObject("/ToUnicode")] = mapping
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    output = io.BytesIO()
    writer.write(output)
    assert "\ufffd" in PdfReader(io.BytesIO(output.getvalue())).pages[0].extract_text()
    monkeypatch.setattr(
        local_ocr, "recognize_pdf", lambda *args: [{"text": "Gross 3,120,000 KRW", "lines": []}]
    )
    result = await recognize_document(output.getvalue(), AgentSettings())
    assert result["source_method"] == "python_ocr"


async def test_scan_with_hidden_text_is_not_trusted_and_mixed_pages_keep_order(monkeypatch):
    native = PdfReader(io.BytesIO(pdf_bytes(["Native net 2,888,000 KRW"])))
    scan = PdfReader(io.BytesIO(pdf_bytes(["Scanned net 2,888,000 KRW"], scanned=True)))

    def hidden(canvas):
        text = canvas.beginText(50, 750)
        text.setTextRenderMode(3)
        text.textLine("Hidden but plausible wrong net 9,999,999 KRW")
        canvas.drawText(text)

    writer = PdfWriter()
    writer.add_page(native.pages[0])
    scanned_page = writer.add_page(scan.pages[0])
    scanned_page.merge_page(PdfReader(io.BytesIO(generated_pdf(hidden))).pages[0])
    assert "9,999,999" in scanned_page.extract_text()
    output = io.BytesIO()
    writer.write(output)
    calls = []

    def recognize(data, settings, indices):
        calls.append(indices)
        return [{"text": "Scanned net 2,888,000 KRW", "lines": [{"text": "Scanned net", "confidence": 85}]}]

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    result = await recognize_document(output.getvalue(), AgentSettings())
    assert calls == [[1]]
    assert result["page_methods"] == ["pdf_text", "python_ocr"]
    assert result["source_method"] == "mixed"
    assert "Native net 2,888,000 KRW" in result["pages"][0]
    assert result["pages"][1] == "Scanned net 2,888,000 KRW"
    assert result["ocr_pages"][1]["lines"][0]["confidence"] == 85


async def test_unavailable_ocr_does_not_block_native_but_still_blocks_scan(monkeypatch):
    def unavailable(*args):
        raise OCRUnavailable("test OCR unavailable")

    monkeypatch.setattr(local_ocr, "recognize_pdf", unavailable)
    result = await recognize_document(pdf_bytes(["Native gross 3,120,000 KRW"]), AgentSettings())
    assert result["source_method"] == "pdf_text"
    with pytest.raises(OCRUnavailable):
        await recognize_document(pdf_bytes(["Scanned gross 3,120,000 KRW"], scanned=True), AgentSettings())


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("02_시급제_급여명세서.pdf", ("2,888,000", "2,400,000", "15,000", "232,000")),
        ("03_시급제_입금기록.pdf", ("2026-10-23", "2,000,000", "888,000", "2,120,000", "3,008,000")),
    ],
)
async def test_authored_payroll_pdfs_keep_exact_source_amounts_and_dates(monkeypatch, filename, expected):
    path = Path(__file__).resolve().parents[2] / "output/pdf/payroll-test-records" / filename
    if not path.is_file():
        pytest.skip("Optional authored payroll PDF fixture is not present")
    monkeypatch.setattr(local_ocr, "recognize_pdf", reject_ocr)
    result = await recognize_document(path.read_bytes(), AgentSettings())
    assert result["page_methods"] == ["pdf_text"]
    for token in expected:
        assert token in result["pages"][0]
