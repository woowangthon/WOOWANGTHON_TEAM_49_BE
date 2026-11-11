"""Version-bound development page state, independent of extraction and model calls."""

import copy
from datetime import UTC, datetime
from decimal import Decimal

from agent.schemas.public.results import AnalysisResult


def result_entry(case, result_id=None):
    current = case.get("analysis")
    if current and (not result_id or current["result_id"] == result_id):
        return {
            "analysis": current,
            "snapshot": case.get("analysis_snapshot"),
            "period": case.get("analysis_period"),
            "materials": case.get("analysis_materials"),
            "explanation": case.get("explanation"),
            "report": case.get("report"),
            "report_sources": case.get("report_sources", []),
            "workplace_name": case.get("analysis_workplace_name"),
            "confirmed": case.get("analysis_confirmed"),
        }
    for item in case.get("analysis_history", []):
        if item["analysis"]["result_id"] == result_id:
            return item
    raise ValueError("선택한 결과 버전을 찾을 수 없습니다. 결과를 먼저 실행해 주세요.")


def archive_current(case):
    """Retain artifact links before updates invalidate the latest view."""
    if not case.get("analysis"):
        return
    entry = copy.deepcopy(result_entry(case))
    history = case.setdefault("analysis_history", [])
    prior = next((h for h in history if h["analysis"]["result_id"] == entry["analysis"]["result_id"]), None)
    if prior is None:
        history.append(entry)
    else:
        for key, value in entry.items():
            if value is not None and (value or key not in prior):
                prior[key] = value


def sources_for_entry(store, entry):
    sources = (entry.get("snapshot") or {}).get("authorized_sources", [])
    if not sources:
        sources = [s["ref"] for s in (entry.get("materials") or {}).get("sources", [])]
    existing = {s["source_id"]: s for s in entry.get("report_sources", [])}
    return [
        existing.get(
            source["source_id"],
            {
                "source_id": source["source_id"],
                "document_id": source["document_id"],
                "filename": store.documents.get(source["document_id"], {}).get(
                    "title", source["document_id"]
                ),
            },
        )
        for source in sources
        if source.get("kind", "document") == "document"
    ]


def report_context(store, case, entry):
    from agent.schemas.public.tasks import ReportDisplayContext
    from agent.services.questions import condition_label

    snapshot = entry.get("snapshot")
    if not snapshot:
        return None
    conditions = []
    for field in snapshot.get("confirmed_fields", []):
        conditions.append(
            {
                "label": condition_label(field["field_id"]),
                "value": _amount(field.get("value"), field["unit"]),
                "state": field.get("state", "known"),
                "evidence_refs": field.get("evidence_refs", []),
            }
        )
        if field.get("time_kind"):
            conditions.append(
                {
                    "label": condition_label(field["field_id"]) + "의 시간 기준",
                    "value": {"actual": "실근로시간", "paid": "유급시간"}[field["time_kind"]],
                    "state": "known",
                }
            )
    wage = snapshot.get("wage_conditions") or {}
    for key, value in wage.items():
        if isinstance(value, dict) and "state" in value:
            conditions.append(
                {
                    "label": condition_label(key),
                    "value": _condition_value(key, value.get("value")),
                    "state": value["state"],
                    "evidence_refs": value.get("evidence_refs", []),
                }
            )
    conditions.extend(_calendar_conditions(wage.get("days", [])))
    from agent.services.facts import WAGE_LABELS

    for allowance in wage.get("included_allowances", []):
        conditions.append(
            {
                "label": "이미 포함된 수당: " + WAGE_LABELS.get(allowance["rule_id"], allowance["rule_id"]),
                "value": _amount(allowance.get("amount"), "KRW"),
                "state": allowance["state"],
                "evidence_refs": allowance.get("evidence_refs", []),
            }
        )
    for mapping in wage.get("statement_mappings", []):
        period = mapping["period"]
        conditions.append(
            {
                "label": "계산·명세서 대응 범위: "
                + ", ".join(WAGE_LABELS.get(rule, rule) for rule in mapping["rule_ids"]),
                "value": f"{period['start']} ~ {period['end']} / 명세서 항목: {', '.join(mapping['statement_item_ids'])}",
                "state": mapping["state"],
                "evidence_refs": mapping.get("evidence_refs", []),
            }
        )
    if wage.get("calculation_period"):
        period = wage["calculation_period"]
        conditions.append(
            {"label": "임금 계산 적용기간", "value": f"{period['start']} ~ {period['end']}", "state": "known"}
        )
    return ReportDisplayContext(
        snapshot_id=snapshot["snapshot_id"],
        snapshot_hash=snapshot["snapshot_hash"],
        workplace_name=workplace_name(case, entry),
        period=snapshot["period"],
        conditions=conditions,
        sources=[
            {"source_id": source["source_id"], "filename": source["filename"]}
            for source in sources_for_entry(store, entry)
        ],
    )


def workplace_name(case, entry):
    """Use the name confirmed for this result, never the comparison title/current draft."""
    if entry.get("workplace_name"):
        return entry["workplace_name"]
    confirmed = entry.get("confirmed") or {}
    overrides = confirmed.get("source_identity", {})
    names = set()
    for source in (entry.get("materials") or {}).get("sources", []):
        name = (
            overrides.get(source.get("case_id"), {}).get("employer_name")
            or source.get("identity", {}).get("employer_name")
            or ""
        ).strip()
        if name:
            names.add(name)
    if not names:
        names.update(
            value["employer_name"].strip()
            for value in overrides.values()
            if value.get("employer_name", "").strip()
        )
    return " · ".join(sorted(names)) or "사업장 이름 미확인"


def _amount(value, unit):
    if value is None:
        return "미확인"
    label = {"KRW": "원", "KRW/hour": "원/시간", "KRW/month": "원/월", "minute": "분"}.get(unit, unit)
    return f"{Decimal(str(value)):,}{label}"


def _condition_value(key, value):
    if value is None:
        return "미확인"
    if isinstance(value, bool):
        return "예" if value else "아니요"
    if key == "schedule":
        weekdays = ("월", "화", "수", "목", "금", "토", "일")
        days = ", ".join(f"{weekdays[day['weekday']]}요일 {day['minutes']}분" for day in value["days"])
        return (
            f"{days}; 주 시작 {weekdays[value['week_start']]}요일; "
            f"주휴일 {weekdays[value['weekly_rest_weekday']]}요일 {value['weekly_rest_minutes']}분"
        )
    if key in {"ordinary_hourly_rate", "ordinary_monthly_amount"}:
        return _amount(value, "KRW/hour" if key == "ordinary_hourly_rate" else "KRW/month")
    if key == "monthly_standard_minutes":
        return _amount(value, "minute")
    return str(value)


def _calendar_conditions(days):
    kinds = {
        "workday": "근무일",
        "unpaid_rest": "무급 휴무일",
        "paid_holiday": "유급휴일",
        "weekly_rest": "주휴일",
    }
    attendance = {
        "present": "출근",
        "absent": "결근",
        "paid_leave": "유급휴가",
        "not_scheduled": "예정된 근무 없음",
        "unknown": "출근 여부 모름",
        "pending": "출근 확인 예정",
        "conflict": "출근 자료 충돌",
    }
    groups = {}
    for day in days:
        state = day["attendance"] if day["attendance"] in {"unknown", "pending", "conflict"} else "known"
        if not day.get("confirmed") and state == "known":
            state = "pending"
        key = day["kind"], day["attendance"], state
        group = groups.setdefault(key, {"dates": [], "refs": []})
        group["dates"].append(str(day["date"]))
        group["refs"].extend(day.get("evidence_refs", []))
    return [
        {
            "label": "날짜별 근무·휴일 분류: " + ", ".join(sorted(group["dates"])),
            "value": kinds[kind] + " / " + attendance[attended],
            "state": state,
            "evidence_refs": sorted(set(group["refs"])),
        }
        for (kind, attended, state), group in sorted(groups.items())
    ]


def generate_inquiry(case, request):
    from agent.services.inquiry import compose_inquiry

    if set(request) - {"result_id", "question_ids", "replace_edited"}:
        raise ValueError("지원하지 않는 문의 입력입니다.")
    entry = result_entry(case, request.get("result_id"))
    result = AnalysisResult.model_validate(entry["analysis"])
    drafts = case.setdefault("inquiries", {})
    previous = drafts.get(result.result_id)
    if previous and previous.get("edited"):
        if request.get("replace_edited") is not True:
            return previous
        case.setdefault("inquiry_history", []).append(copy.deepcopy(previous))
    draft = compose_inquiry(
        result,
        request.get("question_ids", []),
        workplace_name=workplace_name(case, entry),
        period=entry.get("period") or (entry.get("snapshot") or {}).get("period"),
    )
    draft.update(edited=False, updated_at=datetime.now(UTC).isoformat())
    drafts[result.result_id] = draft
    return draft


def save_inquiry(case, request):
    if set(request) - {"text", "result_id"}:
        raise ValueError("지원하지 않는 문의 입력입니다.")
    text = request.get("text")
    if not isinstance(text, str) or len(text) > 30000:
        raise ValueError("문의는 30,000자 이내로 작성해 주세요.")
    entry = result_entry(case, request.get("result_id"))
    rid = entry["analysis"]["result_id"]
    prior = case.setdefault("inquiries", {}).get(rid)
    if prior is None:
        prior = generate_inquiry(case, {"result_id": rid, "question_ids": []})
    case.setdefault("inquiry_history", []).append(copy.deepcopy(prior))
    prior.update(text=text, edited=True, updated_at=datetime.now(UTC).isoformat())
    return prior
