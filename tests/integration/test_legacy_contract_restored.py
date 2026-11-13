"""Frontend contract regressions using real runner, SQLite, source grants and artifacts."""

import asyncio
import io
import zipfile

import pytest
from pypdf import PdfReader

from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import pdf_bytes
from app.compatibility import execute_next, money
from app.db import load
from app.routers.reports import progress_reports
from test_api_contract import api as api_fixture

api = api_fixture


def request(client, method, path, body=None, status=200, **kwargs):
    response = client.request(
        method, "/api/v1" + path, **({"json": body} if body is not None else {}), **kwargs
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if response.content else None


def drain(app):
    with app.state.db.connect(write=True) as con:
        con.execute("UPDATE jobs SET ready_at=0")
    while asyncio.run(execute_next(app.state)):
        pass


def snapshot_identity(ref):
    # Live fingerprint/job diagnostics can change while the immutable snapshot
    # and its per-check freshness stay unchanged.
    return {k: ref[k] for k in ('snapshotId', 'version', 'inputVersion', 'createdAt', 'freshness')}


def payslip(client, cid, **overrides):
    return request(
        client,
        "POST",
        f"/cases/{cid}/manual-records",
        {
            "recordType": "PAYSLIP",
            "data": {
                "grossPay": 2300000,
                "totalDeductions": 200000,
                "netPay": 2090000,
                "paymentItems": [2000000, 300000],
                "deductionItems": [190000],
                **overrides,
            },
        },
        201,
    )


def test_saved_manual_records_reach_real_agent_and_independent_views(api):
    c, app, wid, cid = api
    saved = payslip(c, cid)
    assert saved["recompute"]["scheduled"] is True
    drain(app)
    data = request(c, "GET", f"/cases/{cid}/results")
    assert data["payslipValidation"]["paymentTotal"]["status"] == "MATCH"
    assert data["payslipValidation"]["deductionTotal"]["difference"] == -10000
    assert data["payslipValidation"]["netPay"]["difference"] == 10000
    assert data["summary"]["counts"]["difference"] == 2
    refs = data["versions"]
    assert len({r["snapshotId"] for r in refs.values()}) == 4
    assert all(type(r["snapshotId"]) is int and r["freshness"] == "CURRENT" for r in refs.values())
    tx = request(
        c,
        "POST",
        f"/workplaces/{wid}/payment-transactions",
        {"paymentMethod": "CASH", "amount": 2090000, "transactionDate": "2026-11-01", "source": "USER_INPUT", "confirmationStatus": "USER_CONFIRMED"},
        201,
    )
    request(
        c,
        "POST",
        f"/cases/{cid}/deposits",
        {"transactionId": tx["transactionId"], "allocatedAmount": 2090000},
        201,
    )
    pending = request(c, "GET", f"/cases/{cid}/results")
    assert snapshot_identity(pending["versions"]["calculation"]) == snapshot_identity(refs["calculation"])
    assert pending["versions"]["depositComparison"]["freshness"] == "UPDATING"
    drain(app)
    after = request(c, "GET", f"/cases/{cid}/results")
    assert snapshot_identity(after["versions"]["calculation"]) == snapshot_identity(refs["calculation"])
    assert snapshot_identity(after["versions"]["payslipValidation"]) == snapshot_identity(refs["payslipValidation"])
    deposit = after["depositComparison"]
    assert deposit["status"] == "MATCH" and deposit["linkedCashPaymentTotal"] == 2090000
    assert deposit["deposits"][0]["confirmationStatus"] == "USER_CONFIRMED"
    assert deposit["version"] == 2
    assert request(c, "GET", f"/cases/{cid}/recompute-status")["latestJob"]["status"] == "SUCCEEDED"


def test_basic_subtotal_is_confirmed_scope_and_never_gross(api):
    c, app, _, cid = api
    request(
        c,
        "POST",
        f"/cases/{cid}/manual-records",
        {"recordType": "CONTRACT", "data": {"hourlyWage": 10000}},
        201,
    )
    drain(app)
    assert request(c, "GET", f"/cases/{cid}/results")["calculation"]["calculatedSubtotal"] is None
    request(
        c,
        "PUT",
        f"/cases/{cid}/conditions",
        {
            "answers": [
                {"conditionId": "base_scope_confirmed", "value": True},
                {"conditionId": "regular_minutes", "value": 600},
            ]
        },
    )
    drain(app)
    data = request(c, "GET", f"/cases/{cid}/results")
    assert data["calculation"]["calculatedSubtotal"] == 100000
    assert data["calculation"]["grossPay"] is None
    assert data["calculation"]["items"][0]["rule"]["verified"] is False
    assert request(c, "GET", f"/cases/{cid}/readiness")["checks"][0]["mode"] == "PARTIAL"


def test_requested_checks_are_in_real_task_and_unrequested_views_unchanged(api):
    c, app, _, cid = api
    payslip(c, cid)
    drain(app)
    before = request(c, "GET", f"/cases/{cid}/results")["versions"]
    job = request(
        c, "POST", f"/cases/{cid}/recompute", {"checks": ["PAYSLIP_VALIDATION"], "reason": "USER_RETRY"}, 202
    )
    assert job["affectedChecks"] == ["PAYSLIP_VALIDATION"]
    request(c, "POST", f"/cases/{cid}/calculations", {"checks": ["PAYSLIP_VALIDATION"]}, 422)
    drain(app)
    with app.state.db.connect() as con:
        actual = con.execute(
            "SELECT * FROM agent_jobs WHERE task_kind='analyze_case' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert load(actual["payload_json"])["checks"] == ["PAYSLIP_VALIDATION"]
    output = load(actual["output_json"])
    assert output["wage_section"]["items"] == []
    assert all(
        i["reason_codes"] == ["CHECK_NOT_REQUESTED"]
        for i in output["comparisons"]
        if i["rule_id"] in ("R-01", "R-02", "R-06", "R-07")
    )
    after = request(c, "GET", f"/cases/{cid}/results")["versions"]
    assert {k: snapshot_identity(v) for k, v in after.items()} == {k: snapshot_identity(v) for k, v in before.items()}


def test_zero_missing_conflicting_values_do_not_become_false_matches(api):
    c, app, _, cid = api
    record = payslip(c, cid, grossPay=0, totalDeductions=0, netPay=0, paymentItems=[], deductionItems=[])
    drain(app)
    section = request(c, "GET", f"/cases/{cid}/results")["payslipValidation"]
    assert section["netPay"]["difference"] == 0 and section["paymentTotal"]["itemSum"] == 0
    request(c, "PATCH", f"/cases/{cid}/manual-records/{record['recordId']}", {"data": {"netPay": 0}})
    drain(app)
    section = request(c, "GET", f"/cases/{cid}/results")["payslipValidation"]
    assert section["netPay"]["difference"] is None
    payslip(c, cid)
    drain(app)
    section = request(c, "GET", f"/cases/{cid}/results")["payslipValidation"]
    assert section["netPay"]["status"] == "REVIEW_REQUIRED" and section["netPay"]["difference"] is None


def test_inquiry_validates_versions_and_renders_only_selected_items(api):
    c, app, _, cid = api
    payslip(c, cid)
    drain(app)
    data = request(c, "GET", f"/cases/{cid}/results")
    versions = {k: r["version"] for k, r in data["versions"].items()}
    request(c, "POST", f"/cases/{cid}/company-inquiry", {"resultItemIds": ["invalid"]}, 422)
    request(c, "POST", f"/cases/{cid}/company-inquiry", {"basedOnResultVersions": {"comparison": 999}}, 409)
    inquiry = request(
        c,
        "POST",
        f"/cases/{cid}/company-inquiry",
        {"resultItemIds": ["netPay"], "basedOnResultVersions": versions},
    )
    assert inquiry["requiresUserReview"] is True
    assert "2,100,000" in inquiry["text"] and "2,090,000" in inquiry["text"]
    assert "190,000" not in inquiry["text"]
    assert inquiry["selectedResultItemIds"] == ["netPay"]


def complete_report(app, c, cid, rid):
    for _ in range(12):
        progress_reports(app.state)
        asyncio.run(app.state.service.execute_next())
        data = request(c, "GET", f"/cases/{cid}/reports/{rid}")
        if data["status"] == "READY":
            return data
        assert data["status"] != "FAILED", data
    pytest.fail("Durable report pipeline did not finish")


def test_legacy_report_pipeline_freezes_inputs_idempotency_pdf_and_zip(api):
    c, app, _, cid = api
    request(c, "POST", f"/cases/{cid}/reports", {}, 409)
    record = payslip(c, cid)
    drain(app)
    body = {
        "includeOriginalDocuments": True,
        "documentIds": [],
        "useLatestAvailableResults": True,
        "acknowledgeOutdatedResults": False,
    }
    created = request(c, "POST", f"/cases/{cid}/reports", body, 202, headers={"Idempotency-Key": "report-1"})
    rid = created["reportId"]
    assert (
        request(c, "POST", f"/cases/{cid}/reports", body, 202, headers={"Idempotency-Key": "report-1"})
        == created
    )
    request(c, "GET", f"/cases/{cid}/reports/{rid}/pdf", status=409)
    # Changing inputs while queued must not rewrite the pinned report.
    request(
        c,
        "PATCH",
        f"/cases/{cid}/manual-records/{record['recordId']}",
        {"data": {"grossPay": 9000000, "totalDeductions": 0, "netPay": 9000000}},
    )
    ready = complete_report(app, c, cid, rid)
    assert ready["isCurrent"] is False
    pdf = c.get(f"/api/v1/cases/{cid}/reports/{rid}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "2,300,000" in text and "9,000,000" not in text
    bundle = c.get(f"/api/v1/cases/{cid}/reports/{rid}/originals")
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
        assert z.read("summary.pdf") == pdf.content
        assert "result-snapshot.json" in z.namelist()
    assert request(c, "GET", f"/cases/{cid}/reports")["total"] == 1
    request(c, "DELETE", f"/cases/{cid}", status=204)
    request(c, "GET", f"/cases/{cid}/reports/{rid}/pdf", status=404)


def test_legacy_candidate_confirmation_and_case_override_are_authorized(api):
    c, app, wid, cid = api
    app.state.service.provider = FixtureProvider()
    raw = pdf_bytes(["SYNTHETIC PAYSLIP", "gross: 2300000 KRW", "deduction: 200000 KRW", "net: 2090000 KRW"])
    r = c.post(
        f"/api/v1/cases/{cid}/documents",
        files={"file": ("salary.pdf", raw, "application/pdf")},
        data={"documentType": "PAYSLIP"},
    )
    assert r.status_code == 202, r.text
    did = r.json()["data"]["documentId"]
    asyncio.run(app.state.service.read_document(1, did))
    jid = app.state.service.enqueue_extraction(1, did)
    asyncio.run(app.state.service.execute_job(jid))
    field = next(
        f
        for f in request(c, "GET", f"/cases/{cid}/documents/{did}/fields")["fields"]
        if f["key"] == "grossPay"
    )
    assert field["value"] == 2300000 and field["valueType"] == "MONEY_KRW"
    other = request(
        c,
        "POST",
        "/cases",
        {"workplaceId": wid, "payType": "HOURLY", "periodStart": "2026-10-01", "periodEnd": "2026-10-31"},
        201,
    )["caseId"]
    link = request(c, "POST", f"/cases/{other}/document-links", {"documentId": did}, 201)
    fid = field["fieldId"]
    request(
        c,
        "PATCH",
        f"/cases/{cid}/documents/{did}/fields/{fid}",
        {"value": "invalid amount", "confirmed": True},
        422,
    )
    request(c, "PATCH", f"/cases/{cid}/documents/{did}/fields/{fid}", {"value": 2350000, "confirmed": True})
    current = next(
        f for f in request(c, "GET", f"/cases/{cid}/documents/{did}/fields")["fields"] if f["fieldId"] == fid
    )
    shared = next(
        f
        for f in request(c, "GET", f"/cases/{other}/documents/{did}/fields")["fields"]
        if f["fieldId"] == fid
    )
    assert (
        current["value"] == 2350000
        and current["status"] == "CONFIRMED"
        and current["hasCaseOverride"] is True
    )
    assert shared["value"] == 2300000 and shared["status"] == "UNCONFIRMED"
    # A new base extraction preserves the edit but requires a new explicit confirmation.
    refreshed = app.state.service.enqueue_extraction(1, did)
    asyncio.run(app.state.service.execute_job(refreshed))
    request(c, "PATCH", f"/cases/{cid}/documents/{did}/fields/{fid}", {"confirmed": True})
    current = next(
        f for f in request(c, "GET", f"/cases/{cid}/documents/{did}/fields")["fields"] if f["fieldId"] == fid
    )
    assert current["value"] == 2350000 and current["status"] == "CONFIRMED"
    drain(app)
    data = request(c, "GET", f"/cases/{cid}/results")
    assert any(s.get("documentId") == did for i in data["items"] for s in i["sources"])
    request(c, "DELETE", f"/cases/{other}/document-links/{link['linkId']}", status=204)
    assert (
        request(c, "GET", f"/cases/{cid}/results")["versions"]["payslipValidation"]["freshness"] == "CURRENT"
    )


@pytest.mark.parametrize("number", [21, 50])
def test_accepted_long_pdf_is_readable(api, number):
    c, app, wid, _ = api
    raw = pdf_bytes([f"SYNTHETIC page {i} salary: 100000 KRW" for i in range(number)])
    # fixture helper may put multiple lines on one page, so create actual pages.
    from reportlab.pdfgen.canvas import Canvas

    stream = io.BytesIO()
    writer = Canvas(stream)
    for i in range(number):
        writer.drawString(50, 700, f"SYNTHETIC page {i} salary 100000 KRW")
        writer.showPage()
    writer.save()
    raw = stream.getvalue()
    r = c.post(
        f"/api/v1/workplaces/{wid}/documents",
        files={"file": ("long.pdf", raw, "application/pdf")},
        data={"documentType": "PAYSLIP"},
    )
    assert r.status_code == 202, r.text
    result = asyncio.run(app.state.service.read_document(1, r.json()["data"]["documentId"]))
    assert result["status"] == "COMPLETED" and result["output"]["page_count"] == number


@pytest.mark.parametrize("value", ["1.5", "NaN", "Infinity", True])
def test_public_integer_projection_does_not_truncate_or_accept_invalid_numbers(value):
    with pytest.raises((ValueError, ArithmeticError)):
        money(value)


def test_chat_deduplicates_and_supports_opt_in_history_page(api):
    c, app, wid, cid = api
    calls = []
    from types import SimpleNamespace

    async def answer(payload):
        calls.append(payload)
        return SimpleNamespace(content="합성 대답", sources=[], thread_id=None)

    app.state.ai.chat = answer
    chat = request(
        c, "POST", f"/workplaces/{wid}/chats", {"defaultScope": {"scopeType": "CASE", "caseId": cid}}, 201
    )
    path = f"/api/v1/workplaces/{wid}/chats/{chat['chatId']}"
    body = {"content": "질문", "clientMessageId": "stable-1"}
    assert c.post(path + "/messages", json=body).status_code == 200
    duplicate = c.post(path + "/messages", json=body)
    assert duplicate.status_code == 200 and duplicate.headers["Idempotency-Replayed"] == "true"
    assert len(calls) == 1
    assert len(c.get(path).json()["data"]["messages"]) == 2
    paged = c.get(path + "?page=1&size=1").json()["data"]
    assert paged["session"]["messageCount"] == 2 and paged["messages"]["total"] == 2
    assert len(paged["messages"]["items"]) == 1
    assert c.post(path + "/messages", json={**body, "content": "다른 질문"}).status_code == 409


def test_running_workers_complete_legacy_flow_without_explicit_group_api(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.config import Settings
    import time

    app = create_app(
        Settings(_env_file=None, data_dir=tmp_path / "worker", workers_enabled=True, worker_concurrency=2),
        provider=FixtureProvider(),
    )
    with TestClient(app) as c:
        request(
            c,
            "POST",
            "/auth/signup",
            {"email": "worker@example.test", "password": "synthetic-password", "name": "test"},
            201,
        )
        c.headers["Authorization"] = (
            "Bearer "
            + request(
                c, "POST", "/auth/login", {"email": "worker@example.test", "password": "synthetic-password"}
            )["accessToken"]
        )
        wid = request(c, "POST", "/workplaces", {"name": "worker"}, 201)["workplaceId"]
        cid = request(
            c,
            "POST",
            "/cases",
            {"workplaceId": wid, "payType": "HOURLY", "periodStart": "2026-10-01", "periodEnd": "2026-10-31"},
            201,
        )["caseId"]
        payslip(c, cid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = request(c, "GET", f"/cases/{cid}/results")
            if data["freshness"]["overall"] == "CURRENT":
                break
            time.sleep(0.025)
        assert data["payslipValidation"]["netPay"]["difference"] == 10000
        report = request(c, "POST", f"/cases/{cid}/reports", {"includeOriginalDocuments": False}, 202)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            report = request(c, "GET", f"/cases/{cid}/reports/{report['reportId']}")
            if report["status"] != "PROCESSING":
                break
            time.sleep(0.025)
        assert report["status"] == "READY", report
        raw = pdf_bytes(["SYNTHETIC PAYSLIP", "gross: 2300000 KRW", "net: 2090000 KRW"])
        upload = c.post(
            f"/api/v1/cases/{cid}/documents",
            files={"file": ("salary.pdf", raw, "application/pdf")},
            data={"documentType": "PAYSLIP"},
        )
        assert upload.status_code == 202
        did = upload.json()["data"]["documentId"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            fields = request(c, "GET", f"/cases/{cid}/documents/{did}/fields")
            if fields["fields"]:
                break
            time.sleep(0.025)
        assert any(f["key"] == "grossPay" for f in fields["fields"]), fields


def test_fractional_agent_input_keeps_exact_agent_result_without_truncating_public_view(api):
    from test_api_contract import save_input

    c, app, _, cid = api
    gid = f"default-{cid}"
    confirmed = save_input(c, gid)["confirmedInput"]
    for f in confirmed["confirmed_fields"]:
        if f["field_id"] == "net":
            f["value"] = "1799999.5"
    version = request(c, "GET", f"/comparison-groups/{gid}")["version"]
    request(
        c,
        "PUT",
        f"/comparison-groups/{gid}/confirmed-input",
        {"expectedVersion": version, "confirmedInput": confirmed},
    )
    job = request(c, "POST", f"/comparison-groups/{gid}/analyze", status=202)
    asyncio.run(app.state.service.execute_job(job["jobId"]))
    detail = app.state.service.store.job(1, job["jobId"])
    assert detail["status"] == "COMPLETED", detail
    r5 = next(i for i in detail["output"]["comparisons"] if i["rule_id"] == "R-05")
    assert r5["delta"] == "0.5"
    public = request(c, "GET", f"/cases/{cid}/results")["payslipValidation"]["netPay"]
    assert public["status"] == "REVIEW_REQUIRED" and public["difference"] is None


def test_case_report_original_access_is_revoked_after_unlink(api):
    c, app, _, cid = api
    raw = pdf_bytes(["SYNTHETIC original payroll"])
    uploaded = c.post(
        f"/api/v1/cases/{cid}/documents",
        files={"file": ("original.pdf", raw, "application/pdf")},
        data={"documentType": "PAYSLIP"},
    )
    assert uploaded.status_code == 202
    did = uploaded.json()["data"]["documentId"]
    asyncio.run(app.state.service.read_document(1, did))
    payslip(c, cid)
    drain(app)
    report = request(c, "POST", f"/cases/{cid}/reports", {"documentIds": [did]}, 202)
    rid = report["reportId"]
    complete_report(app, c, cid, rid)
    assert c.get(f"/api/v1/cases/{cid}/reports/{rid}/pdf").status_code == 200
    linked = request(c, "GET", f"/cases/{cid}/documents/{did}")["linkId"]
    request(c, "DELETE", f"/cases/{cid}/document-links/{linked}", status=204)
    request(c, "GET", f"/cases/{cid}/reports/{rid}", status=404)
    request(c, "GET", f"/cases/{cid}/reports/{rid}/originals", status=404)


def test_queued_case_report_resumes_after_app_restart(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.config import Settings

    settings = Settings(_env_file=None, data_dir=tmp_path / "restart", workers_enabled=False)
    app = create_app(settings)
    with TestClient(app) as c:
        credentials = {"email": "resume@example.test", "password": "synthetic-password"}
        request(c, "POST", "/auth/signup", {**credentials, "name": "test"}, 201)
        token = request(c, "POST", "/auth/login", credentials)["accessToken"]
        c.headers["Authorization"] = "Bearer " + token
        wid = request(c, "POST", "/workplaces", {"name": "resume"}, 201)["workplaceId"]
        cid = request(
            c,
            "POST",
            "/cases",
            {"workplaceId": wid, "payType": "HOURLY", "periodStart": "2026-10-01", "periodEnd": "2026-10-31"},
            201,
        )["caseId"]
        payslip(c, cid)
        drain(app)
        report = request(c, "POST", f"/cases/{cid}/reports", {"includeOriginalDocuments": False}, 202)
    restarted = create_app(settings)
    with TestClient(restarted) as c:
        c.headers["Authorization"] = "Bearer " + token
        ready = complete_report(restarted, c, cid, report["reportId"])
        assert ready["status"] == "READY" and ready["isCurrent"] is True


def test_cancelling_underlying_agent_job_finishes_case_polling_job(api):
    from app.compatibility import enqueue_current

    c, app, _, cid = api
    saved = payslip(c, cid)
    jid = saved["recompute"]["jobId"]
    with app.state.db.connect(write=True) as con:
        con.execute("UPDATE jobs SET status='RUNNING' WHERE id=?", (jid,))
        ajid = enqueue_current(con, app.state.service, cid, metadata={"legacyJobId": jid})
    app.state.service.store.cancel(1, ajid)
    job = request(c, "GET", f"/cases/{cid}/recompute-jobs/{jid}")
    assert job["status"] == "FAILED" and job["errorCode"] == "JOB_CANCELLED"
