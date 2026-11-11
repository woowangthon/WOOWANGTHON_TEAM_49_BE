import io
import re
from collections import Counter

import pytest
from pypdf import PdfReader

from agent.graphs.extraction_graph import verify_response
from agent.schemas.public.tasks import AgentFailure, AgentOutcome
from agent.services.hashing import content_hash
from agent.services.normalization import normalize_scalar
from agent.tests.fixtures_support import context
from agent.tests.support.contract_corpus import (
    DEFAULT_OUTPUT,
    FIELDS,
    ROOT,
    hwp_images,
    ordered_blocks,
    read_json,
    select_json_sources,
)
from agent.tests.support.evaluate_corpus import score_document


def compact(text):
    return re.sub(r"\s+", "", text)


@pytest.mark.private_corpus
def test_seed_selection_counts_duplicates_and_priority():
    first, second = select_json_sources(), select_json_sources()
    assert [x[1] for x in first] == [x[1] for x in second]
    assert Counter(x[0] for x in first) == {
        "근로(고용)계약서": 12,
        "아르바이트계약서": 4,
        "기타고용계약서": 4,
    }
    assert len({content_hash(x[1].read_bytes()) for x in first}) == 20
    assert first[0][1].name == "근로계약서_0002.json"
    assert first[12][1].name == "아르바이트계약서_0032.json"
    assert all("Validation" in x[1].parts for x in first)


@pytest.mark.private_corpus
def test_actual_json_pdf_content_order_mapping_and_independent_gold():
    manifest = read_json(DEFAULT_OUTPUT / "manifest.json")
    assert len(manifest["documents"]) == 37
    for doc in manifest["documents"]:
        path = DEFAULT_OUTPUT / doc["pdf"]
        assert content_hash(path.read_bytes()) == doc["sha256"]
        gold = read_json(DEFAULT_OUTPUT / "gold" / (doc["id"] + ".json"))
        assert gold["reviewed_before_calls"] and set(gold["fields"]) == set(FIELDS)
        if doc["kind"] != "json":
            continue
        mapping = read_json(DEFAULT_OUTPUT / "mappings" / (doc["id"] + ".json"))
        original = ROOT / mapping["source"]
        assert content_hash(original.read_bytes()) == mapping["source_hash"] == gold["source_hash"]
        assert [b["paragraph_id"] for b in mapping["paragraphs"]] == [
            b["id"] for b in ordered_blocks(original)
        ]
        pages = PdfReader(path).pages
        assert len(pages) == mapping["page_count"]
        texts = [(p.extract_text() or "").splitlines()[2:] for p in pages]
        joined = compact("".join("".join(lines) for lines in texts))
        cursor = 0
        for block in mapping["paragraphs"]:
            text = compact(block["rendered_text"])
            if text:
                position = joined.find(text, cursor)
                assert position >= cursor, (doc["id"], block["paragraph_id"])
                cursor = position + len(text)
            assert all(1 <= p <= len(pages) for p in block["pages"])
        assert "human-source-v1" not in joined
        assert "annotation_version" not in joined


@pytest.mark.private_corpus
def test_hwp_original_images_pixel_hash_proportion_and_raster_samples():
    manifest = read_json(DEFAULT_OUTPUT / "manifest.json")
    for doc in manifest["documents"]:
        if doc["kind"] not in ("blank_form", "synthetic"):
            continue
        reader = PdfReader(DEFAULT_OUTPUT / doc["pdf"])
        assert len(reader.pages) == 2
        assert all(not (p.extract_text() or "").strip() for p in reader.pages)
        if doc["kind"] == "blank_form":
            original_pages = hwp_images(ROOT / doc["source"])
            mapping = read_json(DEFAULT_OUTPUT / "mappings" / (doc["id"] + ".json"))
            for page, (_, data, original), saved in zip(reader.pages, original_pages, mapping["pages"]):
                assert content_hash(data) == saved["image_hash"]
                restored = page.images[0].image.convert("RGB")
                assert restored.size == original.size
                assert content_hash(restored.tobytes()) == content_hash(original.tobytes())
                assert float(page.mediabox.width / page.mediabox.height) == pytest.approx(
                    original.width / original.height, abs=1e-6
                )


@pytest.mark.parametrize(
    "raw,unit,expected",
    [
        ("300만원", "KRW/month", "3000000"),
        ("1억2천만원", "KRW/year", "120000000"),
        ("0원", "KRW", "0"),
        ("1시간 30분", "minute", "90"),
        ("2022년 11월 1일", "date", "2022-11-01"),
        ("2026-10-01", "date", "2026-10-01"),
        ("오후 7시", "time", "19:00"),
        ("오전 12시", "time", "00:00"),
        ("25일", "day_of_month", "25"),
        ("매월 말일", "day_of_month", "last_day"),
        ("연 봉", "category", "annual"),
    ],
)
def test_source_preserving_normalization(raw, unit, expected):
    assert normalize_scalar(raw, unit) == expected


@pytest.mark.parametrize(
    "raw,unit",
    [
        ("09:00~18:00", "minute"),
        ("2023년 월 일", "date"),
        ("15,000 또는 20,000", "KRW/hour"),
        ("00일", "day_of_month"),
    ],
)
def test_normalization_rejects_inference_and_placeholders(raw, unit):
    with pytest.raises(ValueError):
        normalize_scalar(raw, unit)


def test_image_quote_does_not_claim_text_verification_and_text_quotes_must_match():
    response = {
        "document_type": "contract",
        "fields": [
            {
                "field_id": "contract_rate",
                "raw_value": "300만원",
                "normalized_value": "3000000",
                "unit": "KRW/month",
                "page": 1,
                "quote": "월급 300만원",
                "issues": [],
            }
        ],
        "issues": [],
    }
    verify_response(response, ["Small overlay only"], "contract", [True])
    item = verify_response(response, ["Small overlay only"], "contract", [False]).fields[0]
    assert item.validation_status == "needs_review" and item.normalized_value is None
    assert not item.evidence_valid and "SOURCE_QUOTE_MISMATCH" in item.validation_reasons


def test_failed_extraction_does_not_pass_blank_metrics():
    job = context("extract_document")
    outcome = AgentOutcome(
        job_id=job.job_id,
        run_id=job.run_id,
        provider_mode="openai",
        status="failed",
        error=AgentFailure(code="TEST", message="failed"),
    )
    gold = {
        "fields": {"contract_rate": {"state": "absent", "value": None, "unit": None, "source_quote": "blank"}}
    }
    assert score_document(gold, outcome)["fields"][0]["blank_hallucination"] is None


@pytest.mark.private_corpus
def test_reconstruction_pdf_remains_readable_without_source_layout():
    data = (DEFAULT_OUTPUT / "pdfs/json-01.pdf").read_bytes()
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
    assert "JSON 본문 재구성" in text and "9,620원" in text
    assert "<table>" not in text
