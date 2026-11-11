"""Deterministic, condition-centred questions shared across affected rules."""

from agent.schemas.public.base import Period
from agent.schemas.public.results import Question
from agent.services.hashing import digest
from agent.services.messages import RULE_FIELDS, reason_text

CONDITION_LABELS = {
    "standard_employment": "지원하는 표준 근로형태인지",
    "contract_unchanged": "급여기간 중 계약 변경 여부",
    "five_or_more_workers": "상시근로자 5명 이상 여부",
    "full_period_employment": "급여기간 전체 재직 여부",
    "records_complete": "근무 기록의 누락 여부",
    "composition_complete": "급여 구성 항목 확인",
    "monthly_weekly_rest_included": "월급에 주휴수당 포함 여부",
    "ordinary_hourly_rate": "통상시급",
    "ordinary_monthly_amount": "월 통상임금",
    "monthly_standard_minutes": "월 기준시간(분)",
    "schedule": "약속한 근무 요일·시간과 주휴일",
    "days": "날짜별 근무·휴일·출근 상태",
    "included_allowances": "이미 포함된 수당의 금액과 항목",
    "statement_mappings": "계산 항목과 명세서 항목의 연결",
    "calculation_period": "계산할 급여기간",
    "contract_rate": "계약 단가",
    "slip_rate": "명세서 단가",
    "worked_minutes": "근태 실근로시간",
    "slip_minutes": "명세서 시간",
    "earnings": "세부 지급 항목",
    "deductions": "세부 공제 항목",
    "gross": "지급총액",
    "deduction": "공제액",
    "net": "실수령액",
    "transaction_allocations": "이번 급여에 해당하는 입금·배분액",
    "wage_conditions": "임금 계산에 필요한 근로조건",
    "coverage": "누락 없이 확인한 근무 기록의 기간",
    "work_records": "상세 근무 기록",
    "comparison_scope": "자료의 비교 범위",
    "monthly_contract_amount": "계약 월급",
    "separated_fixed_allowances": "따로 계산한 포함 수당",
    "base_hourly_rate": "계약 시급",
    "regular_minutes": "기본급에 포함한 근로시간(분)",
    "paid_holiday_minutes": "기본급에 포함한 유급휴일 시간(분)",
    "weekly_rest_minutes": "주휴시간(분)",
    "weekly_scheduled_minutes": "주 소정근로시간(분)",
    "overtime_minutes": "연장근로시간(분)",
    "night_minutes": "야간근로시간(분)",
    "holiday_first_minutes": "휴일 8시간 이내 근로시간(분)",
    "holiday_extra_minutes": "휴일 8시간 초과 근로시간(분)",
    "multiplier": "계산 배율",
    "multipliers": "구간별 계산 배율",
    "included_in_monthly_base": "월급에 포함됨",
    "overlaps_other_work": "다른 근로시간과 중복되는 시간대",
    "amount_meaning": "산정액의 의미",
    "minutes": "시간(분)",
}

_REASONS = {
    "WAGE_CONDITIONS_MISSING": "임금 계산에 필요한 근로조건이 아직 확인되지 않았습니다.",
    "CONDITION_MISSING": "계산에 필요한 조건이 비어 있어 관련 항목만 계산을 보류했습니다.",
    "RULE_PERIOD_UNSUPPORTED": "현재 검증된 계산 기준의 적용기간 밖이어서 자동 계산을 보류했습니다.",
    "CONDITION_UNKNOWN": "아직 모르는 조건이 있어 관련 항목만 계산을 보류했습니다.",
    "CONDITION_PENDING": "확인 예정인 조건이 있어 관련 항목만 계산을 보류했습니다.",
    "CONDITION_CONFLICT": "서로 다른 조건이 있어 어느 조건을 적용할지 확인해야 합니다.",
    "STANDARD_PROFILE_UNSUPPORTED": "현재 지원하는 표준 근로형태와 맞는지 확인이 필요합니다.",
    "CONTRACT_CHANGED": "기간 중 계약이 달라졌다면 같은 조건으로 전체 기간을 계산할 수 없습니다.",
    "WORK_RECORDS_INCOMPLETE": "근무 기록이 빠졌는지 확인하지 않아 시간 계산을 확정할 수 없습니다.",
    "WAGE_COMPOSITION_UNCONFIRMED": "수당 포함 여부를 확인해야 금액이 중복되거나 빠지지 않습니다.",
    "ORDINARY_RATE_UNCONFIRMED": "수당 계산의 기준이 되는 통상임금이 확인되지 않았습니다.",
    "MONTHLY_DIVISOR_UNCONFIRMED": "월급을 시간당 금액으로 바꿀 때 쓸 기준시간을 확인해야 합니다. 209시간을 자동 적용하지 않습니다.",
    "SCHEDULE_UNCONFIRMED": "약속한 근무 요일과 시간이 있어야 주 단위의 조건을 확인할 수 있습니다.",
    "HOLIDAY_CLASSIFICATION_MISSING": "어느 날이 휴일인지 확인해야 휴일근로를 구분할 수 있습니다.",
    "WEEK_CONTEXT_MISSING": "급여기간의 첫 주·마지막 주를 확인하려면 기간 앞뒤의 근무 자료도 필요합니다.",
    "WEEK_ATTENDANCE_UNCONFIRMED": "주휴 조건에 영향을 주는 약속한 근무일의 출근 상태가 확인되지 않았습니다.",
    "MONTHLY_PRORATION_UNSUPPORTED": "기간 중 입사·퇴사 등 월급의 일부 기간 계산은 현재 자동 지원 범위 밖입니다.",
    "INCLUDED_ALLOWANCE_UNCONFIRMED": "월급 등에 이미 포함된 수당을 확인해야 같은 금액을 두 번 더하지 않습니다.",
    "WAGE_MAPPING_UNCONFIRMED": "계산값과 같은 범위의 명세서 항목을 연결해야 의미 있게 비교할 수 있습니다.",
    "WAGE_MAPPING_SCOPE_MISMATCH": "계산 항목과 명세서 항목의 기간 또는 포함 범위가 다릅니다.",
}


def question_reason(code):
    return _REASONS.get(code, reason_text(code))


def condition_label(field_id):
    key = field_id.removeprefix("wage_conditions.")
    return CONDITION_LABELS.get(key, CONDITION_LABELS.get(key.split(".")[0], key))


def _materials(field_id):
    key = field_id.removeprefix("wage_conditions.")
    if key in {"days", "records_complete", "worked_minutes", "schedule", "coverage", "work_records"}:
        return ["근무표·출퇴근 기록", "휴일·휴가·결근을 확인할 자료"]
    if key in {"transaction_allocations"}:
        return ["입금 내역", "급여기간과 각 입금의 관계를 확인할 자료"]
    if key in {
        "earnings",
        "deductions",
        "gross",
        "deduction",
        "net",
        "statement_mappings",
        "slip_rate",
        "slip_minutes",
    }:
        return ["해당 급여기간의 급여명세서"]
    return ["근로계약서·근로조건 확인 자료", "해당 급여기간의 급여명세서"]


def _glossary(field_id):
    key = field_id.removeprefix("wage_conditions.")
    if key in {"ordinary_hourly_rate", "ordinary_monthly_amount", "monthly_standard_minutes"}:
        return ["ordinary_wage", "scheduled_hours", "paid_hours"]
    if key in {"schedule", "days", "records_complete", "worked_minutes", "slip_minutes"}:
        return ["scheduled_hours", "worked_hours", "paid_hours", "weekly_rest"]
    if key == "transaction_allocations":
        return ["net", "payroll_period", "payment_allocation"]
    return ["gross", "deduction", "net", "payroll_period"]


def build_questions(wage_items, comparisons, *, snapshot=None):
    """Merge a missing condition across rules; keep the exact requested dates.

    Comparison-only users are not asked to start an unrelated wage calculation.
    This function does not turn unknown/pending/conflicting facts into known data.
    """
    fields = {f.field_id: f for f in snapshot.confirmed_fields} if snapshot is not None else {}
    wage_requested = (
        snapshot is None
        or snapshot.wage_conditions is not None
        or ("contract_rate" in fields and bool(snapshot.work_records))
    )
    grouped = {}
    for item in [*wage_items, *comparisons]:
        is_wage = item.rule_id.startswith("W-") or item.rule_id == "R-07"
        if is_wage and not wage_requested:
            continue
        if item.status not in {"insufficient", "needs_review"}:
            continue
        codes = [
            code for code in item.reason_codes if code not in {"INCLUDED_WEEKLY_REST", "RULE_UNVERIFIED"}
        ]
        for code in codes:
            targets = list(getattr(item, "required_field_ids", []) or RULE_FIELDS.get(item.rule_id, []))
            if item.rule_id == "R-07":
                targets = ["wage_conditions.statement_mappings"]
            if (
                code in {"FIELD_MISSING", "FIELD_UNKNOWN", "FIELD_PENDING", "FIELD_CONFLICT"}
                and snapshot is not None
            ):
                state = code.removeprefix("FIELD_").lower()
                matching = [
                    key
                    for key in targets
                    if (key not in fields and state == "missing")
                    or (key in fields and fields[key].state == state)
                ]
                targets = matching or targets
            if not targets:
                targets = ["wage_conditions" if is_wage else "comparison_scope"]
            periods = list(getattr(item, "required_periods", []))
            if not periods and snapshot is not None:
                periods = [snapshot.period]
            for target in targets:
                group = grouped.setdefault(target, {"codes": set(), "rules": set(), "periods": {}})
                group["codes"].add(code)
                group["rules"].add(item.rule_id)
                for period in periods:
                    period = Period.model_validate(period)
                    group["periods"][(period.start, period.end)] = period
    questions = []
    for target, group in sorted(grouped.items()):
        codes, rules = sorted(group["codes"]), sorted(group["rules"])
        questions.append(
            Question(
                question_id="q-" + digest({"field": target, "rules": rules})[:16],
                reason_code=codes[0],
                target_field_ids=[target],
                blocking_rule_ids=rules,
                text=f"{condition_label(target)}을(를) 확인해 주세요.",
                reason=" ".join(dict.fromkeys(question_reason(code) for code in codes)),
                required_materials=_materials(target),
                required_periods=[group["periods"][key] for key in sorted(group["periods"])],
                glossary_ids=_glossary(target),
            )
        )
    return questions
