"""Viewer highlight boxes: reading coordinates → field source `boundingBox`."""

import io

import pytest
from reportlab.pdfgen.canvas import Canvas

from agent.config import AgentSettings
from agent.services import local_ocr
from agent.services.document_ocr import recognize_document
from app.agent_review import confirm_candidate, initialize_review
from app.db import load
from app.source_location import bounding_box, union_box
from test_runtime_db import Harness


def generated_pdf(draw):
    stream = io.BytesIO()
    canvas = Canvas(stream, pagesize=(595, 842), invariant=1)
    draw(canvas)
    canvas.save()
    return stream.getvalue()


def assert_viewer_box(box):
    # Same acceptance rule as the frontend highlightBox().
    x, y, width, height = box
    assert x >= 0 and y >= 0 and width > 0 and height > 0
    assert x + width <= 1 and y + height <= 1


async def test_digital_page_value_box_matches_its_drawn_position(monkeypatch):
    def draw(canvas):
        canvas.drawString(50, 780, "PAYROLL RECORD")
        canvas.drawString(50, 750, "Base pay")
        canvas.drawString(350, 750, "2,000,000")

    monkeypatch.setattr(local_ocr, "recognize_pdf", lambda *args: pytest.fail("digital page must not OCR"))
    recognized = await recognize_document(generated_pdf(draw), AgentSettings())
    assert recognized["layout_pages"][0]["page"] == 1
    box = bounding_box(recognized, 1, "Base pay 2,000,000", "2,000,000")
    assert_viewer_box(box)
    x, y, width, height = box
    # The value starts at 350pt of a 595pt-wide page; 750pt from the bottom of an 842pt page.
    assert abs(x - 350 / 595) < 0.01
    assert y < (842 - 750) / 842 < y + height + 0.01
    assert width < 0.15


async def test_scanned_page_keeps_ocr_word_boxes(monkeypatch):
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t100\t200\t80\t20\t95\t월",
            "5\t1\t1\t1\t1\t2\t200\t200\t120\t20\t95\t통상임금:",
            "5\t1\t1\t1\t1\t3\t340\t200\t160\t20\t90\t3,000,000원",
        ]
    )
    words = local_ocr.tsv_words(tsv, 1000, 2000)
    assert words[2]["bbox"] == [0.34, 0.1, 0.5, 0.11]
    assert local_ocr.tsv_words("level\tleft\nbroken", 1000, 2000) == []

    def recognize(data, settings, pages):
        return [{"text": "월 통상임금: 3,000,000원", "lines": [], "words": words}]

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)
    monkeypatch.setattr("agent.services.document_ocr.read_pdf_text_page", lambda page: None)
    recognized = await recognize_document(generated_pdf(lambda canvas: canvas.rect(10, 10, 50, 50)), AgentSettings())
    assert recognized["page_methods"] == ["python_ocr"]
    box = bounding_box(recognized, 1, "월 통상임금: 3,000,000원", "3,000,000원")
    assert_viewer_box(box)
    assert box == pytest.approx([0.338, 0.098, 0.164, 0.014])


def test_ambiguous_missing_or_unlocated_sources_have_no_box():
    def word(text, x, y):
        return {"text": text, "bbox": [x, y, x + 0.1, y + 0.02]}

    layout = {
        "layout_pages": [
            {
                "page": 1,
                "blocks": [
                    {"text": "기본급 100", "words": [word("기본급", 0.1, 0.1), word("100", 0.3, 0.1)]},
                    {"text": "기본급 100", "words": [word("기본급", 0.1, 0.5), word("100", 0.3, 0.5)]},
                ],
            }
        ]
    }
    assert bounding_box(layout, 1, "기본급 100", "100") is None  # repeated: never anchor to the first one
    assert bounding_box(layout, 2, "기본급 100", "100") is None
    assert bounding_box(layout, 1, None, "100") is None
    assert bounding_box({"pages": ["legacy reading without coordinates"]}, 1, "기본급 100", "100") is None
    assert union_box([[0.99, 0.99, 1.0, 1.0]]) == [0.988, 0.988, 0.012, 0.012]
    assert_viewer_box(union_box([[0.0, 0.0, 1.0, 1.0]]))
    assert union_box([]) is None


async def test_extracted_and_confirmed_fields_expose_viewer_boxes(tmp_path):
    harness = Harness(tmp_path / "boxes")
    initialize_review(harness.db)
    doc_id = harness.document()
    read = await harness.execute(harness.service.enqueue_read(1, doc_id))
    assert read["status"] == "COMPLETED", read
    extraction = await harness.execute(harness.service.enqueue_extraction(1, doc_id, document_type="payslip"))
    assert extraction["status"] == "COMPLETED", extraction
    candidate_id = load(extraction["output_json"])["candidate_id"]

    def source():
        with harness.db.connect() as con:
            row = con.execute("SELECT source_json FROM fields WHERE document_id=? AND key='gross'", (doc_id,)).fetchone()
        return load(row["source_json"])

    extracted = source()
    assert extracted["coordinateSystem"] == "RELATIVE_0_1"
    assert_viewer_box(extracted["boundingBox"])
    with harness.db.connect(write=True) as con:
        confirm_candidate(con, 1, doc_id, candidate_id, "gross")
    confirmed = source()
    assert confirmed["boundingBox"] == extracted["boundingBox"]
