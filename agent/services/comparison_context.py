"""Read-only source summaries and comparison compatibility, without model calls."""

import re
import unicodedata
from datetime import date

from agent.services.extraction_profiles import IDENTITY_FIELDS
from agent.services.extraction_review import candidate_value, validation_metadata


def source_summary(store, case):
    document_id = case["document_id"]
    document = store.documents[document_id]
    extraction = case.get("extraction") or {}
    fields = {field["field_id"]: field for field in extraction.get("fields", [])}

    def value(key):
        return candidate_value(fields.get(key, {}))

    identity = {key: value(key) for key in IDENTITY_FIELDS}
    # The named owner is usable as a name candidate; it is not proof that a
    # particular transfer belongs to the employee's pay period.
    if case.get("document_type") == "deposit" and not identity["employee_name"]:
        identity["employee_name"] = value("account_holder")
    summary = {
        key: value(key)
        for key in (
            "contract_rate",
            "pay_type",
            "worked_minutes",
            "slip_rate",
            "slip_minutes",
            "slip_actual_minutes",
            "slip_paid_minutes",
            "gross",
            "deduction",
            "net",
            "deposit_total",
        )
    }
    actual = value("slip_actual_minutes")
    # An explicit-but-unusable actual time must not fall back to an ambiguous
    # legacy total (which could include paid leave).
    explicit_actual = fields.get("slip_actual_minutes", {})
    has_actual = actual is not None or explicit_actual.get("validation_status") == "needs_review"
    if has_actual:
        summary["slip_minutes"] = actual
    summary.update(
        contract_unit=fields.get("contract_rate", {}).get("unit"),
        slip_unit=fields.get("slip_rate", {}).get("unit"),
        worked_time_kind="",
        slip_time_kind="actual" if actual is not None else "",
    )
    identity_validation = {}
    for key in IDENTITY_FIELDS:
        source_key = (
            "account_holder" if key == "employee_name" and value(key) is None and identity[key] else key
        )
        if source_key in fields:
            identity_validation[key] = validation_metadata(
                fields[source_key], document_id=document_id, title=document.get("title", document_id)
            )
    return {
        "case_id": case["id"],
        "document_id": document_id,
        "title": document.get("title", document_id),
        "document_type": case["document_type"],
        "revision": case["revision"],
        "extraction_revision": case.get("extraction_revision", 0),
        "has_extraction": bool(extraction) and not case.get("extraction_needs_review"),
        "period_start": value("period_start"),
        "period_end": value("period_end"),
        "contract_start": value("contract_start"),
        "contract_end": value("contract_end"),
        "review_needed_count": sum(
            field.get("validation_status") == "needs_review" for field in fields.values()
        ),
        "identity": identity,
        "identity_validation": identity_validation,
        "summary": summary,
    }


def list_comparison_sources(store):
    return [
        source_summary(store, case)
        for case in store.cases.values()
        if case.get("document_id") in store.documents
    ]


def _identity_key(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", value)).casefold()


def _period(start, end):
    try:
        if start and end:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if first <= last:
                return first, last
    except TypeError, ValueError:
        pass
    return None


def comparison_compatibility(materials, draft=None):
    """Explain explicit conflicts; absence never establishes subject identity."""
    draft = draft or {}
    if hasattr(draft, "model_dump"):
        draft = draft.model_dump()
    sources = (materials or {}).get("sources", [])
    issues = []

    def add(code, message, source_ids, field_ids=(), severity="review"):
        issues.append(
            {
                "code": code,
                "severity": severity,
                "blocking": severity == "blocking",
                "message": message,
                "source_case_ids": list(source_ids),
                "field_ids": list(field_ids),
            }
        )

    identities = []
    overrides = draft.get("source_identity", {})
    for source in sources:
        override = overrides.get(source["case_id"], {})
        identity = {
            key: (override.get(key) or source.get("identity", {}).get(key) or "").strip()
            for key in IDENTITY_FIELDS
        }
        identities.append((source, identity))
    for key, label in (
        ("employee_name", "근로자 성명"),
        ("employee_id", "사원번호"),
        ("employer_name", "사업장명"),
    ):
        known = [(source, identity[key]) for source, identity in identities if identity[key]]
        if len({_identity_key(value) for _, value in known}) > 1:
            names = " / ".join(dict.fromkeys(value for _, value in known))
            add(
                "SUBJECT_MISMATCH",
                f"문서의 {label}이 다릅니다: {names}. 원문을 확인해 값을 보완하거나 다른 대상의 문서를 제외해 주세요.",
                [source["case_id"] for source, _ in known],
                [key],
                "blocking",
            )
    missing = [
        source["case_id"]
        for source, identity in identities
        if not identity["employee_name"] or not identity["employer_name"]
    ]
    if missing:
        add(
            "SUBJECT_DETAILS_MISSING",
            "근로자·사업장 정보가 없는 문서가 있습니다. 원문을 열어 보완하거나 같은 대상의 자료인지 직접 확인해 주세요.",
            missing,
            IDENTITY_FIELDS,
            "info",
        )
    if sources and not draft.get("subjects_confirmed"):
        add(
            "SUBJECT_CONFIRMATION_REQUIRED",
            "선택한 문서가 같은 근로자·사업장의 자료인지 확인해 주세요.",
            [source["case_id"] for source in sources],
            severity="info",
        )
    selected_period = _period(draft.get("period_start"), draft.get("period_end"))
    for source in sources:
        kind = source["document_type"]
        if kind == "contract":
            source_period = _period(source.get("contract_start"), source.get("contract_end"))
            mismatch = (
                selected_period
                and source_period
                and not (source_period[0] <= selected_period[0] and source_period[1] >= selected_period[1])
            )
        else:
            source_period = _period(source.get("period_start"), source.get("period_end"))
            # Bank statement dates describe a query range, not a salary period.
            mismatch = (
                kind != "deposit" and selected_period and source_period and source_period != selected_period
            )
        if mismatch:
            add(
                "PERIOD_MISMATCH",
                f"{source['title']}: 문서의 적용·산정기간과 이번 대조 기간이 다릅니다. 해당 비교는 기간을 확인해야 합니다.",
                [source["case_id"]],
                ["period_start", "period_end"],
            )
    contract = next((s for s in sources if s["document_type"] == "contract"), None)
    slip = next((s for s in sources if s["document_type"] in ("payslip", "payslip_ocr")), None)
    if contract and slip:
        contract_unit = draft.get("contract_unit") or contract.get("summary", {}).get("contract_unit")
        slip_unit = draft.get("slip_unit") or slip.get("summary", {}).get("slip_unit")
        if contract_unit and slip_unit and contract_unit != slip_unit:
            add(
                "UNIT_MISMATCH",
                "계약서와 명세서의 임금 단위가 다릅니다. 시급·월급을 자동 환산하지 않으며 다른 가능한 대조는 계속할 수 있습니다.",
                [contract["case_id"], slip["case_id"]],
                ["contract_rate", "slip_rate"],
            )
    worked_kind = draft.get("worked_time_kind") or draft.get("time_kind")
    slip_kind = draft.get("slip_time_kind") or draft.get("time_kind")
    if worked_kind and slip_kind and worked_kind != slip_kind:
        add(
            "TIME_MEANING_UNCONFIRMED",
            "근무기록과 명세서의 시간 기준이 다릅니다. 실근로시간과 유급·지급기초시간은 직접 비교하지 않습니다.",
            [s["case_id"] for s in sources if s["document_type"] in ("attendance", "payslip", "payslip_ocr")],
            ["worked_minutes", "slip_minutes"],
        )
    return {
        "issues": issues,
        "blocked": any(item["blocking"] for item in issues),
        "requires_subject_confirmation": bool(sources) and not draft.get("subjects_confirmed"),
    }
