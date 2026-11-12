"""documentType=AUTO uploads, reviewable type conflicts and PATCH document type."""

import asyncio

from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import pdf_bytes
from app.db import load
from test_api_contract import api as api_fixture

api = api_fixture

PAYSLIP = pdf_bytes(["SYNTHETIC PAYSLIP", "gross: 2000000 KRW", "deduction: 200000 KRW", "net: 1800000 KRW"])


class ClaimsDeposit(FixtureProvider):
    """A model that reads every document as a deposit record."""

    async def invoke(self, operation, data):
        response, usage = await super().invoke(operation, data)
        if operation != "explain":
            response["document_type"] = "deposit"
        return response, usage


def upload(client, cid, data, document_type):
    response = client.post(
        f"/api/v1/cases/{cid}/documents",
        files={"file": ("upload.pdf", data, "application/pdf")},
        data={"documentType": document_type},
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]["documentId"]


def read_and_extract(app, did):
    asyncio.run(app.state.service.read_document(1, did))
    jid = app.state.service.enqueue_extraction(1, did)
    asyncio.run(app.state.service.execute_job(jid))
    return app.state.service.store.job(1, jid)


def doc(client, wid, did):
    response = client.get(f"/api/v1/workplaces/{wid}/documents/{did}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_auto_upload_takes_the_classified_type(api):
    client, app, wid, cid = api
    app.state.service.provider = FixtureProvider()
    did = upload(client, cid, PAYSLIP, "AUTO")
    assert doc(client, wid, did)["documentType"] == "AUTO"
    job = read_and_extract(app, did)
    assert job["status"] == "COMPLETED", job
    data = doc(client, wid, did)
    assert data["documentType"] == "PAYSLIP"
    assert data["processingStatus"] == "REVIEW_REQUIRED"
    assert data["errorCode"] is None
    assert data["aiMetadata"]["detectedType"] == "PAYSLIP"
    fields = client.get(f"/api/v1/cases/{cid}/documents/{did}/fields").json()["data"]["fields"]
    assert {field["key"] for field in fields} >= {"grossPay"}


def test_unclassifiable_auto_upload_is_reviewable(api):
    client, app, wid, cid = api
    app.state.service.provider = FixtureProvider()
    did = upload(client, cid, pdf_bytes(["UNKNOWN PAPER", "mystery: 5 KRW"]), "AUTO")
    job = read_and_extract(app, did)
    assert job["status"] == "FAILED" and job["error_code"] == "DOCUMENT_TYPE_UNRESOLVED", job
    data = doc(client, wid, did)
    assert data["documentType"] == "AUTO"
    assert data["processingStatus"] == "REVIEW_REQUIRED"
    assert data["errorCode"] == "DOCUMENT_TYPE_UNRESOLVED"
    assert "detectedType" not in data["aiMetadata"]


def test_type_conflict_reports_detected_type_and_patch_reextracts(api):
    client, app, wid, cid = api
    app.state.service.provider = ClaimsDeposit()
    did = upload(client, cid, PAYSLIP, "PAYSLIP")
    job = read_and_extract(app, did)
    assert job["status"] == "FAILED" and job["error_code"] == "DOCUMENT_TYPE_MISMATCH", job
    data = doc(client, wid, did)
    assert data["processingStatus"] == "REVIEW_REQUIRED"
    assert data["errorCode"] == "DOCUMENT_TYPE_MISMATCH"
    assert data["aiMetadata"]["detectedType"] == "DEPOSIT"
    assert data["documentType"] == "PAYSLIP"

    # The user keeps their own choice of type: extraction runs again with it.
    app.state.service.provider = FixtureProvider()
    changed = client.patch(f"/api/v1/workplaces/{wid}/documents/{did}", json={"documentType": "PAYSLIP"})
    assert changed.status_code == 202, changed.text
    body = changed.json()["data"]
    assert body["processingStatus"] == "PROCESSING" and body["errorCode"] is None
    assert "detectedType" not in body["aiMetadata"]
    with app.state.db.connect() as con:
        stored = con.execute("SELECT processing_status FROM documents WHERE id=?", (did,)).fetchone()
        reads = con.execute("SELECT COUNT(*) FROM document_ocr WHERE document_id=?", (did,)).fetchone()[0]
    assert stored["processing_status"] == "READ_READY"  # the stored reading is reused, not re-OCR'd
    jid = app.state.service.enqueue_extraction(1, did)
    asyncio.run(app.state.service.execute_job(jid))
    assert app.state.service.store.job(1, jid)["status"] == "COMPLETED"
    data = doc(client, wid, did)
    assert data["processingStatus"] == "REVIEW_REQUIRED" and data["errorCode"] is None
    with app.state.db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM document_ocr WHERE document_id=?", (did,)).fetchone()[0] == reads


def test_patch_document_type_guards(api):
    client, app, wid, cid = api
    app.state.service.provider = FixtureProvider()
    did = upload(client, cid, PAYSLIP, "PAYSLIP")
    url = f"/api/v1/workplaces/{wid}/documents/{did}"
    # Still being read/extracted.
    assert client.patch(url, json={"documentType": "DEPOSIT"}).json()["data"]["errorCode"] == "DOCUMENT_STILL_PROCESSING"
    assert read_and_extract(app, did)["status"] == "COMPLETED"
    assert client.patch(url, json={"documentType": "OTHER"}).status_code == 422
    # Same type without an error is a no-op: candidates stay.
    same = client.patch(url, json={"documentType": "PAYSLIP"})
    assert same.status_code == 202 and same.json()["data"]["processingStatus"] == "REVIEW_REQUIRED"
    with app.state.db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM fields WHERE document_id=?", (did,)).fetchone()[0] > 0
    # A reviewed value is never discarded by a type change.
    with app.state.db.connect(write=True) as con:
        con.execute("UPDATE fields SET status='CONFIRMED' WHERE document_id=? AND key='gross'", (did,))
    refused = client.patch(url, json={"documentType": "DEPOSIT"})
    assert refused.status_code == 409 and refused.json()["data"]["errorCode"] == "DOCUMENT_REVIEW_STARTED"
    with app.state.db.connect(write=True) as con:
        con.execute("UPDATE fields SET status='UNCONFIRMED' WHERE document_id=?", (did,))
    moved = client.patch(url, json={"documentType": "DEPOSIT"})
    assert moved.status_code == 202, moved.text
    assert moved.json()["data"]["documentType"] == "DEPOSIT"
    with app.state.db.connect() as con:
        # Unreviewed payslip candidates do not linger under the new type.
        assert con.execute("SELECT COUNT(*) FROM fields WHERE document_id=?", (did,)).fetchone()[0] == 0
        metadata = load(con.execute("SELECT ai_metadata FROM documents WHERE id=?", (did,)).fetchone()[0])
    assert metadata["legacyAutoExtract"] is True


def test_read_to_extraction_handoff_is_reported_as_processing(api):
    client, app, wid, cid = api
    app.state.service.provider = FixtureProvider()
    did = upload(client, cid, PAYSLIP, "AUTO")
    asyncio.run(app.state.service.read_document(1, did))
    with app.state.db.connect() as con:
        assert con.execute("SELECT processing_status FROM documents WHERE id=?", (did,)).fetchone()[0] == "READ_READY"
    # Clients must keep polling: extraction starts right after reading.
    assert doc(client, wid, did)["processingStatus"] == "PROCESSING"
    fields = client.get(f"/api/v1/cases/{cid}/documents/{did}/fields").json()["data"]
    assert fields["processingStatus"] == "PROCESSING"
    with app.state.db.connect(write=True) as con:
        con.execute("UPDATE documents SET error_code='MODEL_NOT_CONFIGURED' WHERE id=?", (did,))
    assert doc(client, wid, did)["processingStatus"] == "REVIEW_REQUIRED"
