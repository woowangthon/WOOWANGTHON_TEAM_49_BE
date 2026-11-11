import asyncio
import copy
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import ComparisonInputError, LocalStore
from agent.dev.test_server import LocalHTTPServer
from agent.tests.fixtures_support import pdf_bytes


def response(kind):
    entries = {
        "contract": [
            ("contract_rate", "12000", "12000", "KRW/hour"),
            ("contract_start", "2026-01-01", "2026-01-01", "date"),
            ("contract_end", "2026-12-31", "2026-12-31", "date"),
        ],
        "attendance": [
            ("period_start", "2026-10-01", "2026-10-01", "date"),
            ("period_end", "2026-10-31", "2026-10-31", "date"),
            ("worked_minutes", "480 minutes", "480", "minute"),
        ],
        "payslip": [
            ("period_start", "2026-10-01", "2026-10-01", "date"),
            ("period_end", "2026-10-31", "2026-10-31", "date"),
            ("slip_rate", "14000", "14000", "KRW/hour"),
            ("slip_minutes", "450 minutes", "450", "minute"),
            ("gross", "112000", "112000", "KRW"),
            ("deduction", "12000", "12000", "KRW"),
            ("net", "100000", "100000", "KRW"),
            ("earning_1_label", "Basic pay", "Basic pay", "text"),
            ("earning_1_amount", "90000", "90000", "KRW"),
            ("earning_2_label", "Meal allowance", "Meal allowance", "text"),
            ("earning_2_amount", "22000", "22000", "KRW"),
            ("deduction_1_label", "Income tax", "Income tax", "text"),
            ("deduction_1_amount", "12000", "12000", "KRW"),
        ],
        "deposit": [
            ("account_number", "123-456", "123-456", "text"),
            ("transaction_1_date", "2026-11-05", "2026-11-05", "date"),
            ("transaction_1_sender", "Company", "Company", "text"),
            ("transaction_1_deposit_amount", "50000", "50000", "KRW"),
            ("transaction_1_balance", "9999999", "9999999", "KRW"),
            ("transaction_1_reference", "BANK-A", "BANK-A", "text"),
            ("transaction_2_date", "2026-11-06", "2026-11-06", "date"),
            ("transaction_2_deposit_amount", "49000", "49000", "KRW"),
            ("transaction_2_reference", "BANK-B", "BANK-B", "text"),
        ],
    }[kind]
    return {
        "document_type": kind,
        "issues": [],
        "fields": [
            {
                "field_id": fid,
                "raw_value": raw,
                "normalized_value": normalized,
                "unit": unit,
                "page": 1,
                "quote": f"{fid}: {raw}",
                "issues": [],
            }
            for fid, raw, normalized, unit in entries
        ],
    }


class SavedDocumentProvider:
    def __init__(self):
        self.calls = []
        self.overrides = {}

    async def invoke(self, operation, data):
        assert operation == "extract", "Comparison must run locally without a model"
        self.calls.append(data["document_type"])
        return copy.deepcopy(self.overrides.get(data["document_type"], response(data["document_type"]))), {}


async def stage(store, cid, name):
    job = await store.create_job(cid, name)
    await store.tasks[job["id"]]
    result = await store.get_job(job["id"])
    assert result["status"] == "completed", result
    return result


async def four_documents(store):
    cases = {}
    for kind in ("contract", "attendance", "payslip", "deposit"):
        candidate = getattr(store.provider, "overrides", {}).get(kind, response(kind))
        data = pdf_bytes([kind, *(f["quote"] for f in candidate["fields"])])
        case = await store.upload(data, kind, filename=kind + ".pdf")
        await stage(store, case["id"], "extract")
        cases[kind] = case["id"]
    return cases


def selection(cases):
    return {
        "contract_case_id": cases.get("contract"),
        "attendance_case_ids": [cases["attendance"]] if cases.get("attendance") else [],
        "payslip_case_id": cases.get("payslip"),
        "deposit_case_ids": [cases["deposit"]] if cases.get("deposit") else [],
    }


async def ready(store, cid, cases, allocation=None):
    case = await store.load_materials(cid, selection(cases))
    draft = case["draft"]
    draft.update(
        time_kind="actual", earnings_complete=True, deductions_complete=True, payment_scope_confirmed=True
    )
    # Legacy aggregate/manual inputs must not replace real detail rows or selected transactions.
    draft["manual"].update(earning_item="9", deduction_item="9", deposit="9")
    for item in draft["material_review"]["transactions"].values():
        item["selected"] = True
        if allocation is not None:
            item["amount"] = allocation
    await store.save_draft(cid, draft)
    return await store.confirm(cid)


async def test_all_four_documents_are_compared_with_original_evidence_and_no_new_model_call(tmp_path):
    provider = SavedDocumentProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    cases = await four_documents(store)
    cid = cases["contract"]  # The anchor may also be one of the linked source cases.
    prepared = await ready(store, cid, cases)
    assert not prepared["materials_invalid"]
    before = list(provider.calls)
    job = await stage(store, cid, "analyze")
    assert provider.calls == before and not job["usage"]
    saved = await store.get_case(cid)
    comparisons = {c["rule_id"]: c for c in saved["analysis"]["comparisons"]}
    assert comparisons["R-01"]["delta"] == "2000"
    assert comparisons["R-02"]["delta"] == "-30"
    assert [comparisons[r]["status"] for r in ("R-03", "R-04", "R-05")] == ["match"] * 3
    assert comparisons["R-06"]["rhs"] == "99000" and comparisons["R-06"]["delta"] == "1000"
    assert saved["analysis_period"] == {"start": "2026-10-01", "end": "2026-10-31"}
    evidence = {e["evidence_id"]: e for e in saved["analysis"]["evidence_catalog"]}
    source_ids = {store.document_ref(store.cases[c]["document_id"]).source_id for c in cases.values()}
    assert source_ids <= {e["source_id"] for e in evidence.values()}
    assert any(evidence[ref]["source_id"] in source_ids for ref in comparisons["R-06"]["evidence_refs"])
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    assert (await restored.get_case(cid))["analysis"] == saved["analysis"]
    assert not (await restored.get_case(cid))["materials_invalid"]


async def test_loading_materials_pins_a_persisted_revision_after_previous_jobs(tmp_path):
    provider = SavedDocumentProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    cases = await four_documents(store)
    cid = cases["attendance"]
    previous_job = store.jobs[store.cases[cid]["jobs"][-1]]
    before = list(provider.calls)
    loaded = await store.load_materials(cid, selection(cases))
    assert loaded["materials_revision"] == loaded["revision"] > previous_job["revision"]
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    assert (await restored.get_case(cid))["materials_revision"] == loaded["materials_revision"]
    cleared = await store.load_materials(cid, {})
    assert cleared["materials"] is None
    assert cleared["materials_revision"] == cleared["revision"] > loaded["materials_revision"]
    assert provider.calls == before


async def test_result_history_keeps_each_runs_documents_inputs_and_period(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
    cases = await four_documents(store)
    cid = cases["contract"]
    await ready(store, cid, cases)
    await stage(store, cid, "analyze")
    first_id = store.cases[cid]["analysis"]["result_id"]
    draft = copy.deepcopy(store.cases[cid]["draft"])
    draft["manual"]["net"] = "99000"
    await store.save_draft(cid, draft)
    await store.confirm(cid)
    await stage(store, cid, "analyze")
    case = store.cases[cid]
    assert len(case["analysis_history"]) == 2
    first, latest = case["analysis_history"]
    assert first["analysis"]["result_id"] == first_id
    assert first["analysis"]["comparisons"][5]["delta"] == "1000"
    assert latest["analysis"]["comparisons"][5]["delta"] == "0"
    assert first["period"] == {"start": "2026-10-01", "end": "2026-10-31"}
    assert len(first["materials"]["sources"]) == 4
    original_title = first["materials"]["sources"][0]["title"]
    case["materials"]["sources"][0]["title"] = "Changed current selection name"
    assert first["materials"]["sources"][0]["title"] == original_title
    assert first["snapshot"]["confirmed_fields"] != latest["snapshot"]["confirmed_fields"]
    store.save_case(case)
    restored = LocalStore(AgentSettings(), state=tmp_path)
    assert (await restored.get_case(cid))["analysis_history"] == case["analysis_history"]


async def test_no_contract_or_attendance_still_runs_statement_and_payment_comparisons(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
    cases = await four_documents(store)
    cid = cases["payslip"]
    await ready(store, cid, {k: v for k, v in cases.items() if k in ("payslip", "deposit")})
    await stage(store, cid, "analyze")
    results = {c["rule_id"]: c for c in store.cases[cid]["analysis"]["comparisons"]}
    assert results["R-01"]["status"] == results["R-02"]["status"] == "insufficient"
    assert results["R-05"]["status"] == "match"
    assert results["R-06"]["status"] == "difference"


async def test_python_ocr_then_ai_payslip_flows_into_local_comparison(tmp_path, monkeypatch):
    from agent.services import local_ocr
    from agent.tests.support.payslip_ocr import extract_payslip

    lines = [
        "대상기간 2026-10-01 ~ 2026-10-31",
        "지급내역",
        "기본급 3,000,000",
        "소계 3,000,000",
        "공제내역",
        "소득세 100,000",
        "공제 소계 100,000",
        "총지급액 3,000,000",
        "공제합계 100,000",
        "실수령액 2,900,000",
    ]
    recognized = {"text": "\n".join(lines), "lines": [{"text": line, "confidence": 95} for line in lines]}

    class ClassifiedPayslip:
        def __init__(self):
            self.calls = []

        async def invoke(self, operation, data):
            assert operation == "extract", "Only extraction classification needs AI"
            assert data["pages"] == [recognized["text"]]
            assert "pdf_bytes" not in data
            self.calls.append(operation)
            candidate = extract_payslip([recognized])
            candidate["document_type"] = "payslip"
            return candidate, {}

    monkeypatch.setattr(
        local_ocr,
        "recognize_pdf",
        lambda *args: [recognized],
    )
    provider = ClassifiedPayslip()
    store = LocalStore(AgentSettings(provider_mode="openai"), state=tmp_path, provider=provider)
    case = await store.upload(pdf_bytes(["Synthetic scan"], scanned=True), "payslip_ocr")
    await stage(store, case["id"], "extract")
    loaded = await store.load_materials(case["id"], {"payslip_case_id": case["id"]})
    assert [row["row_role"] for row in loaded["materials"]["earnings"]] == ["detail", "subtotal", "total"]
    assert [row["row_role"] for row in loaded["materials"]["deductions"]] == ["detail", "subtotal", "total"]
    draft = loaded["draft"]
    draft.update(time_kind="actual", earnings_complete=True, deductions_complete=True)
    await store.save_draft(case["id"], draft)
    await store.confirm(case["id"])
    job = await stage(store, case["id"], "analyze")
    results = store.cases[case["id"]]["analysis"]["comparisons"]
    assert [item["status"] for item in results[2:5]] == ["match"] * 3
    assert results[5]["status"] == "insufficient" and results[5]["rhs"] is None
    assert not job["usage"] and not job["call_budget"]
    assert provider.calls == ["extract"]


async def test_excluded_ocr_row_and_manual_missing_item_round_trip_into_comparison(tmp_path):
    provider = SavedDocumentProvider()
    candidate = response("payslip")
    next(f for f in candidate["fields"] if f["field_id"] == "earning_2_amount")["normalized_value"] = None
    provider.overrides["payslip"] = candidate
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    cases = await four_documents(store)
    cid = cases["payslip"]
    loaded = await store.load_materials(cid, selection({"payslip": cid}))
    draft = loaded["draft"]
    draft.update(time_kind="actual", earnings_complete=True, deductions_complete=True)
    draft["material_review"]["row_roles"] = {loaded["materials"]["earnings"][1]["item_id"]: "excluded"}
    draft["material_review"]["manual_rows"] = [
        {
            "row_id": "missing-meal",
            "group": "earnings",
            "label": "Meal allowance",
            "amount": "22000",
            "row_role": "detail",
            "page": 1,
        }
    ]
    await store.save_draft(cid, draft)
    await store.confirm(cid)
    before = list(provider.calls)
    await stage(store, cid, "analyze")
    analysis = store.cases[cid]["analysis"]
    earnings = next(c for c in analysis["comparisons"] if c["rule_id"] == "R-03")
    assert earnings["status"] == "match" and earnings["lhs"] == "112000"
    assert provider.calls == before
    evidence = {e["evidence_id"]: e for e in analysis["evidence_catalog"]}
    assert any(
        evidence[ref]["locator"] == "manual-row:missing-meal"
        and evidence[ref]["verification_method"] == "manual_confirmed"
        and evidence[ref]["page"] == 1
        for ref in earnings["evidence_refs"]
    )
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    assert (await restored.get_case(cid))["draft"]["material_review"]["manual_rows"] == (
        store.cases[cid]["draft"]["material_review"]["manual_rows"]
    )


async def test_explicit_source_period_mismatch_blocks_only_affected_comparisons(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
    cases = await four_documents(store)
    cid = cases["contract"]
    await ready(store, cid, cases)
    draft = copy.deepcopy(store.cases[cid]["draft"])
    draft["period_end"] = "2026-10-15"
    await store.save_draft(cid, draft)
    await store.confirm(cid)
    await stage(store, cid, "analyze")
    results = {c["rule_id"]: c for c in store.cases[cid]["analysis"]["comparisons"]}
    assert results["R-02"]["status"] == "needs_review"
    assert results["R-05"]["delta"] is None
    assert "PERIOD_MISMATCH" in results["R-05"]["reason_codes"]


async def test_reextraction_and_source_type_change_invalidate_dependent_results(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
    cases = await four_documents(store)
    cid = cases["contract"]
    await ready(store, cid, cases)
    await stage(store, cid, "analyze")
    # Same content still constitutes a new extraction candidate revision.
    await stage(store, cases["payslip"], "extract")
    dependent = await store.get_case(cid)
    assert dependent["materials_invalid"] and dependent["dirty"] and dependent["stale"]
    with pytest.raises(ComparisonInputError, match="변경"):
        await store.confirm(cid)
    with pytest.raises(ValueError):
        await store.create_job(cid, "analyze")
    await ready(store, cid, cases)
    await stage(store, cid, "analyze")
    await store.change_document_type(cases["deposit"], "attendance")
    assert (await store.get_case(cid))["materials_invalid"]


async def test_bank_allocation_cannot_exceed_one_transaction_across_payroll_cases(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
    cases = await four_documents(store)
    await ready(store, cases["contract"], cases, allocation="30000")
    second = await store.create_case(store.cases[cases["contract"]]["document_id"])
    await store.load_materials(second["id"], selection(cases))
    draft = copy.deepcopy(store.cases[second["id"]]["draft"])
    draft.update(time_kind="actual", payment_scope_confirmed=True)
    for row in draft["material_review"]["transactions"].values():
        row.update(selected=True, amount="30000")
    await store.save_draft(second["id"], draft)
    with pytest.raises(ComparisonInputError, match="다른 대조 사례"):
        await store.confirm(second["id"])
    for row in draft["material_review"]["transactions"].values():
        row["amount"] = "10000"
    await store.save_draft(second["id"], draft)
    assert not (await store.confirm(second["id"]))["dirty"]


async def test_unselected_bank_transactions_do_not_become_zero_payment(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
    cases = await four_documents(store)
    cid = cases["payslip"]
    case = await store.load_materials(cid, selection({"payslip": cid, "deposit": cases["deposit"]}))
    draft = case["draft"]
    draft["time_kind"] = "actual"
    await store.save_draft(cid, draft)
    await store.confirm(cid)
    await stage(store, cid, "analyze")
    payment = next(c for c in store.cases[cid]["analysis"]["comparisons"] if c["rule_id"] == "R-06")
    assert payment["rhs"] is None and payment["delta"] is None
    assert payment["status"] == "insufficient"


async def extra_bank(store, provider, *, references=True):
    candidate = response("deposit")
    if not references:
        candidate["fields"] = [f for f in candidate["fields"] if not f["field_id"].endswith("_reference")]
    provider.overrides["deposit"] = candidate
    case = await store.upload(
        pdf_bytes(["Another bank PDF", *(f["quote"] for f in candidate["fields"])]), "deposit"
    )
    await stage(store, case["id"], "extract")
    return case["id"]


async def test_same_bank_reference_across_two_pdfs_can_be_selected_once(tmp_path):
    provider = SavedDocumentProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    cases = await four_documents(store)
    other = await extra_bank(store, provider)
    selected = selection(cases)
    selected["deposit_case_ids"].append(other)
    case = await store.load_materials(cases["contract"], selected)
    assert len(case["materials"]["transactions"]) == 2
    draft = case["draft"]
    draft.update(time_kind="actual", payment_scope_confirmed=True)
    for row in draft["material_review"]["transactions"].values():
        row["selected"] = True
    await store.save_draft(case["id"], draft)
    await store.confirm(case["id"])
    await stage(store, case["id"], "analyze")
    assert store.cases[case["id"]]["analysis"]["comparisons"][5]["rhs"] == "99000"


async def test_possible_duplicate_transfers_require_explicit_distinct_transaction_confirmation(tmp_path):
    provider = SavedDocumentProvider()
    provider.overrides["deposit"] = response("deposit")
    provider.overrides["deposit"]["fields"] = [
        f for f in provider.overrides["deposit"]["fields"] if not f["field_id"].endswith("_reference")
    ]
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    cases = await four_documents(store)
    other = await extra_bank(store, provider, references=False)
    selected = selection(cases)
    selected["deposit_case_ids"].append(other)
    case = await store.load_materials(cases["contract"], selected)
    assert all(row.get("duplicate_candidate_group") for row in case["materials"]["transactions"])
    draft = case["draft"]
    draft.update(time_kind="actual", payment_scope_confirmed=True)
    for row in draft["material_review"]["transactions"].values():
        row["selected"] = True
    await store.save_draft(case["id"], draft)
    with pytest.raises(ComparisonInputError, match="중복 후보"):
        await store.confirm(case["id"])
    draft["material_review"]["distinct_transactions_confirmed"] = True
    await store.save_draft(case["id"], draft)
    await store.confirm(case["id"])
    await stage(store, case["id"], "analyze")
    assert store.cases[case["id"]]["analysis"]["comparisons"][5]["rhs"] == "198000"


async def test_misrecognized_original_deposit_amount_can_be_corrected_with_manual_evidence(tmp_path):
    provider = SavedDocumentProvider()
    provider.overrides["deposit"] = response("deposit")
    value = next(
        f for f in provider.overrides["deposit"]["fields"] if f["field_id"] == "transaction_1_deposit_amount"
    )
    value["normalized_value"] = None
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    cases = await four_documents(store)
    case = await store.load_materials(cases["contract"], selection(cases))
    draft = case["draft"]
    draft.update(time_kind="actual", payment_scope_confirmed=True)
    first = case["materials"]["transactions"][0]
    for row in draft["material_review"]["transactions"].values():
        row["selected"] = True
    draft["material_review"]["transactions"][first["transaction_id"]].update(
        transaction_amount="50000", amount="50000"
    )
    await store.save_draft(case["id"], draft)
    await store.confirm(case["id"])
    job = await stage(store, case["id"], "analyze")
    assert store.cases[case["id"]]["analysis"]["comparisons"][5]["rhs"] == "99000"
    snapshot = store.jobs[job["id"]]["task"]["payload"]["snapshot"]
    manual_ids = {
        e["evidence_id"]
        for e in snapshot["evidence_catalog"]
        if e["verification_method"] == "manual_confirmed"
    }
    assert manual_ids & set(snapshot["transaction_allocations"][0]["evidence_refs"])


async def test_corrected_deposit_principal_rechecks_duplicate_candidates_before_confirmation(tmp_path):
    provider = SavedDocumentProvider()
    provider.overrides["deposit"] = response("deposit")
    provider.overrides["deposit"]["fields"] = [
        f for f in provider.overrides["deposit"]["fields"] if not f["field_id"].endswith("_reference")
    ]
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    cases = await four_documents(store)
    candidate = copy.deepcopy(provider.overrides["deposit"])
    item = next(f for f in candidate["fields"] if f["field_id"] == "transaction_1_deposit_amount")
    item.update(raw_value="5000", normalized_value="5000", quote="transaction_1_deposit_amount: 5000")
    provider.overrides["deposit"] = candidate
    extra = await store.upload(
        pdf_bytes(["OCR amount mistake", *(f["quote"] for f in candidate["fields"])]), "deposit"
    )
    await stage(store, extra["id"], "extract")
    selected = selection(cases)
    selected["deposit_case_ids"].append(extra["id"])
    case = await store.load_materials(cases["contract"], selected)
    draft = case["draft"]
    draft.update(time_kind="actual", payment_scope_confirmed=True)
    for transaction in case["materials"]["transactions"]:
        if transaction["date"] == "2026-11-05":
            assert not transaction.get("duplicate_candidate_group")
            draft["material_review"]["transactions"][transaction["transaction_id"]].update(
                selected=True, transaction_amount="50000", amount="50000"
            )
    await store.save_draft(case["id"], draft)
    with pytest.raises(ComparisonInputError, match="중복 후보"):
        await store.confirm(case["id"])
    draft["material_review"]["distinct_transactions_confirmed"] = True
    await store.save_draft(case["id"], draft)
    await store.confirm(case["id"])
    await stage(store, case["id"], "analyze")
    assert store.cases[case["id"]]["analysis"]["comparisons"][5]["rhs"] == "100000"


async def test_negative_tax_refund_is_preserved_in_statement_comparison(tmp_path):
    provider = SavedDocumentProvider()
    candidate = response("payslip")
    for item in candidate["fields"]:
        value = {"deduction": "-1000", "deduction_1_amount": "-1000", "net": "113000"}.get(item["field_id"])
        if value is not None:
            item.update(raw_value=value, normalized_value=value, quote=f"{item['field_id']}: {value}")
    provider.overrides["payslip"] = candidate
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    cases = await four_documents(store)
    await ready(store, cases["contract"], cases)
    await stage(store, cases["contract"], "analyze")
    comparisons = store.cases[cases["contract"]]["analysis"]["comparisons"]
    assert comparisons[3]["lhs"] == "-1000" and comparisons[3]["status"] == "match"
    assert comparisons[4]["status"] == "match"


async def test_linked_sources_continue_into_explanation_report_and_original_zip(tmp_path):
    from agent.tests.fixture_provider import FixtureProvider

    store = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
    cases = await four_documents(store)
    cid = cases["contract"]
    await ready(store, cid, cases)
    await stage(store, cid, "analyze")
    store.provider = FixtureProvider()
    await stage(store, cid, "explain")
    job = await store.create_job(cid, "report", include_original=True)
    await store.tasks[job["id"]]
    assert (await store.get_job(job["id"]))["status"] == "completed"
    report = store.cases[cid]["report"]
    assert len(report["included_source_ids"]) == 4
    artifact = next(a for a in report["artifacts"] if a["mime_type"] == "application/zip")
    data, _ = await store.artifact(artifact["artifact_id"])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert len([name for name in archive.namelist() if name.startswith("originals/")]) == 4


def test_http_link_confirm_and_analyze_saved_documents(tmp_path):
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    async def initialize():
        store = LocalStore(AgentSettings(), state=tmp_path, provider=SavedDocumentProvider())
        cases = await four_documents(store)
        return store, cases

    store, cases = asyncio.run_coroutine_threadsafe(initialize(), loop).result(timeout=10)
    server = LocalHTTPServer(("127.0.0.1", 0), store, loop)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        with urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}" + path,
                data=data,
                method=method,
                headers={"X-Local-Agent": "1", "Content-Type": "application/json"},
            ),
            timeout=5,
        ) as response:
            return json.load(response)

    try:
        cid = cases["contract"]
        assert len(request("/api/comparison-sources")["sources"]) == 4
        case = request(f"/api/cases/{cid}/materials", "PUT", selection(cases))
        assert len(case["materials"]["sources"]) == 4
        draft = case["draft"]
        draft.update(
            time_kind="actual", earnings_complete=True, deductions_complete=True, payment_scope_confirmed=True
        )
        for row in draft["material_review"]["transactions"].values():
            row["selected"] = True
        request(f"/api/cases/{cid}/draft", "PUT", draft)
        request(f"/api/cases/{cid}/confirm", "POST", {})
        job = request(f"/api/cases/{cid}/jobs", "POST", {"stage": "analyze"})

        async def completion():
            await store.tasks[job["id"]]

        asyncio.run_coroutine_threadsafe(completion(), loop).result(timeout=5)
        assert request(f"/api/jobs/{job['id']}")["status"] == "completed"
        assert request(f"/api/cases/{cid}")["analysis"]["comparisons"][5]["delta"] == "1000"
    finally:
        server.shutdown()
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        loop.close()
