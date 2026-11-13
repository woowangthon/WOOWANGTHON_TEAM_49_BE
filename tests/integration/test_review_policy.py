"""Review API -> SQLite -> immutable real Agent snapshot/result regressions."""

import pytest
from agent.tests.fixtures_support import pdf_bytes
from agent.services.hashing import content_hash
from app.db import dump, now, load
from app.compatibility import collect, prepare_snapshot
from app.review_policy import metadata
from test_api_contract import api as api_fixture

from test_legacy_contract_restored import request, drain

api = api_fixture


def seed(app, wid, cid, kind="CONTRACT", fields=None):
    fields = fields or [
        ("periodStart", "2026-10-01", "CONFIRMED"),
        ("periodEnd", "2026-10-31", "CONFIRMED"),
        ("payType", "HOURLY", "CONFIRMED"),
        ("regularMinutes", 100, "CONFIRMED"),
        ("name", "Kim", "UNCONFIRMED"),
        ("address", "Seoul", "UNCONFIRMED"),
        ("memo", "ok", "UNCONFIRMED"),
    ]
    data = pdf_bytes("Review policy fixture")
    (app.state.settings.data_dir / "uploads" / "review.pdf").write_bytes(data)
    with app.state.db.connect(write=True) as con:
        did = con.execute(
            "INSERT INTO documents(workplace_id,document_type,file_name,storage_name,media_type,sha256,processing_status,created_at) VALUES(?,?,'review.pdf','review.pdf','application/pdf',?,'READY',?)",
            (wid, kind, content_hash(data), now()),
        ).lastrowid
        con.execute("INSERT INTO document_links(case_id,document_id) VALUES(?,?)", (cid, did))
        for key, value, status in fields:
            con.execute(
                "INSERT INTO fields(document_id,key,label,value_json,value_type,status) VALUES(?,?,?,?,?,?)",
                (
                    did,
                    key,
                    key,
                    dump(value),
                    "MONEY_LIST_KRW"
                    if isinstance(value, list)
                    else "MONEY_KRW"
                    if type(value) is int
                    else "TEXT",
                    status,
                ),
            )
        ids = {r["key"]: r["id"] for r in con.execute("SELECT * FROM fields WHERE document_id=?", (did,))}
    return did, ids


def test_complete_and_optional_edit(api):
    c, app, wid, cid = api
    did, ids = seed(app, wid, cid)
    base = f"/cases/{cid}/documents/{did}"
    before = request(c, "GET", f"/cases/{cid}")["inputVersion"]
    summary = request(c, "POST", base + "/review/confirm")
    assert summary["reviewStatus"] == "CONFIRMED"
    assert summary["requiredReviewCount"] == 4 and summary["resolvedRequiredCount"] == 4
    assert len(summary["unconfirmedFieldIds"]) == 3 and not summary["pendingRequiredFieldIds"]
    assert request(c, "GET", f"/cases/{cid}")["inputVersion"] == before
    result = request(c, "PATCH", base + f"/fields/{ids['memo']}", {"value": "changed"})
    assert result["analysisValueOrigin"] == "SYSTEM_EXTRACTED_UNVERIFIED"
    assert request(c, "GET", base + "/review")["reviewStatus"] == "CONFIRMED"
    request(c, "PATCH", base + f"/fields/{ids['periodStart']}", {"value": "2026-10-02"})
    assert request(c, "GET", base + "/review")["reviewStatus"] == "IN_PROGRESS"
    fields = request(c, "GET", base + "/fields")["fields"]
    assert all("usableForAnalysis" in f for f in fields)


@pytest.mark.parametrize("resolution", ["UNKNOWN", "NOT_IN_DOCUMENT"])
def test_resolution(api, resolution):
    c, app, wid, cid = api
    did, ids = seed(app, wid, cid, fields=[("memo", None, "MISSING")])
    base = f"/cases/{cid}/documents/{did}"
    path = base + f"/fields/{ids['memo']}"
    assert not request(c, "POST", base + "/review/confirm")["canCompleteReview"]
    result = request(c, "PATCH", path, {"reviewResolution": resolution})
    assert result["value"] is None and result["status"] == "MISSING"
    assert result["reviewResolved"] and not result["usableForAnalysis"]
    review = request(c, "POST", base + "/review/confirm")
    assert review["canCompleteReview"] and review["unconfirmedFieldIds"] == [ids["memo"]]
    assert not review["pendingRequiredFieldIds"]
    request(c, "PATCH", path, {"reviewResolution": "NONE"})
    assert not request(c, "GET", base + "/review")["canCompleteReview"]
    for body in [
        {"reviewResolution": "UNKNOWN", "value": None},
        {"reviewResolution": "UNKNOWN", "confirmed": False},
    ]:
        request(c, "PATCH", path, body, 422)
    request(
        c,
        "PATCH",
        f"/workplaces/{wid}/documents/{did}/fields/{ids['memo']}",
        {"reviewResolution": "UNKNOWN"},
        422,
    )
    request(c, "PATCH", path, {"reviewResolution": "UNKNOWN", "expectedInputVersion": 1}, 409)


@pytest.mark.parametrize(
    "status", ["CONFLICTED", "DEFERRED", "NEEDS_RECONFIRMATION", "UNREADABLE", "MISSING"]
)
def test_exclusion_and_resolution_guard(api, status):
    c, app, wid, cid = api
    did, ids = seed(app, wid, cid, fields=[("basePay", 123, status)])
    path = f"/cases/{cid}/documents/{did}/fields/{ids['basePay']}"
    result = request(c, "GET", f"/cases/{cid}/documents/{did}/fields")["fields"][0]
    assert not result["usableForAnalysis"] and result["requiresUserReview"]
    request(c, "PATCH", path, {"reviewResolution": "UNKNOWN"}, 422)
    with app.state.db.connect() as con:
        assert not collect(con, cid)["groups"]["CONTRACT"]


@pytest.mark.parametrize("status,count", [("UPLOADED", 1), ("PROCESSING", 1), ("FAILED", 1), ("READY", 0)])
def test_uncompletable(api, status, count):
    c, app, wid, cid = api
    did, _ = seed(app, wid, cid)
    with app.state.db.connect(write=True) as con:
        con.execute("UPDATE documents SET processing_status=? WHERE id=?", (status, did))
        if not count:
            con.execute("DELETE FROM fields WHERE document_id=?", (did,))
    base = f"/cases/{cid}/documents/{did}"
    assert not request(c, "GET", base + "/review")["canCompleteReview"]
    response = request(
        c, "POST", base + "/review/confirm", status=409 if status in {"UPLOADED", "PROCESSING"} else 200
    )
    if status not in {"UPLOADED", "PROCESSING"}:
        assert response["reviewStatus"] == "IN_PROGRESS"


def test_optional_reaches_real_agent_with_unverified_provenance(api):
    c, app, wid, cid = api
    did, ids = seed(
        app,
        wid,
        cid,
        "PAYSLIP",
        [
            ("grossPay", 1000, "CONFIRMED"),
            ("totalDeductions", 100, "CONFIRMED"),
            ("netPay", 900, "CONFIRMED"),
            ("paymentItems", [800, 200], "UNCONFIRMED"),
        ],
    )
    base = f"/cases/{cid}/documents/{did}"
    assert request(c, "POST", base + "/review/confirm")["canCompleteReview"]
    request(c, "PATCH", base + f"/fields/{ids['paymentItems']}", {"value": [800, 200]})
    request(c, "PUT", base + "/statement-scope", {"paymentItems": True})
    with app.state.db.connect(write=True) as con:
        snapshot, *_ = prepare_snapshot(con, cid, "inspect")
        assert snapshot.earnings[0].confirmed is False
        assert snapshot.earnings[0].analysis_value_origin == "SYSTEM_EXTRACTED_UNVERIFIED"
        assert all(f.field_id != "paymentItems" for f in snapshot.confirmed_fields)
    drain(app)
    result = request(c, "GET", f"/cases/{cid}/results")
    payment = result["payslipValidation"]["paymentTotal"]
    assert payment["status"] == "MATCH" and payment["itemSum"] == 1000
    automatic = [
        s for s in payment["sources"] if s.get("analysisValueOrigin") == "SYSTEM_EXTRACTED_UNVERIFIED"
    ]
    assert automatic and automatic[0]["confirmedAt"] is None
    with app.state.db.connect() as con:
        old = con.execute(
            "SELECT data_json FROM agent_snapshots ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        body = load(old)
        evs = {e["evidence_id"]: e for e in body["evidence_catalog"]}
        assert all(
            evs[e]["verification_method"] == "ocr_unverified"
            for item in body["earnings"]
            for e in item["evidence_refs"]
        )
    request(c, "PATCH", base + f"/fields/{ids['paymentItems']}", {"value": [1000, 200]})
    request(c, "PUT", base + "/statement-scope", {"paymentItems": True})
    drain(app)
    assert (
        request(c, "GET", f"/cases/{cid}/results")["payslipValidation"]["paymentTotal"]["status"]
        == "DIFFERENCE"
    )
    with app.state.db.connect() as con:
        assert con.execute("SELECT 1 FROM agent_snapshots WHERE data_json=?", (old,)).fetchone()


def test_scalar_optional_is_separate_agent_input(api):
    c, app, wid, cid = api
    seed(app, wid, cid, "PAYSLIP", [("hourlyWage", 10000, "UNCONFIRMED")])
    with app.state.db.connect(write=True) as con:
        snapshot, *_ = prepare_snapshot(con, cid, "scalar")
        assert not any(f.field_id == "slip_rate" for f in snapshot.confirmed_fields)
        assert snapshot.extracted_fields[0].field_id == "slip_rate"
        assert "confirmation_id" not in snapshot.extracted_fields[0].model_dump()


def test_priority():
    f = {"key": "memo", "value": None, "status": "CONFLICTED"}
    assert metadata(f, "AUTO", "HOURLY", "UNKNOWN")["reviewReason"] == "CONFLICTED"
    assert not metadata(f, "AUTO", "HOURLY", "UNKNOWN")["reviewResolved"]


def test_shared_cases_overrides_and_pay_type(api):
    c, app, wid, cid = api
    other = request(
        c,
        "POST",
        "/cases",
        {"workplaceId": wid, "payType": "MONTHLY", "periodStart": "2026-10-01", "periodEnd": "2026-10-31"},
        201,
    )["caseId"]
    did, ids = seed(
        app, wid, cid, fields=[("hourlyWage", 10000, "CONFIRMED"), ("monthlyBasePay", 2000000, "UNCONFIRMED")]
    )
    with app.state.db.connect(write=True) as con:
        con.execute("INSERT INTO document_links(case_id,document_id) VALUES(?,?)", (other, did))
    base = f"/cases/{cid}/documents/{did}"
    second = f"/cases/{other}/documents/{did}"
    assert request(c, "POST", base + "/review/confirm")["canCompleteReview"]
    assert not request(c, "POST", second + "/review/confirm")["canCompleteReview"]
    request(c, "PATCH", second + f"/fields/{ids['monthlyBasePay']}", {"confirmed": True})
    assert request(c, "POST", second + "/review/confirm")["canCompleteReview"]
    request(c, "PATCH", f"/workplaces/{wid}/documents/{did}/fields/{ids['hourlyWage']}", {"value": 11000})
    assert request(c, "GET", base + "/review")["reviewStatus"] == "IN_PROGRESS"
    assert request(c, "GET", second + "/review")["reviewStatus"] == "CONFIRMED"
    assert request(c, "GET", second + "/fields")["fields"][1]["hasCaseOverride"]
    request(c, "PATCH", base + f"/fields/{ids['hourlyWage']}", {"confirmed": True})
    request(c, "POST", base + "/review/confirm")
    request(
        c, "PATCH", f"/workplaces/{wid}/documents/{did}/fields/{ids['monthlyBasePay']}", {"value": 2100000}
    )
    assert request(c, "GET", base + "/review")["reviewStatus"] == "CONFIRMED"
    assert request(c, "GET", second + "/review")["reviewStatus"] == "CONFIRMED"


def test_scalar_actual_comparison(api):
    c, app, wid, cid = api
    seed(app, wid, cid, "CONTRACT", [("hourlyWage", 10000, "CONFIRMED")])
    did, ids = seed(app, wid, cid, "PAYSLIP", [("hourlyWage", 10500, "UNCONFIRMED")])
    request(
        c,
        "PUT",
        f"/cases/{cid}/conditions",
        {"answers": [{"conditionId": "comparison_basis_confirmed", "value": True}]},
    )
    drain(app)
    result = request(c, "GET", f"/cases/{cid}/results")
    rate = next(i for i in result["items"] if i["id"] == "HOURLY_WAGE")
    assert rate["difference"] == 500
    assert any(s.get("analysisValueOrigin") == "SYSTEM_EXTRACTED_UNVERIFIED" for s in rate["sources"])


def test_migration_and_source_reset(api):
    c, app, wid, cid = api
    did, ids = seed(app, wid, cid, fields=[("memo", None, "MISSING")])
    path = f"/cases/{cid}/documents/{did}/fields/{ids['memo']}"
    request(c, "PATCH", path, {"reviewResolution": "UNKNOWN"})
    app.state.db.initialize()
    with app.state.db.connect(write=True) as con:
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
        con.execute("UPDATE fields SET source_json=? WHERE id=?", (dump({"page": 2}), ids["memo"]))
        assert not con.execute("SELECT * FROM field_review_resolutions").fetchall()
        assert con.execute("SELECT COUNT(*) FROM field_history").fetchone()[0] >= 2


def test_old_snapshot_hash_unchanged():
    from agent.schemas.public.inputs import InputSnapshot
    from agent.services.hashing import digest, snapshot_hash

    snapshot = InputSnapshot(
        snapshot_id="old",
        input_version=1,
        owner_id="1",
        workplace_id="1",
        case_id="1",
        period={"start": "2026-10-01", "end": "2026-10-31"},
    )
    legacy = snapshot.model_dump(
        exclude={"snapshot_hash", "snapshot_id", "extracted_fields", "wage_conditions"}
    )
    assert snapshot_hash(snapshot) == digest(legacy)


def test_source_refresh_invalidates_override_and_resolution(api):
    from app.agent_review import invalidate_document_confirmations
    from app.compatibility import schedule

    c, app, wid, cid = api
    did, ids = seed(app, wid, cid, fields=[("memo", None, "MISSING"), ("hourlyWage", 10000, "CONFIRMED")])
    base = f"/cases/{cid}/documents/{did}"
    request(c, "PATCH", base + f"/fields/{ids['memo']}", {"reviewResolution": "UNKNOWN"})
    request(c, "PATCH", base + f"/fields/{ids['hourlyWage']}", {"confirmed": True})
    assert request(c, "POST", base + "/review/confirm")["canCompleteReview"]
    with app.state.db.connect(write=True) as con:
        invalidate_document_confirmations(con, did, "TEST_REFRESH")
        schedule(con, cid)
        assert not con.execute("SELECT * FROM field_review_resolutions").fetchall()
    review = request(c, "GET", base + "/review")
    assert review["reviewStatus"] == "IN_PROGRESS" and len(review["pendingRequiredFieldIds"]) == 2
    fields = request(c, "GET", base + "/fields")["fields"]
    assert fields[1]["reviewReason"] == "RECONFIRMATION" and not fields[1]["usableForAnalysis"]


@pytest.mark.parametrize("status,reason", [("CONFLICTED", "CONFLICTED"), ("DEFERRED", "RECONFIRMATION")])
def test_invalid_number_keeps_higher_priority(status, reason):
    f = {"key": "grossPay", "value": "invalid", "status": status}
    assert metadata(f, "PAYSLIP", "HOURLY")["reviewReason"] == reason


def test_empty_automatic_items_do_not_invent_zero_scope(api):
    c, app, wid, cid = api
    seed(app, wid, cid, "PAYSLIP", [("paymentItems", [], "UNCONFIRMED")])
    with app.state.db.connect(write=True) as con:
        snapshot, *_ = prepare_snapshot(con, cid, "empty")
        assert snapshot.earnings == [] and not snapshot.earnings_complete


def test_extracted_scalar_cannot_claim_manual_confirmation(api):
    from agent.services.hashing import snapshot_hash
    from agent.services.calculation_context import build_calculation_context
    from agent.schemas.public.errors import IntegrityError

    c, app, wid, cid = api
    seed(app, wid, cid, "PAYSLIP", [("hourlyWage", 10000, "UNCONFIRMED")])
    with app.state.db.connect(write=True) as con:
        snapshot, *_ = prepare_snapshot(con, cid, "boundary")
        evidence = next(
            e
            for e in snapshot.evidence_catalog
            if e.evidence_id in snapshot.extracted_fields[0].evidence_refs
        )
        evidence.verification_method = "manual_confirmed"
        evidence.page = 1
        snapshot.snapshot_hash = snapshot_hash(snapshot)
        with pytest.raises(IntegrityError, match="unconfirmed document evidence"):
            build_calculation_context(snapshot, snapshot.owner_id, snapshot.case_id)
