from agent.schemas.public.results import ExplanationCandidate
from agent.services.messages import RULE_LABELS

WAGE_LABELS = {
    "W-01": "기본급",
    "W-02": "주휴수당",
    "W-03": "연장근로수당",
    "W-04": "야간근로수당",
    "W-05": "휴일근로수당",
}
STATUS_LABELS = {
    "match": "자료상 일치",
    "difference": "차이 발견",
    "insufficient": "정보 부족",
    "needs_review": "검토 필요",
    "calculated": "계산 완료",
    "not_applicable": "계산 제외",
}
COMPARISON_SIDES = {
    "R-01": ("명세서 단가", "계약 단가"),
    "R-02": ("명세서 시간", "근태 시간"),
    "R-03": ("지급 세부 항목 합계", "명세서 지급총액"),
    "R-04": ("공제 세부 항목 합계", "명세서 공제액"),
    "R-05": ("지급총액에서 공제액을 뺀 금액", "명세서 실수령액"),
    "R-06": ("명세서 실수령액", "이번 급여로 확인한 입금 배분액"),
    "R-07": ("확인한 계산 임금", "연결한 명세서 항목 합계"),
}


def format_value(value, unit="KRW"):
    if value is None:
        return "미확인"
    suffix = {"KRW": "원", "KRW/hour": "원/시간", "KRW/month": "원/월", "minute": "분"}.get(unit, unit)
    return f"{value:,}{suffix}"


def allowed_facts(result):
    facts = {"summary": "자료상 일치·차이·정보 부족·검토 필요를 항목별로 확인했습니다."}
    for index, comparison in enumerate(result.comparisons):
        prefix = f"comparison_{index}"
        facts[prefix + "_rule"] = comparison.rule_id
        facts[prefix + "_status"] = {
            "match": "자료상 일치",
            "difference": "차이 발견",
            "insufficient": "정보 부족",
            "needs_review": "검토 필요",
        }[comparison.status]
        if comparison.delta is not None:
            facts[prefix + "_delta"] = format(comparison.delta, "f")
    return facts


def allowed_templates(facts):
    templates = ["{{summary}}", "확인 결과: {{summary}}"]
    for key in facts:
        if key.endswith("_status"):
            prefix = key.removesuffix("_status")
            templates.append("{{" + prefix + "_rule}} 항목은 {{" + key + "}}입니다.")
        elif key.endswith("_delta"):
            prefix = key.removesuffix("_delta")
            templates.append(
                "{{" + prefix + "_rule}}의 왼쪽 값에서 오른쪽 값을 뺀 차이는 {{" + key + "}}입니다."
            )
    return templates


def wage_formula_text(item):
    """Render the recorded operands without recomputing or inventing missing data."""
    values = item.operands
    if item.status != "calculated":
        return "조건이 확인되지 않아 계산 보류"
    if item.rule_id == "W-01":
        if {"monthly_contract_amount", "separated_fixed_allowances"} <= values.keys():
            return f"계약 월급 {values['monthly_contract_amount']}원 − 따로 계산한 포함 수당 {values['separated_fixed_allowances']}원"
        if {"base_hourly_rate", "regular_minutes"} <= values.keys():
            if "paid_holiday_minutes" in values:
                return f"계약 시급 {values['base_hourly_rate']}원 × (기본 근로 {values['regular_minutes']}분 + 유급휴일 {values['paid_holiday_minutes']}분) ÷ 60"
            return f"계약 시급 {values['base_hourly_rate']}원 × 기본 근로 {values['regular_minutes']}분 ÷ 60"
    rate = values.get("ordinary_hourly_rate")
    if rate is not None:
        rate = f"통상시급 {rate}원"
    elif {"ordinary_monthly_amount", "monthly_standard_minutes"} <= values.keys():
        rate = f"(월 통상임금 {values['ordinary_monthly_amount']}원 × 60 ÷ 월 기준시간 {values['monthly_standard_minutes']}분)"
    else:
        return "세부 산식 표시 정보 없음"
    key, label = {
        "W-02": ("weekly_rest_minutes", "주휴시간"),
        "W-03": ("overtime_minutes", "연장시간"),
        "W-04": ("night_minutes", "야간시간"),
    }.get(item.rule_id, (None, None))
    if key in values:
        multiplier = f" × {values['multiplier']}" if "multiplier" in values else ""
        return f"{rate} × {label} {values[key]}분 ÷ 60{multiplier}"
    if item.rule_id == "W-05" and {"holiday_first_minutes", "holiday_extra_minutes"} <= values.keys():
        return (
            f"{rate} × (휴일 8시간 이내 {values['holiday_first_minutes']}분 × 1.5 + "
            f"휴일 8시간 초과 {values['holiday_extra_minutes']}분 × 2) ÷ 60"
        )
    return "세부 산식 표시 정보 없음"


def result_fact_lines(result):
    """Describe only recorded calculations; a difference is never a legal finding."""
    lines = []
    for item in result.wage_section.items:
        label = WAGE_LABELS.get(item.rule_id, item.rule_id)
        if item.status == "calculated":
            lines.append(f"{label}: {format_value(item.amount)} (확인된 조건의 계산값).")
            if item.payable_amount is not None and item.payable_amount != item.amount:
                lines.append(
                    f"{label} 중 총액에 더한 금액: {format_value(item.payable_amount)}. 이미 포함된 금액은 중복해서 더하지 않았습니다."
                )
        else:
            lines.append(f"{label}: {STATUS_LABELS[item.status]}.")
    wage = result.wage_section
    if wage.items:
        if any(item.status == "calculated" for item in wage.items):
            lines.append(f"계산 가능한 항목의 소계: {format_value(wage.subtotal)}.")
        else:
            lines.append("아직 계산을 완료한 임금 항목이 없습니다.")
        if wage.scope_complete and wage.gross_total is not None:
            lines.append(f"확인된 계산 범위의 지급총액: {format_value(wage.gross_total)}.")
        else:
            lines.append("보류 또는 제외 범위가 있어 소계를 전체 지급총액으로 확정하지 않았습니다.")
    for item in result.comparisons:
        label = RULE_LABELS.get(item.rule_id, item.rule_id)
        text = f"{label}: {STATUS_LABELS[item.status]}."
        lhs_label, rhs_label = COMPARISON_SIDES.get(item.rule_id, ("왼쪽 값", "오른쪽 값"))
        if item.lhs is not None and item.rhs is not None:
            text += f" {lhs_label} {format_value(item.lhs, item.unit)}, {rhs_label} {format_value(item.rhs, item.unit)}."
        if item.delta is not None:
            text += f" 앞의 값에서 뒤의 값을 뺀 차이 {format_value(item.delta, item.unit)}."
        if "WAGE_COMPONENT_DIFFERENCE" in item.reason_codes:
            text += " 총액 차이는 0원이지만 세부 항목별 차이가 서로 상쇄되어 있습니다."
        if not item.scope_complete:
            text += " 확인된 일부 범위이며 전체 금액의 비교가 아닐 수 있습니다."
        lines.append(text)
    return lines


def template_explanation(
    result, model_config_version="deterministic-v1", prompt_version="explain-template-v1"
):
    return ExplanationCandidate(
        result_id=result.result_id,
        result_version=result.result_version,
        result_hash=result.result_hash,
        status="template_fallback",
        paragraphs=[
            "사용자가 확인한 자료와 조건을 기준으로 계산하고 대조했습니다.",
            *result_fact_lines(result),
            "자료의 차이만으로 미지급이나 법 위반을 확정하지 않습니다. 보완 질문과 비교 범위를 함께 확인해 주세요.",
        ],
        question_ids=[question.question_id for question in result.questions],
        model_config_version=model_config_version,
        prompt_version=prompt_version,
    )
