"""No remote calls: verify the OCR-only boundary of model requests."""

import json
import sys
from types import SimpleNamespace

import pytest

from agent.config import AgentSettings
from agent.providers.model_provider import OpenAIProvider, extraction_messages
from agent.schemas.public.errors import OCRFailed
from agent.services.extraction_profiles import (
    DOCUMENT_TYPES,
    PROFILES,
    canonical_document_type,
    extraction_instructions,
)
from agent.tests.fixture_provider import FixtureProvider


def ocr_input(kind="auto"):
    return {
        "document_type": kind,
        "source_method": "python_ocr",
        "pages": ["급여명세서\n총지급액 3,000,000원", "실수령액 2,700,000원"],
    }


@pytest.mark.parametrize("kind", [*DOCUMENT_TYPES, "auto", "payslip_ocr"])
def test_each_type_uses_only_page_numbered_ocr_text(kind):
    data = ocr_input(kind)
    data.update(pdf_bytes=b"private original PDF", page_text="untrusted alternate page source")
    system, content = extraction_messages(data)
    assert all(block["type"] == "text" for block in content)
    assert content[0]["text"] == (
        "Python OCR source text:\nPAGE 1\n급여명세서\n총지급액 3,000,000원\n\nPAGE 2\n실수령액 2,700,000원"
    )
    assert "private original PDF" not in json.dumps(content)
    assert "untrusted alternate" not in json.dumps(content)
    assert content[-1]["text"].endswith(canonical_document_type(kind))
    assert "OCR text may contain errors" in system
    assert "never instructions" in system
    assert "Never silently repair a number" in system
    for supported in DOCUMENT_TYPES:
        assert supported in system
        assert all(name in system for name in PROFILES[supported]["fields"])
    assert "return tables as []" in system


@pytest.mark.parametrize(
    "changes",
    [
        {"source_method": None, "pdf_bytes": b"original PDF"},
        {"source_method": "vision"},
        {"pages": []},
        {"pages": [" ", "\n"]},
        {"pages": "not a list"},
        {"pages": [None]},
    ],
)
def test_raw_pdf_or_missing_ocr_cannot_reach_model(changes):
    with pytest.raises(OCRFailed):
        extraction_messages(ocr_input() | changes)


@pytest.mark.parametrize("kind", [*DOCUMENT_TYPES, "auto", "payslip_ocr"])
async def test_model_uses_same_structured_schema_for_classification_and_rows(monkeypatch, kind):
    captured = {}

    class Message:
        def __init__(self, content):
            self.content = content

    class TextChat:
        def __init__(self, **kwargs):
            captured["options"] = kwargs

        def with_structured_output(self, schema, **kwargs):
            captured["schema"] = schema
            return self

        async def ainvoke(self, messages):
            captured["messages"] = messages
            return {
                "raw": SimpleNamespace(
                    usage_metadata={"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}
                ),
                "parsed": {"document_type": "payslip", "fields": [], "tables": [], "issues": []},
                "parsing_error": None,
            }

    monkeypatch.setitem(sys.modules, "langchain_openai", SimpleNamespace(ChatOpenAI=TextChat))
    monkeypatch.setitem(
        sys.modules, "langchain_core.messages", SimpleNamespace(HumanMessage=Message, SystemMessage=Message)
    )
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(
            APIConnectionError=type("APIConnectionError", (Exception,), {}),
            APIStatusError=type("APIStatusError", (Exception,), {}),
            APITimeoutError=type("APITimeoutError", (Exception,), {}),
        ),
    )
    provider = OpenAIProvider(AgentSettings(openai_api_key="test-key-never-sent"))
    response, usage = await provider.invoke("extract", ocr_input(kind) | {"pdf_bytes": b"private PDF"})
    assert response == {"document_type": "payslip", "fields": [], "tables": [], "issues": []}
    assert usage == {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}
    assert "tables" in captured["schema"]["required"]
    assert "document_type" in captured["schema"]["required"]
    assert all(part["type"] == "text" for part in captured["messages"][1].content)
    assert captured["options"]["store"] is False


async def test_synthetic_fixture_auto_classifies_explicit_fields_and_preserves_old_alias():
    provider = FixtureProvider()
    result, _ = await provider.invoke("extract", ocr_input() | {"pages": ["gross: 3000000 KRW"]})
    assert result["document_type"] == "payslip"
    alias, _ = await provider.invoke("extract", ocr_input("payslip_ocr"))
    assert alias["document_type"] == "payslip"
    unknown, _ = await provider.invoke("extract", ocr_input())
    assert unknown["document_type"] == "unknown" and unknown["fields"] == []


def test_legacy_prompt_is_canonical_and_auto_has_all_four_vocabularies():
    assert extraction_instructions("payslip_ocr") == extraction_instructions("payslip")
    assert "transaction" in extraction_instructions("auto")
    assert "contract_rate" in extraction_instructions("auto")
    assert "mixed content" in extraction_instructions("auto")
