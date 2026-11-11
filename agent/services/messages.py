REASONS = {
    "RULE_UNVERIFIED": "이 항목은 자동 임금 계산을 아직 제공하지 않습니다.",
    "WAGE_MAPPING_OR_RULE_UNVERIFIED": "계산 임금과 명세서 항목의 대응을 검토해야 합니다.",
    "FIELD_MISSING": "비교에 필요한 확인값을 입력해 주세요.",
    "FIELD_UNKNOWN": "모르는 항목의 원본 자료를 확인해 주세요.",
    "FIELD_PENDING": "확인 예정인 항목을 확인해 주세요.",
    "FIELD_CONFLICT": "서로 다른 자료의 값이 달라 확인이 필요합니다.",
    "UNIT_MISMATCH": "비교하는 값의 단위를 맞춰 주세요.",
    "PERIOD_MISMATCH": "비교하는 자료의 적용기간을 확인해 주세요.",
    "SOURCE_PERIOD_MISMATCH": "연결한 출처의 적용기간이 비교할 값의 기간을 포함하는지 확인해 주세요.",
    "TIME_MEANING_UNCONFIRMED": "실근로시간과 유급시간 중 어떤 시간인지 확인해 주세요.",
    "WORK_SOURCE_CONFLICT": "서로 겹치는 근무 기록을 확인해 주세요.",
    "WORK_SUMMARY_MISMATCH": "급여기간의 상세 실근로시간 합계와 확인한 근태 시간 요약이 다릅니다.",
    "WORK_BREAK_UNCONFIRMED": "상세 근무 기록의 휴게 위치와 겹치는 휴게 구간을 확인해 주세요.",
    "ITEM_SCOPE_INCOMPLETE": "빠진 세부 항목이 없는지 확인해 주세요.",
    "DUPLICATE_STATEMENT_ITEM": "중복된 명세서 항목을 확인해 주세요.",
    "PAYMENT_SCOPE_UNCONFIRMED": "이 급여에 해당하는 입금 기록과 배분액을 확인해 주세요.",
    "DUPLICATE_TRANSACTION": "중복 연결된 입금 기록을 확인해 주세요.",
    "INVALID_ALLOCATION": "입금액을 넘거나 음수인 배분액을 수정해 주세요.",
    "CONDITION_CONFLICT": "계산에 필요한 근로조건이 서로 달라 확인이 필요합니다.",
    "REQUIRED_INPUT_MISSING": "계산에 필요한 조건이나 기간 전후 자료를 확인해 주세요.",
    "WAGE_COMPONENT_DIFFERENCE": "총액 차이는 0원이지만 세부 항목의 차이가 서로 상쇄되어 있습니다. 항목별 금액을 확인해 주세요.",
    "HOLIDAY_BOUNDARY_REVIEW": "자정을 넘는 근무 중 휴일의 구분이 달라져 원문 기록과 적용 기준을 확인해야 합니다.",
}

RULE_FIELDS = {
    "R-01": ["contract_rate", "slip_rate"],
    "R-02": ["worked_minutes", "slip_minutes"],
    "R-03": ["earnings", "gross"],
    "R-04": ["deductions", "deduction"],
    "R-05": ["gross", "deduction", "net"],
    "R-06": ["net", "transaction_allocations"],
}

RULE_LABELS = {
    "R-01": "계약 단가",
    "R-02": "근무시간",
    "R-03": "지급 합계",
    "R-04": "공제 합계",
    "R-05": "실수령액",
    "R-06": "연결 입금",
    "R-07": "계산 임금",
}


def reason_text(code):
    return REASONS.get(code, "해당 항목의 자료와 조건을 추가로 확인해 주세요.")
