"""Optional PDF candidates never replace A or user inputs before explicit review."""

import asyncio
import copy

import pytest

from agent.config import AgentSettings
from agent.dev import local_workflow
from agent.dev.candidate_selection import supplement_targets
from agent.dev.comparison_materials import build_materials
from agent.dev.local_workflow import ComparisonInputError, LocalStore
from agent.schemas.public.inputs import EvidenceRef
from agent.schemas.public.results import ExtractedField, ExtractionCandidate
from agent.schemas.public.tasks import AgentFailure, AgentOutcome
from agent.tests.fixtures_support import pdf_bytes


class NoModel:
    async def invoke(self, operation, data):
        pytest.fail("Workflow tests must not call a model")


def candidate(store, cid, *, supplementary=False, fields=None):
    case = store.cases[cid]
    definitions = fields or [
        ("gross", "100", "KRW", "valid"),
        ("net", "90", "KRW", "needs_review"),
        ("earning_1_label", "Basic pay", "text", "valid"),
        ("earning_1_amount", "100", "KRW", "valid"),
        ("deduction", "10", "KRW", "valid"),
        ("slip_actual_minutes", None, "minute", "missing"),
    ]
    ref = store.document_ref(case["document_id"])
    values, evidence = [], []
    for key, value, unit, status in definitions:
        eid = ("b-" if supplementary else "a-") + key
        quote = f"{key}: {value or 'not printed'}"
        evidence.append(
            EvidenceRef(
                evidence_id=eid,
                source_id=ref.source_id,
                page=1,
                quote=quote,
                verification_method="vision_unverified" if supplementary else "ocr_unverified",
            )
        )
        values.append(
            ExtractedField(
                field_id=key,
                raw_value=value,
                suggested_value=value,
                normalized_value=value if status == "valid" else None,
                validation_status=status,
                validation_reasons=[] if status == "valid" else ["SOURCE_VALUE_MISMATCH"],
                source_quote=quote,
                source_page=1,
                unit=unit,
                evidence_refs=[eid],
            )
        )
    return ExtractionCandidate(
        document_id=case["document_id"],
        document_version=1,
        document_type=case["document_type"],
        extraction_version="1.2",
        model_config_version="test",
        fields=values,
        evidence_catalog=evidence,
        issues=[],
        page_count=1,
        verification_method="vision_unverified" if supplementary else "ocr_unverified",
        source_method="pdf_supplement" if supplementary else "python_ocr",
        page_methods=["pdf_supplement" if supplementary else "python_ocr"],
    )


async def seeded(tmp_path, *, kind="payslip", fields=None):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=NoModel())
    case = await store.upload(pdf_bytes(["saved source for candidate review"]), kind)
    cid = case["id"]
    ref = store.document_ref(case["document_id"])
    case = store.cases[cid]
    case["ocr"] = {
        "id": "ocr-test",
        "document_id": ref.document_id,
        "document_version": 1,
        "content_hash": ref.content_hash,
        "pages": ["saved OCR"],
        "ocr_pages": [],
        "source_method": "python_ocr",
        "page_methods": ["python_ocr"],
    }
    payload = candidate(store, cid, fields=fields)
    store._apply(case, {"stage": "extract", "id": "job-A", "task": {"context": {"run_id": "run-A"}}}, payload)
    store.save_case(case)
    return store, cid


def output(store, cid, definitions=None):
    return candidate(store, cid, supplementary=True, fields=definitions or [("net", "90", "KRW", "valid")])


def fake_success(monkeypatch, payload, captured=None):
    async def execute(task, **options):
        if captured is not None:
            captured.append(task)
        return AgentOutcome(
            job_id=task.context.job_id,
            run_id=task.context.run_id,
            provider_mode="openai",
            status="completed",
            payload=payload,
            usage={},
        )

    monkeypatch.setattr(local_workflow, "run_task", execute)


async def finish_supplement(store, cid, field_ids=None):
    job = await store.create_job(
        cid,
        "supplement",
        field_ids=field_ids or ["net"],
        extraction_revision=store.cases[cid]["extraction_revision"],
    )
    await store.tasks[job["id"]]
    return await store.get_job(job["id"])


def choice(store, cid, key="net", candidate_id="manual", **options):
    return {
        "field_id": key,
        "candidate_id": candidate_id,
        "source_confirmed": True,
        "extraction_revision": store.cases[cid]["extraction_revision"],
        **options,
    }


async def test_supplement_keeps_a_effective_and_user_draft_unchanged(tmp_path, monkeypatch):
    store, cid = await seeded(tmp_path)
    store.cases[cid]["draft"]["manual"]["net"] = "85"
    before = copy.deepcopy(store.cases[cid])
    captured = []
    fake_success(monkeypatch, output(store, cid), captured)
    job = await finish_supplement(store, cid)
    assert job["status"] == "completed"
    task = captured[0]
    assert task.context.task_kind == "extract_document"
    assert task.payload.extraction_method == "pdf_supplement"
    assert task.payload.ocr_result_id == "ocr-test"
    assert [target.field_id for target in task.payload.target_fields] == ["net"]
    current = await store.get_case(cid)
    for key in (
        "extraction",
        "extraction_a",
        "extraction_revision",
        "extraction_a_revision",
        "draft",
        "revision",
    ):
        assert current[key] == before[key]
    assert current["supplements"][0]["payload"]["verification_method"] == "vision_unverified"
    assert current["supplements"][0]["id"] == job["id"]
    assert current["candidate_selections"] == {}


@pytest.mark.parametrize(
    "fields,revision_delta",
    [(["gross"], 0), (["unknown"], 0), ([], 0), (["net", "net"], 0), (["net"], -1), (["net"] * 51, 0)],
)
async def test_only_current_unresolved_a_fields_can_be_requested(tmp_path, fields, revision_delta):
    store, cid = await seeded(tmp_path)
    jobs = dict(store.jobs)
    with pytest.raises(ComparisonInputError):
        await store.create_job(
            cid,
            "supplement",
            field_ids=fields,
            extraction_revision=store.cases[cid]["extraction_revision"] + revision_delta,
        )
    assert store.jobs == jobs


async def test_table_target_context_is_generated_only_from_saved_a_siblings(tmp_path):
    store, cid = await seeded(
        tmp_path,
        kind="deposit",
        fields=[
            ("transaction_1_date", "2026-10-2", "date", "needs_review"),
            ("transaction_1_deposit_amount", "888000", "KRW", "valid"),
            ("transaction_1_reference", "BANK-ONE", "text", "valid"),
            ("transaction_2_date", "2026-10-24", "date", "valid"),
        ],
    )
    case = store.cases[cid]
    target = supplement_targets(case, ["transaction_1_date"], case["extraction_revision"])[0]
    assert target["page"] == 1
    assert target["row_context"]["transaction_1_deposit_amount.normalized_value"] == "888000"
    assert target["row_context"]["transaction_1_reference.raw_value"] == "BANK-ONE"
    assert target["row_context"]["transaction_1_reference"] == "BANK-ONE"
    assert "transaction_1_date" not in target["row_context"]
    assert not any(key.startswith("transaction_2") for key in target["row_context"])


@pytest.mark.parametrize("mode", ["failed", "cancelled"])
async def test_failed_or_cancelled_b_preserves_a_and_manual_input(tmp_path, monkeypatch, mode):
    store, cid = await seeded(tmp_path)
    case = store.cases[cid]
    case["draft"]["manual"]["net"] = "85"
    before = copy.deepcopy(case)
    entered = asyncio.Event()

    async def execute(task, **options):
        entered.set()
        if mode == "cancelled":
            await asyncio.Event().wait()
        return AgentOutcome(
            job_id=task.context.job_id,
            run_id=task.context.run_id,
            provider_mode="openai",
            status="failed",
            error=AgentFailure(code="TEST_FAILURE", message="test", retryable=False),
        )

    monkeypatch.setattr(local_workflow, "run_task", execute)
    job = await store.create_job(
        cid, "supplement", field_ids=["net"], extraction_revision=case["extraction_revision"]
    )
    await entered.wait()
    if mode == "cancelled":
        await store.cancel_job(job["id"])
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == mode
    for key in (
        "extraction",
        "extraction_a",
        "draft",
        "revision",
        "extraction_revision",
        "supplements",
        "candidate_selections",
    ):
        assert case[key] == before[key]


async def test_selecting_b_preserves_manual_conflict_invalidates_links_and_keeps_a_lineage(
    tmp_path, monkeypatch
):
    store, cid = await seeded(tmp_path)
    link = await store.create_comparison(selection={"payslip_case_id": cid})
    store.cases[cid]["draft"]["manual"]["net"] = "85"
    fake_success(
        monkeypatch,
        output(
            store, cid, [("net", "90", "KRW", "valid"), ("slip_actual_minutes", "9600", "minute", "valid")]
        ),
    )
    job = await finish_supplement(store, cid, ["net", "slip_actual_minutes"])
    assert not (await store.get_comparison(link["id"]))["materials_invalid"]
    original_a = copy.deepcopy(store.cases[cid]["extraction_a"])
    selected = await store.select_candidate(cid, choice(store, cid, candidate_id=job["id"] + ":net"))
    assert selected["draft"]["manual"]["net"] == "85"
    assert (
        next(field for field in selected["extraction"]["fields"] if field["field_id"] == "net")[
            "normalized_value"
        ]
        == "90"
    )
    assert selected["extraction_a"] == original_a
    assert (await store.get_comparison(link["id"]))["materials_invalid"]
    record = selected["candidate_selections"]["net"]
    assert record["job_id"] == job["id"] and record["run_id"]
    assert record["document_hash"] == store.documents[selected["document_id"]]["sha256"]
    assert record["source_quote"] and record["source_page"] == 1
    assert record["base_extraction_revision"] == selected["extraction_a_revision"]
    # A second field from the same B still belongs to the unchanged A lineage.
    selected = await store.select_candidate(
        cid, choice(store, cid, "slip_actual_minutes", job["id"] + ":slip_actual_minutes")
    )
    assert selected["draft"]["manual"]["slip_minutes"] == "9600"
    assert selected["draft"]["slip_time_kind"] == "actual"
    with pytest.raises(ComparisonInputError, match="이미 확인"):
        await finish_supplement(store, cid)
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=NoModel())
    assert (await restored.get_case(cid))["candidate_selections"] == selected["candidate_selections"]


async def test_manual_date_repair_unblocks_only_that_saved_transaction(tmp_path):
    store, cid = await seeded(
        tmp_path,
        kind="deposit",
        fields=[
            ("transaction_1_date", "2026-10-2", "date", "needs_review"),
            ("transaction_1_deposit_amount", "888000", "KRW", "valid"),
        ],
    )
    assert build_materials(store, {"deposit_case_ids": [cid]})["transactions"][0]["validation_warnings"]
    selected = await store.select_candidate(
        cid, choice(store, cid, "transaction_1_date", value="2026-10-23", unit="date")
    )
    row = build_materials(store, {"deposit_case_ids": [cid]})["transactions"][0]
    assert row["date"] == "2026-10-23" and row["validation_warnings"] == []
    assert selected["extraction_a"]["fields"][0]["normalized_value"] is None
    assert any(
        item["verification_method"] == "manual_confirmed"
        for item in selected["extraction"]["evidence_catalog"]
    )


@pytest.mark.parametrize(
    "request_update",
    [
        {"source_confirmed": False},
        {"candidate_id": "A"},
        {"candidate_id": "job-other:net"},
        {"field_id": "not-in-a"},
        {"unit": "minute"},
        {"value": "2,.888,000"},
        {"extraction_revision": 0},
    ],
)
async def test_selection_rejects_unconfirmed_unusable_wrong_scope_and_invalid_manual_values(
    tmp_path, request_update
):
    store, cid = await seeded(tmp_path)
    before = copy.deepcopy(store.cases[cid])
    request = choice(store, cid, value="90", unit="KRW")
    request.update(request_update)
    with pytest.raises(ComparisonInputError):
        await store.select_candidate(cid, request)
    assert store.cases[cid] == before


async def test_new_a_invalidates_current_b_choices_preserving_history_and_draft(tmp_path, monkeypatch):
    store, cid = await seeded(tmp_path)
    fake_success(monkeypatch, output(store, cid))
    job = await finish_supplement(store, cid)
    await store.select_candidate(cid, choice(store, cid, candidate_id=job["id"] + ":net"))
    case = store.cases[cid]
    case["draft"]["manual"]["net"] = "85"
    store._apply(case, {"stage": "extract", "id": "job-new-A"}, candidate(store, cid))
    assert case["supplements"] == [] and case["candidate_selections"] == {}
    assert case["draft"]["manual"]["net"] == "85"
    event = case["candidate_selection_history"][-1]
    assert event["event"] == "invalidated" and event["supplements"][0]["id"] == job["id"]
    with pytest.raises(ComparisonInputError, match="후보"):
        await store.select_candidate(cid, choice(store, cid, candidate_id=job["id"] + ":net"))


async def test_b_rejects_out_of_scope_response_and_resume_after_base_change(tmp_path, monkeypatch):
    store, cid = await seeded(tmp_path)
    fake_success(monkeypatch, output(store, cid, [("gross", "999", "KRW", "valid")]))
    before = copy.deepcopy(store.cases[cid]["extraction"])
    job = await finish_supplement(store, cid)
    assert job["status"] == "failed"
    assert store.cases[cid]["extraction"] == before and store.cases[cid]["supplements"] == []
    store.cases[cid]["extraction_a_revision"] += 1
    with pytest.raises(ComparisonInputError, match="기본 추출"):
        await store.resume_job(job["id"])


async def test_supplement_resume_keeps_job_and_fixes_run_provenance(tmp_path, monkeypatch):
    store, cid = await seeded(tmp_path)

    async def fail(task, **options):
        return AgentOutcome(
            job_id=task.context.job_id,
            run_id=task.context.run_id,
            provider_mode="openai",
            status="failed",
            error=AgentFailure(code="TEST_FAILURE", message="test", retryable=True),
        )

    monkeypatch.setattr(local_workflow, "run_task", fail)
    job = await finish_supplement(store, cid)
    old_run = store.jobs[job["id"]]["task"]["context"]["run_id"]
    fake_success(monkeypatch, output(store, cid))
    await store.resume_job(job["id"])
    await store.tasks[job["id"]]
    saved = store.cases[cid]["supplements"][0]
    assert saved["id"] == job["id"] and saved["run_id"] != old_run
    assert saved["run_id"] == store.jobs[job["id"]]["task"]["context"]["run_id"]
