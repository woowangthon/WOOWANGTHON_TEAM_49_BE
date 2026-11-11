"""Shared extraction vocabulary for prompts, validation, and the local review UI."""

import re

RATE_UNITS = ("KRW/hour", "KRW/month", "KRW/year", "KRW/day")
STATEMENT_ROW_ROLES = ("detail", "subtotal", "total", "excluded")
DOCUMENT_TYPES = ("contract", "attendance", "payslip", "deposit")


def canonical_document_type(document_type):
    """Keep old saved OCR selections readable without making OCR a document type."""
    return "payslip" if document_type == "payslip_ocr" else document_type


def statement_row_role(label):
    """Recognize printed rollups without guessing from a row's amount.

    Equal amounts and unfamiliar names are still separate details. These are
    review defaults only; the user can correct the role of each preserved row.
    """
    key = re.sub(r"[\s:：()·/\[\]_-]", "", label or "").casefold()
    if "소계" in key or key in {"subtotal", "부분합계", "중간합계", "과세합계", "비과세합계"}:
        return "subtotal"
    if (
        key in {"합계", "총계", "총액", "total", "grosspay", "totalearnings", "totaldeductions"}
        or key.endswith(("합계", "총계", "총액"))
        or key.startswith("총")
        and key.endswith(("지급액", "공제액"))
    ):
        return "total"
    return "detail"


def field(label, unit):
    return {"label": label, "unit": unit}


PERIOD_FIELDS = {
    "period_start": field("대상기간 시작일", "date"),
    "period_end": field("대상기간 종료일", "date"),
}
IDENTITY_FIELDS = {
    "employee_name": field("근로자 성명", "text"),
    "employee_id": field("사원번호", "text"),
    "employer_name": field("사업장명", "text"),
}
PROFILES = {
    "contract": {
        "label": "근로계약서",
        "fields": {
            "pay_type": field("임금 형태", "category"),
            "contract_rate": field("임금 금액", "rate"),
            "contract_start": field("계약 시작일", "date"),
            "contract_end": field("계약 종료일", "date"),
            "work_start": field("출근 시각", "time"),
            "work_end": field("퇴근 시각", "time"),
            "break_minutes": field("휴게 시간 (분)", "minute"),
            "scheduled_daily_minutes": field("일 소정근로시간 (분)", "minute"),
            "scheduled_weekly_minutes": field("주 소정근로시간 (분)", "minute"),
            "payday": field("지급일", "day_of_month"),
        },
        "rows": {},
    },
    "attendance": {
        "label": "근무기록",
        "fields": {
            **PERIOD_FIELDS,
            "worked_minutes": field("기재된 총 근로시간 (분)", "minute"),
            "overtime_minutes": field("기재된 연장시간 (분)", "minute"),
            "night_minutes": field("기재된 야간시간 (분)", "minute"),
            "holiday_minutes": field("기재된 휴일시간 (분)", "minute"),
            "work_days": field("기재된 근로일수", "count"),
        },
        "rows": {
            "work": {
                "label": "날짜별 근무기록",
                "fields": {
                    "date": field("근무일 (원문)", "text"),
                    "start": field("출근", "time"),
                    "end": field("퇴근", "time"),
                    "break_minutes": field("휴게 (분)", "minute"),
                    "break_interval": field("휴게 구간 (원문)", "text"),
                    "worked_minutes": field("근로 (분)", "minute"),
                    "overtime_minutes": field("연장 (분)", "minute"),
                    "night_minutes": field("야간 (분)", "minute"),
                    "holiday_minutes": field("휴일 (분)", "minute"),
                    "note": field("비고", "text"),
                },
            }
        },
    },
    "payslip": {
        "label": "급여명세서",
        "fields": {
            **PERIOD_FIELDS,
            "payment_date": field("지급일", "date"),
            "slip_rate": field("명세서 기재 단가", "rate"),
            "slip_minutes": field("명세서 기재 시간 (분)", "minute"),
            "slip_actual_minutes": field("명세서 실근로시간 (분)", "minute"),
            "slip_paid_minutes": field("명세서 지급기초·유급시간 (분)", "minute"),
            "gross": field("총지급액 (원)", "KRW"),
            "deduction": field("공제 합계 (원)", "KRW"),
            "net": field("실수령액 (원)", "KRW"),
        },
        "rows": {
            name: {
                "label": label,
                "fields": {
                    "label": field("항목명", "text"),
                    "amount": field("금액 (원)", "KRW"),
                    "calculation": field("기재된 계산 방법", "text"),
                },
            }
            for name, label in (("earning", "지급 항목"), ("deduction", "공제 항목"))
        },
    },
    "deposit": {
        "label": "입금기록",
        "fields": {
            **PERIOD_FIELDS,
            "account_holder": field("예금주", "text"),
            "account_number": field("계좌 표시", "text"),
            "bank_name": field("은행", "text"),
            "deposit_total": field("기재된 입금 합계 (원)", "KRW"),
        },
        "rows": {
            "transaction": {
                "label": "거래 내역",
                "fields": {
                    "date": field("거래일", "date"),
                    "time": field("거래시각", "time"),
                    "description": field("거래 구분", "text"),
                    "sender": field("입금자 표시", "text"),
                    "deposit_amount": field("입금액 (원)", "KRW"),
                    "withdrawal_amount": field("출금액 (원)", "KRW"),
                    "balance": field("거래 후 잔액 (원)", "KRW"),
                    "reference": field("거래 식별자", "text"),
                },
            }
        },
    },
}

CONTRACT_FIELDS = dict(PROFILES["contract"]["fields"])
for _profile in PROFILES.values():
    _profile["fields"] = {**IDENTITY_FIELDS, **_profile["fields"]}

PROFILES["payslip_ocr"] = {
    **PROFILES["payslip"],
    "label": "급여명세서 · Python OCR + AI",
}
PROFILES["auto"] = {"label": "AI 문서 유형 자동 분류", "fields": {}, "rows": {}}


def validate_document_type(document_type):
    if not isinstance(document_type, str) or document_type not in PROFILES:
        raise ValueError("지원하지 않는 문서 유형입니다.")
    return document_type


def field_definition(document_type, field_id):
    profile = PROFILES.get(document_type, {})
    definition = profile.get("fields", {}).get(field_id)
    if definition:
        return definition
    match = re.fullmatch(r"([a-z]+)_([1-9]\d{0,3})_([a-z_]+)", field_id)
    if match:
        return profile.get("rows", {}).get(match[1], {}).get("fields", {}).get(match[3])
    return None


def expected_unit(document_type, field_id):
    definition = field_definition(document_type, field_id)
    return definition["unit"] if definition and definition["unit"] != "rate" else None


def extraction_instructions(document_type):
    """Keep stable field IDs and preserve table rows in the existing scalar field contract."""
    import json

    document_type = canonical_document_type(document_type)
    validate_document_type(document_type)
    profile = (
        {kind: PROFILES[kind] for kind in DOCUMENT_TYPES}
        if document_type == "auto"
        else PROFILES[document_type]
    )
    instructions = (
        " The source is Python OCR text and may contain recognition errors, broken columns, or omitted "
        "characters. Never silently repair a number or invent a missing value. Preserve the exact OCR "
        "spelling in raw_value and quote; if a value or unit is ambiguous, keep its normalized_value "
        "null and explain the ambiguity in issues. Classify only from document content. "
        "Extract this document using the following field vocabulary (unit 'rate' means one of "
        "KRW/hour, KRW/month, KRW/year, KRW/day, only when explicit). "
        + json.dumps(profile, ensure_ascii=False)
        + " Return every summary field once; use null raw_value/normalized_value/unit, page 1 and empty "
        "quote when absent. Put summary fields in fields. Put detail rows in tables, not fields. "
        "Each table has prefix (work/earning/deduction/transaction as appropriate), columns (the exact "
        "column keys from this vocabulary), header_quote (the exact printed column/unit heading, "
        "including '기록 단위: 시간(h)' when present), and rows. Each row has a 1-based page, one exact "
        "contiguous row quote, raw_values and normalized_values arrays aligned to columns, and issues. "
        "Use null cells for absent values. Do not repeat the header in each row quote. "
        "The header_quote must occur on the same page as its rows; start a new table for a new page. "
        "Copy the complete contiguous header, including any section title between the unit line and "
        "column names. In raw_values copy each cell exactly: if the cell is 8 under an hours heading, "
        "return raw 8 and normalized 480; never append '시간(h)' to the raw cell. Do not join text cells "
        "with punctuation that is absent from the source. "
        "If a duration's unit cannot be grounded in the row or that page's heading, retain its raw "
        "value with null normalized_value and an issue instead of assuming hours or minutes. "
        "Keep every row separate in source order, including repeated dates and equal amounts. "
        "Never encode rows in JSON strings or merge rows. "
        "Use exact source text for text units. Include duration units in raw_value; normalize hours "
        "to minute. Use exact quotes containing each value. Do not sum rows to create an absent total. "
        "For employee_name, employee_id and employer_name, copy only explicit worker identity and "
        "workplace information. Never treat an employer representative as the employee, an account "
        "number as an employee ID, or a transfer sender as the employer without explicit evidence. "
        "Keep absent identity fields null. For payslips distinguish actual worked time in "
        "slip_actual_minutes from payment-basis/paid time in slip_paid_minutes. Do not combine "
        "actual work and paid leave. Use legacy slip_minutes only if a stated total's basis is "
        "unclear; leave it null when the explicit actual/paid fields already describe the total. "
    )
    if document_type == "auto":
        instructions += (
            " First classify the document as exactly one of contract, attendance, payslip, or deposit. "
            "Return the actual classification in document_type and use only that type's field and table "
            "vocabulary. A contract states agreed employment terms; attendance records actual work; "
            "a payslip lists pay and deductions; a deposit record lists bank transfers. Filename and "
            "selected type are hints only, never evidence. If mixed content or insufficient text prevents "
            "a single supported classification, return document_type 'unknown', empty fields and tables, "
            "and an issue asking the user to check or split the document. "
        )
    if document_type in ("contract", "auto"):
        instructions += (
            " For a contract return tables as [] and every contract summary field once: pay_type "
            "(category: hourly/monthly/annual/daily), contract_rate (KRW/hour, KRW/month, KRW/year, "
            "KRW/day), contract_start, contract_end (date), work_start, work_end (time HH:MM), "
            "break_minutes, scheduled_daily_minutes, scheduled_weekly_minutes (minute), payday "
            "(day_of_month: number or last_day). Each raw_value must be a short exact scalar from the "
            "quote, not the whole sentence. Preserve explicit source units in raw_value: return "
            "'8 hours' or '8시간', never just '8', when normalized_value is '480' and unit is minute. "
            "Use only the amount for monetary raw_value ('3,000,000원', not '월 3,000,000원'). For "
            "scheduled durations use '8시간' and '40시간', omitting the period labels '1일' and '1주'; "
            "keep the complete period labels in quote. A monthly amount labelled '월 통상임금: 월 "
            "3,000,000원' is monthly pay; raw_value may be the exact '월' marker with the full wage "
            "quote as evidence. Normalize only explicit Korean money multipliers (300만원=3000000), "
            "duration (1시간=60 minutes), Korean dates, and 오전/오후 clock times. Do not derive work "
            "durations from start/end clock times or convert annual pay to monthly. Unfilled checkboxes, "
            "zeros used as placeholders, example options, article numbers, statutory conditional break "
            "guidance, and percentage penalties are not filled values. If annual pay is stated but "
            "amount missing, keep annual pay_type and null contract_rate. Do not select a pay type "
            "from unselected hourly/monthly/daily options. For conflicting schedules or only relative "
            "payment dates return null and describe the issue. When an explicit payday has a holiday "
            "adjustment, extract the regular payday (매월 25일 means 25) and describe the adjustment "
            "in issues. Absent fields use page 1 and empty quote. "
        )
    if document_type in ("attendance", "auto"):
        instructions += (
            " Preserve explicit work dates, start/end, breaks and any stated work/overtime/night/holiday "
            "durations per row. Do not calculate durations from clock times or classify overtime by law. "
            "An overnight end time may be earlier than its start; do not invent the next date. "
            "Work-row date is source text, so preserve '10/01' without inventing a year. "
            "A break interval such as 12:00-13:00 belongs in break_interval, not break_minutes; "
            "do not calculate a break duration. If a printed table heading explicitly gives hours, "
            "keep raw numeric cells unchanged and normalize those durations to minutes. "
            "Do not use scheduled contract hours as actual attendance. Missing cells remain null."
        )
    if document_type in ("payslip", "auto"):
        instructions += (
            " Separate earnings and deductions and extract their individual names, amounts and printed "
            "calculation text. Preserve printed subtotal/total rows in the corresponding earning or "
            "deduction table with their exact labels so their calculation role can be reviewed; "
            "they are not additional detail amounts. Also keep explicitly printed gross, total "
            "deductions and net in the separate summary fields. Do not invent labels or row roles. "
            "Do not infer rates from amounts or missing deductions from tax rates."
        )
    if document_type in ("deposit", "auto"):
        instructions += (
            " Separate deposits, withdrawals and balances on each transaction row. A balance is never "
            "a deposit amount. Preserve a printed transaction reference if present. Do not decide "
            "whether a transfer is salary or link transactions to payslips. Do not infer missing zeros."
        )
    return instructions
