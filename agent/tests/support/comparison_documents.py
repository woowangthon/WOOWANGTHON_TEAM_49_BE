"""Authored four-document examples, isolated from OCR and model accuracy tests."""

import copy
import json
from pathlib import Path

from agent.schemas.public.inputs import EvidenceRef
from agent.schemas.public.results import ExtractedField, ExtractionCandidate
from agent.services.hashing import content_hash, digest
from agent.tests.fixtures_support import pdf_bytes

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/comparison_four_documents.json"


class NoModelCalls:
    async def invoke(self, operation, data):
        raise AssertionError("Saved-document comparison must not call an AI model")


def author_gold():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def selection(cases, *, contract=False):
    return {
        "contract_case_id": cases["contract"] if contract else None,
        "attendance_case_ids": [cases["attendance"]],
        "payslip_case_id": cases["payslip"],
        "deposit_case_ids": [cases["deposit"]],
    }


async def seed_documents(store, *, actual_files=False):
    """Upload real bytes or portable PDFs, then seed explicit author gold.

    Evidence is deliberately marked OCR-unverified. Quotes label test data;
    seeding is not an assertion that OCR or a model extracted these values.
    All mutations are inside the caller's isolated LocalStore.
    """
    cases = {}
    for kind, definition in author_gold()["documents"].items():
        path = ROOT / definition["path"]
        data = path.read_bytes() if actual_files else pdf_bytes(["Authored comparison fixture: " + kind])
        if actual_files:
            assert content_hash(data) == definition["sha256"], path
        case = await store.upload(data, kind, filename=path.name)
        ref = store.document_ref(case["document_id"])
        fields, evidence = [], []
        for field_id, field in definition["fields"].items():
            quote = f"Author gold / {field_id}: {field['value']}"
            evidence_id = "gold-" + digest([case["id"], field_id])
            evidence.append(
                EvidenceRef(
                    evidence_id=evidence_id,
                    source_id=ref.source_id,
                    page=1,
                    quote=quote,
                    verification_method="ocr_unverified",
                )
            )
            fields.append(
                ExtractedField(
                    field_id=field_id,
                    raw_value=field["value"],
                    suggested_value=field["value"],
                    normalized_value=field["value"],
                    validation_status="valid",
                    source_quote=quote,
                    source_page=1,
                    unit=field["unit"],
                    evidence_refs=[evidence_id],
                )
            )
        payload = ExtractionCandidate(
            document_id=case["document_id"],
            document_version=1,
            document_type=kind,
            extraction_version="1.1",
            model_config_version="test-author-gold-no-model",
            fields=fields,
            evidence_catalog=evidence,
            issues=["TEST_AUTHOR_GOLD_NOT_AN_OCR_EVALUATION"],
            page_count=store.documents[case["document_id"]]["pages"],
            verification_method="ocr_unverified",
        )
        store._apply(store.cases[case["id"]], {"stage": "extract"}, payload)
        store.save_case(store.cases[case["id"]])
        cases[kind] = case["id"]
    return cases


async def create_bundle(store, cases, *, contract=False, title="가온 2026년 10월"):
    period = author_gold()["period"]
    return await store.create_comparison(
        title,
        period_start=period["start"],
        period_end=period["end"],
        selection=selection(cases, contract=contract),
    )


async def reviewed(store, comparison_id, *, selected_count=2, payment_scope=True):
    current = await store.get_comparison(comparison_id)
    draft = copy.deepcopy(current["draft"])
    draft.update(
        worked_time_kind="actual",
        slip_time_kind="actual",
        subjects_confirmed=True,
        earnings_complete=True,
        deductions_complete=True,
        payment_scope_confirmed=payment_scope,
    )
    for index, row in enumerate(draft["material_review"]["transactions"].values()):
        row["selected"] = index < selected_count
    await store.save_draft(comparison_id, draft)
    return await store.confirm(comparison_id)


async def analyzed(store, comparison_id):
    job = await store.create_job(comparison_id, "analyze")
    await store.tasks[job["id"]]
    completed = await store.get_job(job["id"])
    assert completed["status"] == "completed", completed
    assert not completed["usage"]
    current = await store.get_comparison(comparison_id)
    return current, {item["rule_id"]: item for item in current["analysis"]["comparisons"]}
