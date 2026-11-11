import asyncio
import copy
import io
import json
import re
import threading
import urllib.error
import urllib.request

import pytest
from pypdf import PdfReader, PdfWriter

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.dev.test_server import LocalHTTPServer
from agent.graphs.extraction_graph import verify_response
from agent.schemas.public.errors import ModelFormatError
from agent.services.extraction_profiles import extraction_instructions
from agent.services.extraction_tables import expand_tables_for_review
from agent.tests.fixtures_support import pdf_bytes


def example_response(kind):
    """Synthetic documents contain repeated dates/amounts to check row preservation."""
    entries = {
        "attendance": [
            ("worked_minutes", "8 hours", "480", "minute", "Total actual work: 8 hours"),
            ("work_1_date", "2026-10-01", "2026-10-01", "text", "Work date: 2026-10-01"),
            ("work_1_start", "22:00", "22:00", "time", "Start: 22:00"),
            ("work_1_end", "06:00", "06:00", "time", "End: 06:00"),
            ("work_1_break_minutes", "60 minutes", "60", "minute", "Break: 60 minutes"),
            ("work_2_date", "2026-10-01", "2026-10-01", "text", "Work date: 2026-10-01"),
            ("work_2_start", "09:00", "09:00", "time", "Start: 09:00"),
        ],
        "payslip": [
            ("gross", "3,000,000", "3000000", "KRW", "Gross: 3,000,000 KRW"),
            ("deduction", "300,000", "300000", "KRW", "Deduction total: 300,000 KRW"),
            ("net", "2,700,000", "2700000", "KRW", "Net: 2,700,000 KRW"),
            ("earning_1_label", "Basic pay", "Basic pay", "text", "Earning: Basic pay"),
            ("earning_1_amount", "2,800,000", "2800000", "KRW", "Basic pay amount: 2,800,000 KRW"),
            ("earning_2_amount", "200,000", "200000", "KRW", "Allowance amount: 200,000 KRW"),
            ("deduction_1_amount", "300,000", "300000", "KRW", "Tax: 300,000 KRW"),
        ],
        "deposit": [
            ("deposit_total", "2,700,000", "2700000", "KRW", "Deposit total: 2,700,000 KRW"),
            ("transaction_1_date", "2026-10-25", "2026-10-25", "date", "Date: 2026-10-25"),
            ("transaction_1_time", "10:00:00", "10:00:00", "time", "Time: 10:00:00"),
            ("transaction_1_deposit_amount", "1,350,000", "1350000", "KRW", "Deposit: 1,350,000 KRW"),
            ("transaction_1_balance", "1,450,000", "1450000", "KRW", "Balance: 1,450,000 KRW"),
            ("transaction_2_date", "2026-10-25", "2026-10-25", "date", "Date: 2026-10-25"),
            ("transaction_2_deposit_amount", "1,350,000", "1350000", "KRW", "Deposit: 1,350,000 KRW"),
            ("transaction_2_balance", "2,800,000", "2800000", "KRW", "Balance: 2,800,000 KRW"),
        ],
    }[kind]
    return {
        "document_type": kind,
        "fields": [
            {
                "field_id": fid,
                "raw_value": raw,
                "normalized_value": normalized,
                "unit": unit,
                "page": 1,
                "quote": quote,
                "issues": [],
            }
            for fid, raw, normalized, unit, quote in entries
        ],
        "issues": [],
    }


def example_pdf(kind, scanned=False):
    return pdf_bytes([f["quote"] for f in example_response(kind)["fields"]], scanned=scanned)


class DocumentProvider:
    def __init__(self):
        self.types = []

    async def invoke(self, operation, data):
        self.types.append(data["document_type"])
        return example_response(data["document_type"]), {}


@pytest.mark.parametrize("kind", ["attendance", "payslip", "deposit"])
@pytest.mark.parametrize("scanned", [False, True])
async def test_upload_extract_rows_and_restore_selected_type(tmp_path, kind, scanned, monkeypatch):
    if scanned:
        text = "\n".join(field["quote"] for field in example_response(kind)["fields"])
        monkeypatch.setattr(
            "agent.services.local_ocr.recognize_pdf",
            lambda *_: [{"text": text, "lines": [{"text": text, "confidence": 99}]}],
        )
    provider = DocumentProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    case = await store.upload(example_pdf(kind, scanned), kind, filename=kind + ".pdf")
    assert case["document_type"] == kind
    job = await store.create_job(case["id"], "extract")
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "completed", store.jobs[job["id"]]
    assert provider.types == [kind]
    case = await store.get_case(case["id"])
    assert len(case["extraction"]["fields"]) == len(example_response(kind)["fields"])
    assert case["extraction"]["verification_method"] == ("ocr_unverified" if scanned else "text_match")
    assert case["dirty"] and case["confirmed"] is None
    assert not case["draft"]["payment_scope_confirmed"]
    assert not case["draft"]["earnings_complete"]
    if kind == "deposit":
        assert case["draft"]["manual"]["deposit"] == "2700000"
        amounts = [f for f in case["extraction"]["fields"] if f["field_id"].endswith("_deposit_amount")]
        assert len(amounts) == 2 and all(f["normalized_value"] == "1350000" for f in amounts)
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    assert (await restored.get_case(case["id"])) == case


async def test_image_page_with_matching_text_keeps_evidence_unverified(tmp_path):
    reader = PdfReader(io.BytesIO(example_pdf("payslip", scanned=True)))
    text_layer = PdfReader(io.BytesIO(example_pdf("payslip")))
    writer, buffer = PdfWriter(), io.BytesIO()
    writer.add_page(reader.pages[0])
    writer.pages[0].merge_page(text_layer.pages[0])
    writer.write(buffer)
    store = LocalStore(AgentSettings(), state=tmp_path, provider=DocumentProvider())
    case = await store.upload(buffer.getvalue(), "payslip")
    job = await store.create_job(case["id"], "extract")
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "completed", store.jobs[job["id"]]
    result = (await store.get_case(case["id"]))["extraction"]
    assert result["verification_method"] == "ocr_unverified"
    assert result["evidence_catalog"]
    assert all(ref["verification_method"] == "ocr_unverified" for ref in result["evidence_catalog"])


async def test_document_type_change_invalidates_old_candidates_and_preserves_pdf(tmp_path):
    provider = DocumentProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    data = pdf_bytes([f["quote"] for k in ("attendance", "payslip") for f in example_response(k)["fields"]])
    case = await store.upload(data, "attendance")
    first = await store.create_job(case["id"], "extract")
    await store.tasks[first["id"]]
    changed = await store.change_document_type(case["id"], "payslip")
    assert changed["extraction"] is None and changed["draft"]["manual"] == {}
    assert changed["confirmed"] is None and changed["confirmed_revision"] is None
    assert changed["document_type_revision"] > first["revision"]
    assert store.document_data(case["document_id"]) == data
    second = await store.create_job(case["id"], "extract")
    await store.tasks[second["id"]]
    assert store.jobs[second["id"]]["status"] == "completed"
    assert provider.types == ["attendance", "payslip"]
    assert store.jobs[first["id"]]["task"]["payload"]["document_type"] == "attendance"


async def test_invalid_type_rejected_before_upload_and_active_job_cannot_change_type(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path)
    with pytest.raises(ValueError):
        await store.upload(example_pdf("deposit"), "unknown")
    assert not list((tmp_path / "uploads").glob("*"))
    case = await store.upload(example_pdf("deposit"), "deposit")
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingProvider(DocumentProvider):
        async def invoke(self, operation, data):
            entered.set()
            await release.wait()
            return await super().invoke(operation, data)

    store.provider = BlockingProvider()
    job = await store.create_job(case["id"], "extract")
    await asyncio.wait_for(entered.wait(), 3)
    try:
        with pytest.raises(ValueError):
            await store.change_document_type(case["id"], "payslip")
    finally:
        release.set()
        await store.tasks[job["id"]]


async def test_wrong_type_asks_for_change_without_paid_automatic_retry(tmp_path):
    class DepositProvider(DocumentProvider):
        async def invoke(self, operation, data):
            self.types.append(data["document_type"])
            return example_response("deposit"), {}

    provider = DepositProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    case = await store.upload(example_pdf("deposit"), "contract")
    job = await store.create_job(case["id"], "extract")
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "failed"
    assert "문서 유형" in store.jobs[job["id"]]["error"]
    assert provider.types == ["contract"]
    await store.change_document_type(case["id"], "deposit")
    job = await store.create_job(case["id"], "extract")
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "completed"


def test_row_units_and_source_quotes_are_still_validated():
    response = example_response("deposit")
    bad = copy.deepcopy(response)
    bad["fields"][3]["unit"] = "minute"
    item = verify_response(bad, ["\n".join(f["quote"] for f in bad["fields"])], "deposit").fields[3]
    assert item.normalized_value is None and item.validation_status == "needs_review"
    bad = copy.deepcopy(response)
    bad["fields"][3]["normalized_value"] = "1450000"
    item = verify_response(bad, ["\n".join(f["quote"] for f in bad["fields"])], "deposit").fields[3]
    assert item.normalized_value is None and item.validation_status == "needs_review"


@pytest.mark.parametrize("kind", ["attendance", "payslip", "deposit"])
def test_real_http_upload_change_type_extract_and_reload(tmp_path, kind):
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    async def initialize():
        return LocalStore(AgentSettings(), state=tmp_path, provider=DocumentProvider())

    store = asyncio.run_coroutine_threadsafe(initialize(), loop).result(timeout=5)
    server = LocalHTTPServer(("127.0.0.1", 0), store, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, method="GET", body=None):
        headers = {"X-Local-Agent": "1"}
        if isinstance(body, bytes):
            headers["Content-Type"] = "application/pdf"
        elif body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        with urllib.request.urlopen(
            urllib.request.Request(base + path, data=body, method=method, headers=headers), timeout=5
        ) as response:
            return json.load(response)

    try:
        assert kind in request("/api/extraction-profiles")
        case = request("/api/uploads?document_type=contract", "POST", example_pdf(kind))
        changed = request(f"/api/cases/{case['id']}/document-type", "PUT", {"document_type": kind})
        assert changed["document_type"] == kind
        job = request(f"/api/cases/{case['id']}/jobs", "POST", {"stage": "extract"})

        async def completion():
            await store.tasks[job["id"]]

        asyncio.run_coroutine_threadsafe(completion(), loop).result(timeout=5)
        assert request(f"/api/jobs/{job['id']}")["status"] == "completed"
        saved = request(f"/api/cases/{case['id']}")
        assert saved["document_type"] == saved["extraction"]["document_type"] == kind
        with pytest.raises(urllib.error.HTTPError) as error:
            request(f"/api/cases/{case['id']}/document-type", "PUT", {"document_type": "other"})
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        loop.close()


def test_type_specific_prompts_preserve_rows_and_forbid_inferred_totals():
    for kind in ("attendance", "payslip", "deposit"):
        prompt = extraction_instructions(kind)
        assert "Do not sum rows" in prompt and "raw_values" in prompt


def compact_response(kind):
    response = example_response(kind)
    groups = {}
    summary = []
    for item in response["fields"]:
        match = re.fullmatch(r"([a-z]+)_(\d+)_([a-z_]+)", item["field_id"])
        if not match:
            summary.append(item)
            continue
        groups.setdefault(match[1], {}).setdefault(match[2], {})[match[3]] = item
    tables = []
    for prefix, rows in groups.items():
        columns = list(dict.fromkeys(key for row in rows.values() for key in row))
        tables.append(
            {
                "prefix": prefix,
                "columns": columns,
                "header_quote": "",
                "rows": [
                    {
                        "page": 1,
                        "quote": "\n".join(f["quote"] for f in row.values()),
                        "raw_values": [row.get(key, {}).get("raw_value") for key in columns],
                        "normalized_values": [row.get(key, {}).get("normalized_value") for key in columns],
                        "issues": [],
                    }
                    for row in rows.values()
                ],
            }
        )
    return {**response, "fields": summary, "tables": tables}


@pytest.mark.parametrize("kind", ["attendance", "payslip", "deposit"])
async def test_compact_model_tables_complete_full_upload_graph(tmp_path, kind):
    class TableProvider(DocumentProvider):
        async def invoke(self, operation, data):
            return compact_response(data["document_type"]), {}

    store = LocalStore(AgentSettings(), state=tmp_path, provider=TableProvider())
    case = await store.upload(example_pdf(kind), kind)
    job = await store.create_job(case["id"], "extract")
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "completed", store.jobs[job["id"]]
    saved = store.cases[case["id"]]["extraction"]
    expected = {f["field_id"]: f["normalized_value"] for f in example_response(kind)["fields"]}
    for f in saved["fields"]:
        if f["normalized_value"] is not None:
            assert f["normalized_value"] == expected[f["field_id"]]
    assert {f["field_id"] for f in saved["fields"]} >= set(expected)


def month_table():
    rows = [
        {
            "page": 1,
            "quote": f"10/{day:02} 09:00 18:00 8",
            "raw_values": [f"10/{day:02}", "09:00", "18:00", "8"],
            "normalized_values": [f"10/{day:02}", "09:00", "18:00", "480"],
            "issues": [],
        }
        for day in range(1, 32)
    ]
    header = "기록 단위: 시간(h)"
    page = header + "\n" + "\n".join(row["quote"] for row in rows)
    response = {
        "document_type": "attendance",
        "fields": [],
        "issues": [],
        "tables": [
            {
                "prefix": "work",
                "columns": ["date", "start", "end", "worked_minutes"],
                "header_quote": header,
                "rows": rows,
            }
        ],
    }
    return response, page


def test_month_of_rows_keeps_source_dates_and_verifiable_heading_units():
    response, page = month_table()
    verify_response(response, [page], "attendance")
    result, reasons, _ = expand_tables_for_review(response, [page])
    assert not reasons
    assert len(result["fields"]) == 31 * 4
    assert result["fields"][-1]["field_id"] == "work_31_worked_minutes"
    assert result["fields"][-1]["normalized_value"] == "480"
    assert result["fields"][0]["normalized_value"] == "10/01"
    response["tables"][0]["header_quote"] = ""
    checked = verify_response(response, [page], "attendance")
    durations = [field for field in checked.fields if field.field_id.endswith("_worked_minutes")]
    assert all(
        field.normalized_value is None and field.validation_status == "needs_review" for field in durations
    )


@pytest.mark.parametrize("damage", ["header", "cells", "columns", "page"])
def test_table_adapter_rejects_unverifiable_or_misaligned_rows(damage):
    response, page = month_table()
    table = response["tables"][0]
    if damage == "header":
        table["header_quote"] = "invented heading"
    elif damage == "cells":
        table["rows"][0]["raw_values"].pop()
    elif damage == "columns":
        table["columns"][0] = "not_a_column"
    else:
        table["rows"][0]["page"] = 2
    if damage == "columns":
        with pytest.raises(ModelFormatError):
            verify_response(response, [page], "attendance")
    else:
        checked = verify_response(response, [page], "attendance")
        assert all(
            field.validation_status == "needs_review" and field.normalized_value is None
            for field in checked.fields[:4]
        )
        assert "EXTRACTION_INCOMPLETE" in checked.issues


def earning_table_response(rows):
    return {
        "document_type": "payslip",
        "fields": [],
        "issues": [],
        "tables": [
            {
                "prefix": "earning",
                "columns": ["label", "amount"],
                "header_quote": "Item Amount",
                "rows": [
                    {
                        "page": 1,
                        "quote": quote,
                        "raw_values": [label, amount],
                        "normalized_values": [label, amount],
                        "issues": [],
                    }
                    for quote, label, amount in rows
                ],
            }
        ],
    }


@pytest.mark.parametrize("amount", ["3000000", "100"])
def test_table_cell_must_appear_on_its_own_row_before_header_expansion(amount):
    response = earning_table_response([("Base 3000000", "Base", "3000000"), ("Meal 100000", "Meal", amount)])
    checked = verify_response(response, ["Item Amount\nBase 3000000\nMeal 100000"], "payslip")
    fields = {item.field_id: item for item in checked.fields}
    item = fields["earning_2_amount"]
    assert item.normalized_value is None and item.suggested_value == amount
    assert "TABLE_CELL_SOURCE_MISMATCH" in item.validation_reasons
    assert not item.evidence_valid
    assert fields["earning_1_amount"].normalized_value == "3000000"
    assert fields["earning_2_label"].normalized_value == "Meal"


@pytest.mark.parametrize(
    "quote,raw,normalized,valid",
    [
        ("Meal:100000", "100000", "100000", True),
        ("Meal -100000", "-100000", "-100000", True),
        ("Meal -100000", "100000", "100000", False),
        ("Meal -100000", "-100", "-100", False),
        ("Meal 100000원", "100원", "100", False),
    ],
)
def test_table_scalar_boundaries_preserve_signs_and_allow_label_colons(quote, raw, normalized, valid):
    response = earning_table_response([(quote, "Meal", raw)])
    response["tables"][0]["rows"][0]["normalized_values"][1] = normalized
    page = "Item Amount\n" + quote
    if valid:
        verify_response(response, [page], "payslip")
    else:
        item = verify_response(response, [page], "payslip").fields[1]
        assert item.normalized_value is None and item.suggested_value == normalized
        assert "TABLE_CELL_SOURCE_MISMATCH" in item.validation_reasons


@pytest.mark.parametrize("split_tables", [False, True])
def test_model_cannot_reuse_one_source_row_with_new_row_ids(split_tables):
    response = earning_table_response([("Base 3000000", "Base", "3000000")] * 2)
    if split_tables:
        second = copy.deepcopy(response["tables"][0])
        second["rows"] = [second["rows"].pop()]
        response["tables"][0]["rows"].pop()
        response["tables"].append(second)
    checked = verify_response(response, ["Item Amount\nBase 3000000"], "payslip")
    assert checked.fields[1].normalized_value == "3000000"
    assert all(
        item.normalized_value is None and "TABLE_ROW_SOURCE_REUSED" in item.validation_reasons
        for item in checked.fields[2:]
    )
    assert "EXTRACTION_INCOMPLETE" in checked.issues


def test_identical_rows_are_preserved_up_to_real_source_occurrence_count():
    response = earning_table_response([("Base 3000000", "Base", "3000000")] * 2)
    page = "Item Amount\nBase 3000000\nBase 3000000"
    verify_response(response, [page], "payslip")
    flat, reasons, _ = expand_tables_for_review(response, [page])
    assert not reasons
    amounts = [f for f in flat["fields"] if f["field_id"].endswith("_amount")]
    assert len(amounts) == 2 and all(f["normalized_value"] == "3000000" for f in amounts)
    assert len(amounts[1]["quote"]) > len(amounts[0]["quote"])
    response["tables"][0]["rows"].append(copy.deepcopy(response["tables"][0]["rows"][0]))
    checked = verify_response(response, [page], "payslip")
    assert all(item.validation_status == "valid" for item in checked.fields[:4])
    assert all(
        item.normalized_value is None and "TABLE_ROW_SOURCE_REUSED" in item.validation_reasons
        for item in checked.fields[4:]
    )


def test_identical_source_rows_on_separate_pages_are_preserved():
    response = earning_table_response([("Base 3000000", "Base", "3000000")] * 2)
    response["tables"][0]["rows"][1]["page"] = 2
    pages = ["Item Amount\nBase 3000000"] * 2
    verify_response(response, pages, "payslip")
    flat, reasons, _ = expand_tables_for_review(response, pages)
    assert not reasons
    assert [f["page"] for f in flat["fields"] if f["field_id"].endswith("_amount")] == [1, 2]


def test_image_rows_have_only_quote_consistency_not_text_occurrence_verification():
    response = earning_table_response([("Base 3000000", "Base", "3000000")] * 2)
    # A scanned page provides no reliable text occurrence count; confirmation remains required.
    verify_response(response, [""], "payslip", [True])
    response["tables"][0]["rows"][1]["raw_values"][1] = "100000"
    item = verify_response(response, [""], "payslip", [True]).fields[3]
    assert item.normalized_value is None and "TABLE_CELL_SOURCE_MISMATCH" in item.validation_reasons


@pytest.mark.parametrize("copies_in_source", [1, 2])
def test_legacy_flat_rows_also_limit_identical_quote_reuse(copies_in_source):
    response = {
        "document_type": "payslip",
        "fields": [
            {
                "field_id": f"earning_{row_number}_{key}",
                "raw_value": value,
                "normalized_value": value,
                "unit": unit,
                "page": 1,
                "quote": "Base 3000000",
                "issues": [],
            }
            for row_number in (1, 2)
            for key, value, unit in (("label", "Base", "text"), ("amount", "3000000", "KRW"))
        ],
        "issues": [],
    }
    page = "\n".join(["Base 3000000"] * copies_in_source)
    if copies_in_source == 1:
        checked = verify_response(response, [page], "payslip")
        assert all(item.validation_status == "valid" for item in checked.fields[:2])
        assert all(
            item.normalized_value is None and "TABLE_ROW_SOURCE_REUSED" in item.validation_reasons
            for item in checked.fields[2:]
        )
    else:
        verify_response(response, [page], "payslip")


def test_expanded_rows_keep_valid_overlapping_header_context():
    response = earning_table_response([("Base 3000000", "Base", "3000000")] * 2)
    page = "Item Amount\nBase 3000000\nBase 3000000"
    flat, reasons, _ = expand_tables_for_review(response, [page])
    assert not reasons
    verify_response(flat, [page], "payslip")


def recorded_attendance():
    from pathlib import Path

    return json.loads(
        (Path(__file__).parent / "fixtures/attendance_real_response.json").read_text(encoding="utf-8")
    )


def test_actual_failed_attendance_response_all_31_days_passes_with_source_spelling():
    saved = recorded_attendance()
    verify_response(saved["response"], saved["pages"], "attendance", saved["image_pages"])
    flat, reasons, _ = expand_tables_for_review(saved["response"], saved["pages"], saved["image_pages"])
    assert not reasons
    assert len(flat["fields"]) == 317
    daily = [f for f in flat["fields"] if f["field_id"].endswith("_worked_minutes")]
    assert len(daily) == 31
    assert sum(int(f["normalized_value"]) for f in daily) == 9600
    assert daily[0]["raw_value"] == "8" and daily[0]["normalized_value"] == "480"
    assert "일별 근무 내역" in daily[0]["quote"]
    assert saved["response"]["tables"][0]["rows"][0]["raw_values"][5] == "8시간(h)"


@pytest.mark.parametrize(
    "damage", ["invented_number", "wrong_value", "wrong_alias", "invented_header", "reordered_header"]
)
def test_actual_attendance_still_rejects_unproven_model_changes(damage):
    saved = recorded_attendance()
    table = saved["response"]["tables"][0]
    row = table["rows"][0]
    if damage == "invented_number":
        row["raw_values"][5], row["normalized_values"][5] = "9시간(h)", "540"
    elif damage == "wrong_value":
        row["normalized_values"][5] = "8"
    elif damage == "wrong_alias":
        row["raw_values"][5] = "8시간(min)"
    elif damage == "invented_header":
        table["header_quote"] += "\n존재하지 않는 열"
    else:
        table["header_quote"] = "\n".join(reversed(table["header_quote"].splitlines()))
    checked = verify_response(saved["response"], saved["pages"], "attendance", saved["image_pages"])
    item = next(field for field in checked.fields if field.field_id == "work_1_worked_minutes")
    assert item.validation_status == "needs_review" and item.normalized_value is None
    assert "EXTRACTION_INCOMPLETE" in checked.issues


def test_adjacent_note_cells_keep_real_source_separators():
    saved = recorded_attendance()
    row = saved["response"]["tables"][0]["rows"][0]
    row["raw_values"][-1] = "목, 정상근무"
    row["normalized_values"][-1] = "목, 정상근무"
    verify_response(saved["response"], saved["pages"], "attendance", saved["image_pages"])
    flat, reasons, _ = expand_tables_for_review(saved["response"], saved["pages"], saved["image_pages"])
    assert not reasons
    note = next(f for f in flat["fields"] if f["field_id"] == "work_1_note")
    assert note["raw_value"] == note["normalized_value"] == "목 정상근무"


async def test_classification_budget_is_saved_without_a_corpus_usage_cache(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path / "state", provider=DocumentProvider())
    case = await store.upload(example_pdf("attendance"), "attendance")
    job = await store.create_job(case["id"], "extract")
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "completed"
    assert store.ports[job["id"]].checkpoints[job["id"]].call_budget == {"extract_candidates": 1}
    assert store.jobs[job["id"]]["usage"] == {}
    usage = json.loads(
        (tmp_path / "state" / "usage" / ("interactive-" + job["id"] + ".json")).read_text(encoding="utf-8")
    )
    assert usage["model_calls"] == 1
    assert usage["usage"] == {}
    assert usage["call_budget"] == {"extract_candidates": 1}
