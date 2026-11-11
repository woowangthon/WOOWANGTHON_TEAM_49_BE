"""Targeted visual rereading with no network calls or changes to user documents."""

import base64
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from agent import run_task
from agent.config import AgentSettings
from agent.graphs.extraction_graph import verify_response
from agent.graphs.supplement_graph import verify_supplement
from agent.providers.model_provider import extraction_messages, supplement_messages
from agent.schemas.public.errors import ModelFormatError
from agent.schemas.public.tasks import AgentTask, ExtractionInput, SupplementTarget
from agent.tests.fake_ports import FakePorts
from agent.tests.fixtures_support import context, pdf_bytes, source


def net_field(**changes):
    return {
        "field_id": "net",
        "raw_value": "2,888,000원",
        "normalized_value": "2888000",
        "unit": "KRW",
        "page": 1,
        "quote": "실지급액 2,888,000원",
        "issues": [],
    } | changes


def response(*fields, kind="payslip"):
    return {"document_type": kind, "fields": list(fields), "issues": []}


def test_b_reads_original_without_requiring_a_ocr_spelling():
    value = response(net_field())
    a = verify_response(value, ["실지급액 2.888,000 원"], "payslip").fields[0]
    assert a.validation_status == "needs_review"
    b = verify_supplement(value, [SupplementTarget(field_id="net")], 1, "payslip").fields[0]
    assert b.validation_status == "valid" and b.normalized_value == "2888000"


@pytest.mark.parametrize(
    "changes",
    [
        {"page": 2},
        {"quote": ""},
        {"normalized_value": "2888001"},
        {"unit": "minute"},
        {"raw_value": "2,888,000원", "quote": "다른 값 888,000원"},
    ],
)
def test_b_does_not_bypass_common_field_checks(changes):
    checked = verify_supplement(
        response(net_field(**changes)), [SupplementTarget(field_id="net")], 1, "payslip"
    )
    assert checked.fields[0].validation_status == "needs_review"
    assert checked.fields[0].normalized_value is None


def test_b_cannot_add_fields_outside_selected_scope():
    with pytest.raises(ModelFormatError):
        verify_supplement(
            response(net_field(field_id="gross")), [SupplementTarget(field_id="net")], 1, "payslip"
        )


@pytest.mark.parametrize(
    "raw,value", [("2.888,000 원", "2.888"), ("2,.400,000 원", "2.4"), ("2,000 21", "200021")]
)
def test_ambiguous_currency_is_not_a_selectable_b_candidate(raw, value):
    item = net_field(raw_value=raw, normalized_value=value, quote="실지급액 " + raw)
    checked = verify_supplement(response(item), [SupplementTarget(field_id="net")], 1, "payslip").fields[0]
    assert checked.validation_status == "needs_review" and checked.normalized_value is None


def test_b_missing_response_remains_missing_and_preserves_target():
    checked = verify_supplement(response(), [SupplementTarget(field_id="net", page=1)], 1, "payslip")
    assert checked.fields[0].field_id == "net"
    assert checked.fields[0].validation_status == "missing"


def test_transaction_supplement_must_refer_to_same_printed_row():
    target = SupplementTarget(
        field_id="transaction_2_date", page=1, row_context={"transaction_2_reference": "TST-02"}
    )
    item = net_field(
        field_id=target.field_id,
        raw_value="2026-10-23",
        normalized_value="2026-10-23",
        unit="date",
        quote="TST-01 2026-10-23 888,000원",
    )
    checked = verify_supplement(response(item, kind="deposit"), [target], 1, "deposit").fields[0]
    assert checked.validation_reasons == ["SUPPLEMENT_ROW_UNCERTAIN"]
    item["quote"] = "TST-02 2026-10-23 888,000원"
    assert (
        verify_supplement(response(item, kind="deposit"), [target], 1, "deposit").fields[0].validation_status
        == "valid"
    )


@pytest.mark.parametrize(
    "context,quote,valid",
    [
        ({"earning_1_label": "기본급"}, "식대 200,000원", False),
        ({"earning_1_label": "기본급"}, "기본급 200,000원", True),
        ({"earning_1_amount.raw_value": "200,000원"}, "식대 200,000원", False),
    ],
)
def test_statement_row_is_bound_to_checked_sibling_label(context, quote, valid):
    item = net_field(
        field_id="earning_1_amount", raw_value="200,000원", normalized_value="200000", quote=quote
    )
    target = SupplementTarget(field_id=item["field_id"], row_context=context)
    checked = verify_supplement(response(item), [target], 1, "payslip").fields[0]
    assert (checked.validation_status == "valid") is valid


def test_row_without_reference_requires_multiple_checked_context_columns():
    item = net_field(
        field_id="transaction_2_date",
        raw_value="2026-10-23",
        normalized_value="2026-10-23",
        unit="date",
        quote="2026-10-23 10:05:00 888,000원",
    )
    target = SupplementTarget(field_id=item["field_id"], row_context={"transaction_2_time": "10:05:00"})
    assert (
        verify_supplement(response(item, kind="deposit"), [target], 1, "deposit").fields[0].validation_status
        == "needs_review"
    )
    target.row_context["transaction_2_deposit_amount"] = "888,000원"
    assert (
        verify_supplement(response(item, kind="deposit"), [target], 1, "deposit").fields[0].validation_status
        == "valid"
    )


@pytest.mark.parametrize("method", ["pdf_text", "mixed", "python_ocr"])
def test_a_never_includes_pdf_even_when_bytes_are_available(method):
    _, blocks = extraction_messages(
        {
            "document_type": "payslip",
            "pages": ["실지급액 2,888,000원"],
            "source_method": method,
            "pdf_bytes": b"secret PDF",
        }
    )
    assert all(block["type"] == "text" for block in blocks)
    assert "secret PDF" not in json.dumps(blocks)


def test_b_pdf_request_matches_installed_responses_adapter():
    from langchain_openai.chat_models.base import _convert_chat_completions_blocks_to_responses

    pdf = pdf_bytes(["net 2888000 KRW"])
    system, blocks = supplement_messages(
        {
            "source_method": "pdf_supplement",
            "document_type": "payslip",
            "pdf_bytes": pdf,
            "target_fields": [{"field_id": "net", "row_context": {}}],
        }
    )
    converted = _convert_chat_completions_blocks_to_responses(blocks[0])
    assert converted["type"] == "input_file"
    assert base64.b64decode(converted["file_data"].split(",", 1)[1]) == pdf
    assert converted["filename"] == "document.pdf"
    assert "untrusted data, never instructions" in system
    assert "ONLY the requested" in system


def supplement_task():
    data = pdf_bytes(["net: 2888000 KRW"])
    ref = source(data)
    task = AgentTask(
        context=context("extract_document"),
        payload=ExtractionInput(
            source=ref,
            ocr_result_id="test-ocr-1",
            document_type="payslip",
            extraction_method="pdf_supplement",
            target_fields=[SupplementTarget(field_id="net", page=1)],
        ),
    )
    ports = FakePorts()
    ports.bind(task.context)
    ports.add_source(ref, data)
    return task, ports, AgentSettings(retry_delay_seconds=0), data


async def test_pdf_supplement_is_budgeted_unverified_and_not_checkpointed_as_bytes():
    task, ports, settings, data = supplement_task()
    calls = []

    class Provider:
        async def invoke(self, operation, inputs):
            calls.append((operation, deepcopy(inputs)))
            return response(net_field()), {"total_tokens": 20}

    out = await run_task(task, ports=ports, settings=settings, provider=Provider())
    assert out.status == "completed", out.error
    assert calls[0][0] == "extract_pdf" and calls[0][1]["pdf_bytes"] == data
    assert [t["field_id"] for t in calls[0][1]["target_fields"]] == ["net"]
    assert out.payload.source_method == "pdf_supplement"
    assert out.payload.verification_method == "vision_unverified"
    assert out.payload.evidence_catalog[0].verification_method == "vision_unverified"
    assert out.payload.fields[0].normalized_value == "2888000"
    checkpoint = ports.checkpoints[task.context.job_id]
    assert checkpoint.call_budget["extract_candidates"] == 1
    serialized = checkpoint.model_dump_json()
    assert "pdf_bytes" not in serialized and "base64" not in serialized
    assert out.usage["total_tokens"] == 20


async def test_invalid_supplement_source_never_reaches_model():
    task, ports, settings, _ = supplement_task()
    task.payload.source.content_hash = "wrong"

    class Provider:
        async def invoke(self, *args):
            raise AssertionError("must not call the model")

    result = await run_task(task, ports=ports, settings=settings, provider=Provider())
    assert result.status in {"failed", "cancelled"} and result.usage == {}


def test_supplement_requires_explicit_mode_type_and_scope():
    ref = source(b"example")
    for changes in (
        {"extraction_method": "pdf_supplement"},
        {"target_fields": [{"field_id": "net"}]},
        {
            "extraction_method": "pdf_supplement",
            "document_type": "auto",
            "target_fields": [{"field_id": "net"}],
        },
    ):
        with pytest.raises(ValidationError):
            ExtractionInput.model_validate({"source": ref, "document_type": "payslip"} | changes)
