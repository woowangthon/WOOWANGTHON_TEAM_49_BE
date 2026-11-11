"""Four authored monthly PDFs replayed offline; this is not an AI/OCR quality claim."""

import copy
import json
from pathlib import Path

from agent.schemas.public.results import ExtractedField, ExtractionCandidate
from agent.services.hashing import digest

DIRECTORY = Path(__file__).resolve().parents[3] / "output/pdf/월급제_검증문서_202604"


async def seed_monthly_documents(store):
    gold = json.loads((DIRECTORY / "검증기준.json").read_text(encoding="utf-8"))
    names = {
        "attendance": "01_월급제_근무기록.pdf",
        "payslip": "02_월급제_급여명세서.pdf",
        "deposit": "03_월급제_입금기록.pdf",
        "contract": "04_월급제_표준근로계약서_스캔.pdf",
    }
    cases = {}
    for kind, filename in names.items():
        case = await store.upload((DIRECTORY / filename).read_bytes(), kind, filename=filename)
        source = store.document_ref(case["document_id"])
        pairs = {
            "employee_name": (gold["employee"], "text"),
            "employee_id": (gold["employee_id"], "text"),
            "employer_name": (gold["company"], "text"),
        }
        if kind != "contract":
            pairs.update(
                period_start=(gold["period"]["start"], "date"), period_end=(gold["period"]["end"], "date")
            )
        if kind == "contract":
            pairs.update(
                contract_rate=("3000000", "KRW/month"),
                pay_type=("월급", "text"),
                contract_start=("2026-03-01", "date"),
                contract_end=("2027-02-28", "date"),
            )
        elif kind == "attendance":
            pairs["worked_minutes"] = (str(gold["minutes"]["actual"]), "minute")
        elif kind == "payslip":
            pairs.update(
                slip_rate=("3000000", "KRW/month"),
                gross=(str(gold["earnings"]["gross"]), "KRW"),
                deduction=(str(gold["deduction_total"]), "KRW"),
                net=(str(gold["net"]), "KRW"),
                slip_actual_minutes=(str(gold["minutes"]["actual"]), "minute"),
            )
            for i, (label, key) in enumerate(
                [
                    ("기본급", "monthly_base"),
                    ("연장근로수당", "overtime"),
                    ("야간근로수당", "night_additional"),
                    ("휴일근로수당", "holiday"),
                ],
                1,
            ):
                pairs[f"earning_{i}_label"] = (label, "text")
                pairs[f"earning_{i}_amount"] = (str(gold["earnings"][key]), "KRW")
            for i, (label, amount) in enumerate(gold["deductions"].items(), 1):
                pairs[f"deduction_{i}_label"] = (label, "text")
                pairs[f"deduction_{i}_amount"] = (str(amount), "KRW")
        elif kind == "deposit":
            pairs["account_number"] = ("TEST-000-0026", "text")
            for i, row in enumerate(gold["transactions"], 1):
                for key, value, unit in [
                    ("date", row["at"][:10], "text"),
                    ("reference", row["id"], "text"),
                    ("description", row["memo"], "text"),
                    ("sender", row["sender"], "text"),
                    ("deposit_amount", str(row["deposit"]), "KRW"),
                ]:
                    pairs[f"transaction_{i}_{key}"] = (value, unit)
        fields, evidence = [], []
        for field_id, (value, unit) in pairs.items():
            eid = "authored-" + digest([case["id"], field_id])
            quote = f"검증 문서 작성값(실제 AI 판독 아님): {field_id}={value}"
            evidence.append(
                dict(
                    evidence_id=eid,
                    source_id=source.source_id,
                    page=1,
                    quote=quote,
                    verification_method="ocr_unverified",
                )
            )
            fields.append(
                ExtractedField(
                    field_id=field_id,
                    raw_value=value,
                    normalized_value=value,
                    suggested_value=value,
                    unit=unit,
                    evidence_refs=[eid],
                    source_quote=quote,
                    source_page=1,
                )
            )
        candidate = ExtractionCandidate(
            document_id=source.document_id,
            document_version=1,
            document_type=kind,
            extraction_version="1.2",
            model_config_version="authored-no-model",
            fields=fields,
            evidence_catalog=evidence,
            issues=["AUTHORED_FIXTURE_NOT_MODEL_EVALUATION"],
            page_count=store.documents[case["document_id"]]["pages"],
            verification_method="ocr_unverified",
        )
        store._apply(store.cases[case["id"]], {"stage": "extract"}, candidate)
        store.save_case(store.cases[case["id"]])
        cases[kind] = case["id"]
    bundle = await store.create_comparison(
        "한빛정밀 2026년 4월 검증",
        period_start=gold["period"]["start"],
        period_end=gold["period"]["end"],
        selection={
            "contract_case_id": cases["contract"],
            "attendance_case_ids": [cases["attendance"]],
            "payslip_case_id": cases["payslip"],
            "deposit_case_ids": [cases["deposit"]],
        },
    )
    draft = copy.deepcopy(bundle["draft"])

    def known(value):
        return dict(state="known", value=value)

    wage = {
        key: known(True)
        for key in (
            "standard_employment",
            "contract_unchanged",
            "five_or_more_workers",
            "full_period_employment",
            "records_complete",
            "composition_complete",
            "monthly_weekly_rest_included",
        )
    }
    wage.update(
        ordinary_monthly_amount=known(str(gold["contract"]["ordinary_monthly_wage"])),
        monthly_standard_minutes=known(gold["contract"]["monthly_conversion_hours"] * 60),
        schedule=known(
            dict(
                days=[dict(weekday=d, minutes=480) for d in range(5)],
                week_start=0,
                weekly_rest_weekday=6,
                weekly_rest_minutes=480,
            )
        ),
        calculation_period=gold["period"],
        days=[],
        statement_mappings=[],
    )
    records = []
    for row in gold["records"]:
        kind = {
            "무급휴무": "unpaid_rest",
            "유급주휴": "weekly_rest",
            "유급휴일": "paid_holiday",
            "휴일근무": "weekly_rest",
        }.get(row["classification"], "workday")
        wage["days"].append(
            dict(
                date=row["date"],
                kind=kind,
                attendance="present" if row["start_at"] else "not_scheduled",
                confirmed=True,
            )
        )
        if row["start_at"]:
            records.append(
                dict(
                    record_id="work-" + row["date"],
                    start_at=row["start_at"],
                    end_at=row["end_at"],
                    breaks=[
                        [f"{row['date']}T{a}:00+09:00", f"{row['date']}T{b}:00+09:00"]
                        for a, b in row["breaks"]
                    ],
                    evidence_refs=[],
                )
            )
    for row, rules in zip(bundle["materials"]["earnings"], [["W-01", "W-02"], ["W-03"], ["W-04"], ["W-05"]]):
        wage["statement_mappings"].append(
            dict(
                rule_ids=rules,
                statement_item_ids=[row["item_id"]],
                period=gold["period"],
                state="known",
                evidence_refs=row["evidence_refs"],
            )
        )
    draft.update(
        wage_conditions=wage,
        work_records=records,
        coverage=[dict(start="2026-03-30", end="2026-05-03")],
        subjects_confirmed=True,
        contract_period_confirmed=True,
        worked_time_kind="actual",
        slip_time_kind="actual",
        earnings_complete=True,
        deductions_complete=True,
        payment_scope_confirmed=True,
    )
    for i, transaction in enumerate(draft["material_review"]["transactions"].values()):
        transaction["selected"] = i < 2
    await store.save_draft(bundle["id"], draft)
    await store.confirm(bundle["id"])
    return bundle["id"], cases, gold
