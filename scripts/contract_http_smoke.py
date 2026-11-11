"""Synthetic legacy frontend flow against real HTTP workers; no paid model calls."""

import argparse
import io
import json
import secrets
import time
import uuid
import zipfile

import httpx


def check(base_url, timeout=30):
    with httpx.Client(base_url=base_url, timeout=15) as c:

        def req(method, path, body=None, status=200, **kwargs):
            r = c.request(method, "/api/v1" + path, **({"json": body} if body is not None else {}), **kwargs)
            assert r.status_code == status, f"{method} {path}: HTTP {r.status_code}"
            return r.json()["data"]

        def wait(path, done):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                data = req("GET", path)
                if done(data):
                    return data
                time.sleep(0.1)
            raise TimeoutError(path)

        creds = {"email": f"contract-{uuid.uuid4().hex}@example.test", "password": secrets.token_urlsafe(24)}
        req("POST", "/auth/signup", {**creds, "name": "합성 HTTP 계약 검사"}, 201)
        c.headers["Authorization"] = "Bearer " + req("POST", "/auth/login", creds)["accessToken"]
        wid = req("POST", "/workplaces", {"name": "SYNTHETIC contract HTTP"}, 201)["workplaceId"]
        cid = req(
            "POST",
            "/cases",
            {"workplaceId": wid, "payType": "HOURLY", "periodStart": "2026-10-01", "periodEnd": "2026-10-31"},
            201,
        )["caseId"]
        req(
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
                },
            },
            201,
        )
        result = wait(f"/cases/{cid}/results", lambda d: d["freshness"]["overall"] == "CURRENT")
        assert result["payslipValidation"]["netPay"]["difference"] == 10000
        assert result["payslipValidation"]["deductionTotal"]["difference"] == -10000
        refs = result["versions"]
        tx = req(
            "POST",
            f"/workplaces/{wid}/payment-transactions",
            {
                "paymentMethod": "CASH",
                "transactionDate": "2026-11-01",
                "amount": 2090000,
                "source": "USER_INPUT",
            },
            201,
        )
        req(
            "POST",
            f"/cases/{cid}/deposits",
            {"transactionId": tx["transactionId"], "allocatedAmount": 2090000},
            201,
        )
        result = wait(f"/cases/{cid}/results", lambda d: d["freshness"]["overall"] == "CURRENT")
        assert result["depositComparison"]["status"] == "MATCH"
        assert result["versions"]["calculation"] == refs["calculation"]
        assert result["versions"]["payslipValidation"] == refs["payslipValidation"]
        inquiry = req("POST", f"/cases/{cid}/company-inquiry", {"resultItemIds": ["netPay"]})
        assert inquiry["selectedResultItemIds"] == ["netPay"] and "2,100,000" in inquiry["text"]
        body = {"includeOriginalDocuments": False, "documentIds": [], "useLatestAvailableResults": True}
        report = req(
            "POST", f"/cases/{cid}/reports", body, 202, headers={"Idempotency-Key": "synthetic-report"}
        )
        repeated = req(
            "POST", f"/cases/{cid}/reports", body, 202, headers={"Idempotency-Key": "synthetic-report"}
        )
        assert report == repeated
        rid = report["reportId"]
        report = wait(f"/cases/{cid}/reports/{rid}", lambda d: d["status"] in ("READY", "FAILED"))
        assert report["status"] == "READY", report
        pdf = c.get(f"/api/v1/cases/{cid}/reports/{rid}/pdf")
        bundle = c.get(f"/api/v1/cases/{cid}/reports/{rid}/originals")
        assert pdf.status_code == bundle.status_code == 200 and pdf.content.startswith(b"%PDF")
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
            assert z.read("summary.pdf") == pdf.content
            assert (
                json.loads(z.read("result-snapshot.json"))["input_version"] == result["currentInputVersion"]
            )
        cors = c.options(
            "/api/v1/cases",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        assert (
            cors.status_code == 200 and cors.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        )
        return {
            "transport": "real Docker TCP HTTP",
            "manual_to_real_agent": True,
            "net_delta": 10000,
            "deduction_delta": -10000,
            "independent_versions": True,
            "cash_reported_status_preserved": True,
            "inquiry_selection": True,
            "legacy_report_pdf_zip": True,
            "idempotency": True,
            "cors": True,
            "paid_model_calls": 0,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    print(json.dumps(check(args.base_url), ensure_ascii=False, indent=2))
