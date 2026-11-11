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
from agent.dev.local_workflow import ComparisonInputError, LocalPorts, LocalStore, extraction_draft
from agent.dev.test_server import LocalHTTPServer
from agent.tests.support.comparison_documents import (
    NoModelCalls,
    analyzed,
    create_bundle,
    reviewed,
    seed_documents,
    selection,
)


async def test_independent_comparison_cancel_resume_report_and_source_invalidation(tmp_path, monkeypatch):
    provider = NoModelCalls()
    entered, release = asyncio.Event(), asyncio.Event()
    original_progress = LocalPorts.emit_progress

    async def pause_template(ports, context, event):
        await original_progress(ports, context, event)
        if context.task_kind == "explain_result" and event.node_id == "compose" and event.status == "running":
            entered.set()
            await release.wait()

    monkeypatch.setattr(LocalPorts, "emit_progress", pause_template)
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    cases = await seed_documents(store)
    before = copy.deepcopy(store.cases)
    bundle = await create_bundle(store, cases)
    cid = bundle["id"]
    await reviewed(store, cid)
    current, _ = await analyzed(store, cid)
    snapshot = current["analysis_snapshot"]
    document_refs = [item for item in snapshot["authorized_sources"] if item["kind"] == "document"]
    assert {item["document_id"] for item in document_refs} == {
        store.cases[cases[kind]]["document_id"] for kind in ("attendance", "payslip", "deposit")
    }
    assert current["document_id"] is None
    job = await store.create_job(cid, "explain")
    await asyncio.wait_for(entered.wait(), 5)
    await store.cancel_job(job["id"])
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    release.set()
    await restored.resume_job(job["id"])
    await restored.tasks[job["id"]]
    assert restored.jobs[job["id"]]["status"] == "completed"
    report_job = await restored.create_job(cid, "report", include_original=True)
    await restored.tasks[report_job["id"]]
    assert restored.jobs[report_job["id"]]["status"] == "completed"
    current = await restored.get_comparison(cid)
    artifact = next(item for item in current["report"]["artifacts"] if item["mime_type"] == "application/zip")
    data, _ = await restored.artifact(artifact["artifact_id"])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        originals = [name for name in archive.namelist() if name.startswith("originals/")]
        assert len(originals) == 3
    assert restored.cases == before
    restored.cases[cases["payslip"]]["extraction_revision"] += 1
    restored.save_case(restored.cases[cases["payslip"]])
    assert (await restored.artifact(artifact["artifact_id"]))[0] == data
    assert (await restored.get_comparison(cid))["materials_invalid"] is True
    with pytest.raises(ValueError):
        await restored.create_job(cid, "analyze")


async def test_comparison_rejects_document_jobs_and_failed_creation_is_atomic(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=NoModelCalls())
    bundle = await store.create_comparison("새 묶음")
    for stage in ("ocr", "extract"):
        with pytest.raises(ComparisonInputError, match="OCR"):
            await store.create_job(bundle["id"], stage)
    assert not store.jobs
    with pytest.raises((ComparisonInputError, KeyError)):
        await store.create_comparison("잘못된 자료", selection={"payslip_case_id": "case-absent"})
    assert len(store.comparisons) == 1
    assert len(list((tmp_path / "comparisons").glob("*.json"))) == 1
    assert not store.cases


async def test_period_alternatives_count_maximum_once_but_cross_period_sum_is_bounded(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=NoModelCalls())
    cases = await seed_documents(store)

    async def allocate(month, amount):
        bundle = await store.create_comparison(
            f"{month}월 대안",
            period_start=f"2026-{month:02d}-01",
            period_end=f"2026-{month:02d}-28",
            selection={"deposit_case_ids": [cases["deposit"]]},
        )
        draft = bundle["draft"]
        draft["subjects_confirmed"] = True
        transaction = next(iter(draft["material_review"]["transactions"].values()))
        transaction.update(selected=True, amount=amount)
        await store.save_draft(bundle["id"], draft)
        return await store.confirm(bundle["id"])

    await allocate(9, "600000")
    await allocate(9, "900000")
    await allocate(10, "1100000")
    with pytest.raises(ComparisonInputError, match="입금액을 초과"):
        await allocate(10, "1100001")
    with pytest.raises(ComparisonInputError, match="입금액을 초과"):
        await allocate(11, "1")


def test_actual_minutes_under_review_do_not_fall_back_to_legacy_minutes():
    draft = extraction_draft(
        {
            "document_type": "payslip",
            "fields": [
                {"field_id": "slip_minutes", "normalized_value": "9600", "validation_status": "valid"},
                {
                    "field_id": "slip_actual_minutes",
                    "normalized_value": None,
                    "validation_status": "needs_review",
                },
            ],
        }
    )
    assert draft["manual"]["slip_minutes"] is None
    assert draft["slip_time_kind"] == ""


async def test_identity_refresh_keeps_only_actual_manual_changes(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path, provider=NoModelCalls())
    cases = await seed_documents(store)
    bundle = await create_bundle(store, cases)
    source_id = cases["payslip"]
    source = next(item for item in bundle["materials"]["sources"] if item["case_id"] == source_id)
    draft = bundle["draft"]
    draft["source_identity"] = {
        source_id: {
            **{key: value or "" for key, value in source["identity"].items()},
            "employer_name": "사용자가 확인한 사업장",
        }
    }
    draft["subjects_confirmed"] = True
    await store.save_draft(bundle["id"], draft)
    saved = store.cases[source_id]
    employee = next(item for item in saved["extraction"]["fields"] if item["field_id"] == "employee_name")
    employee["normalized_value"] = "새 원문 성명"
    saved["extraction_revision"] += 1
    store.save_case(saved)
    refreshed = await store.load_materials(bundle["id"], selection(cases))
    identity = refreshed["draft"]["source_identity"][source_id]
    assert identity == {"employee_name": "", "employee_id": "", "employer_name": "사용자가 확인한 사업장"}
    assert refreshed["draft"]["subjects_confirmed"] is False
    assert (
        next(item for item in refreshed["materials"]["sources"] if item["case_id"] == source_id)["identity"][
            "employee_name"
        ]
        == "새 원문 성명"
    )


def test_comparison_http_lifecycle_has_separate_routes_and_no_document_anchor(tmp_path):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def initialize():
        store = LocalStore(AgentSettings(), state=tmp_path, provider=NoModelCalls())
        return store, await seed_documents(store)

    store, cases = asyncio.run_coroutine_threadsafe(initialize(), loop).result(timeout=5)
    server = LocalHTTPServer(("127.0.0.1", 0), store, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, method="GET", data=None):
        req = urllib.request.Request(
            base + path,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"X-Local-Agent": "1", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)

    try:
        status, bundle = request(
            "/api/comparisons", "POST", {"title": "HTTP 비교", "selection": selection(cases)}
        )
        assert status == 201 and bundle["kind"] == "comparison" and bundle["document_id"] is None
        cid = bundle["id"]
        assert request("/api/comparisons")[1]["comparisons"][0]["id"] == cid
        assert request(f"/api/comparisons/{cid}")[1]["compatibility"]["blocked"] is False
        draft = bundle["draft"]
        draft.update(subjects_confirmed=True, worked_time_kind="actual", slip_time_kind="actual")
        request(f"/api/comparisons/{cid}/draft", "PUT", draft)
        assert request(f"/api/comparisons/{cid}/confirm", "POST", {})[1]["dirty"] is False
        status, job = request(f"/api/comparisons/{cid}/jobs", "POST", {"stage": "analyze"})
        assert status == 202

        async def completed():
            await store.tasks[job["id"]]

        server.call(completed())
        assert request(f"/api/jobs/{job['id']}")[1]["status"] == "completed"
        for path, method, body, expected in (
            (f"/api/cases/{cid}", "GET", None, 404),
            (f"/api/comparisons/{cases['payslip']}/draft", "PUT", draft, 404),
            (f"/api/comparisons/{cid}/jobs", "POST", {"stage": "extract"}, 400),
            (f"/api/comparisons/{cid}/document-type", "PUT", {"document_type": "payslip"}, 404),
        ):
            with pytest.raises(urllib.error.HTTPError) as error:
                request(path, method, body)
            assert error.value.code == expected
        refreshed = request(f"/api/comparisons/{cid}/materials", "PUT", selection(cases))[1]
        assert refreshed["draft"]["subjects_confirmed"] is False
        with urllib.request.urlopen(base + "/compare.js", timeout=5) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
