import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import Confirmation, LocalPorts, LocalStore
from agent.dev.test_server import LocalHTTPServer
from agent.schemas.public.errors import ExecutionRevoked
from agent.services.hashing import content_hash
from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import pdf_bytes


async def test_upload_validation_opaque_ids_and_source_hash(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path)
    for invalid in (b"not a pdf", b"%PDF-malformed"):
        with pytest.raises(ValueError):
            await store.upload(invalid)
    case = await store.upload(pdf_bytes(["hourly_rate: 15000 KRW/hour"]))
    assert case["document_id"].startswith("upload-") and "pdf" not in store.documents[case["document_id"]]
    data = store.document_data(case["document_id"])
    assert content_hash(data) == store.documents[case["document_id"]]["sha256"]
    store2 = LocalStore(AgentSettings(), state=tmp_path)
    assert store2.document_data(case["document_id"]) == data


async def test_units_period_zero_missing_are_not_silently_assumed(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path)
    case = await store.upload(pdf_bytes(["contract_rate: 15000 KRW/hour"]), "contract")
    cid = case["id"]
    await run_stage(store, cid, "extract")
    case = await store.get_case(cid)
    draft = {
        **case["draft"],
        "values": {"contract_rate": "15000", "contract_start": "2020-01-01", "contract_end": "2020-12-31"},
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
        "manual": {"slip_rate": "15000", "deduction": "0"},
        "slip_unit": "KRW/month",
    }
    await store.save_draft(cid, draft)
    await store.confirm(cid)
    await run_stage(store, cid, "analyze")
    comparison = next(c for c in store.cases[cid]["analysis"]["comparisons"] if c["rule_id"] == "R-01")
    assert comparison["status"] == "needs_review"
    assert "UNIT_MISMATCH" in comparison["reason_codes"] and "PERIOD_MISMATCH" in comparison["reason_codes"]
    confirmation = Confirmation.model_validate({**draft, "slip_unit": ""})
    with pytest.raises(ValueError):
        confirmation.validate_for_analysis()


class BlockingProvider(FixtureProvider):
    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def invoke(self, operation, data):
        self.entered.set()
        await self.release.wait()
        return await super().invoke(operation, data)


async def test_cancellation_restore_reserved_budget_and_reject_old_execution(tmp_path):
    provider = BlockingProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    case = await store.upload(pdf_bytes(["contract_rate: 15000 KRW/hour"]), "contract")
    job = await store.create_job(case["id"], "extract")
    await asyncio.wait_for(provider.entered.wait(), 3)
    jid = job["id"]
    old_ports = store.ports[jid]
    old_task = store.jobs[jid]["task"]
    old_token = store.current_tokens[jid]
    await store.cancel_job(jid)
    assert store.jobs[jid]["status"] == "cancelled"
    cp = old_ports.checkpoints[jid]
    assert cp.call_budget["extract_candidates"] == 1
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    provider.release.set()
    await restored.resume_job(jid)
    await restored.tasks[jid]
    assert restored.jobs[jid]["status"] == "completed"
    assert restored.ports[jid].checkpoints[jid].call_budget["extract_candidates"] == 2
    # All LocalPorts in one server share the execution-token fence.
    from agent.schemas.public.tasks import JobContext

    old_context = JobContext.model_validate(old_task["context"])
    old_context.execution_token = old_token
    newer = LocalPorts(store, store.jobs[jid])
    newer_context = old_context.model_copy(deep=True)
    newer_context.execution_token = "newer-execution"
    newer.bind(newer_context)
    old_ports.cancelled.discard(jid)
    with pytest.raises(ExecutionRevoked):
        await old_ports.check_control(old_context)


async def test_edit_during_execution_rejects_old_result(tmp_path):
    provider = BlockingProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    case = await store.upload(pdf_bytes(["contract_rate: 15000 KRW/hour"]), "contract")
    job = await store.create_job(case["id"], "extract")
    await asyncio.wait_for(provider.entered.wait(), 3)
    await store.save_draft(case["id"], case["draft"])
    provider.release.set()
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "superseded"
    assert store.cases[case["id"]]["extraction"] is None


async def test_corrupt_local_source_becomes_failed_job_not_forever_pending(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path)
    case = await store.upload(pdf_bytes(["SYNTHETIC"]))
    cid = case["id"]
    (store.state / "uploads" / (case["document_id"] + ".pdf")).write_bytes(b"corrupt")
    job = await store.create_job(cid, "ocr")
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "failed"


async def run_stage(store, case_id, stage):
    job = await store.create_job(case_id, stage)
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "completed", store.jobs[job["id"]]
    return store.jobs[job["id"]]


def test_http_api_keeps_uploads_private_and_rejects_cross_origin_requests(tmp_path):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def initialize():
        store = LocalStore(AgentSettings(), state=tmp_path)
        case = await store.upload(pdf_bytes(["contract_rate: 15000 KRW/hour"]), "contract")
        return store, case

    store, case = asyncio.run_coroutine_threadsafe(initialize(), loop).result(timeout=5)
    server = LocalHTTPServer(("127.0.0.1", 0), store, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, *, headers=None, data=None):
        return urllib.request.urlopen(
            urllib.request.Request(base + path, data=data, headers={"X-Local-Agent": "1", **(headers or {})}),
            timeout=5,
        )

    try:
        with request("/api/documents") as response:
            documents = json.load(response)["documents"]
            assert len(documents) == 1 and documents[0]["id"] == case["document_id"]
            assert not any("source" in document or "pdf" in document for document in documents)
        with request(f"/api/documents/{case['document_id']}/pdf") as response:
            assert response.headers["Content-Type"] == "application/pdf"
            assert response.read().startswith(b"%PDF")
        for path, headers, data, status in (
            ("/api/cases", {"Origin": "https://other.example"}, b"{}", 403),
            ("/agent/.env", {}, None, 404),
            ("/api/evaluation", {}, None, 404),
            ("/api/health", {"Host": "attacker.example"}, None, 403),
        ):
            with pytest.raises(urllib.error.HTTPError) as error:
                request(path, headers=headers, data=data)
            assert error.value.code == status
    finally:
        server.shutdown()
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
