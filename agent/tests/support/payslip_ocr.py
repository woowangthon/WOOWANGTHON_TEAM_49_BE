"""Conservative rule extraction from local OCR; preserve all unparsed text for review."""

import re

from agent.services.extraction_profiles import PROFILES, statement_row_role
from agent.services.normalization import normalize_scalar

TOTALS = {
    "gross": ("총지급액", "지급총액", "지급액합계", "지급합계", "급여총액", "세전금액"),
    "deduction": ("공제합계", "총공제액", "공제총액", "공제액합계"),
    "net": ("실수령액", "차인지급액", "차감지급액", "차인지급", "실지급액"),
}
DEDUCTIONS = {
    "국민연금",
    "건강보험",
    "고용보험",
    "장기요양보험",
    "장기요양보험료",
    "건강보험료",
    "고용보험료",
    "소득세",
    "지방소득세",
    "갑근세",
    "주민세",
    "기타공제",
    "기타공제액",
}
EARNINGS = {
    "기본급",
    "기본급여",
    "식대",
    "식비",
    "상여금",
    "상여",
    "교통비",
    "차량유지비",
    "성과급",
    "연장수당",
    "야간수당",
    "휴일수당",
    "연장근로수당",
    "야간근로수당",
    "휴일근로수당",
    "직책수당",
    "직무수당",
    "근속수당",
    "주휴수당",
    "기타수당",
}
PAIR = re.compile(
    r"(?P<label>[가-힣][가-힣\s()·/]*?)\s*[:：]?\s*"
    r"(?P<amount>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+))\s*원?(?![\d,/:.\-])"
)
DATE = re.compile(r"\d{4}\s*(?:[./-]|년)\s*\d{1,2}\s*(?:[./-]|월)\s*\d{1,2}\s*일?")


def compact(value):
    return re.sub(r"[\s:：()·/]", "", value)


def extract_payslip(pages):
    fields, found = [], {}
    counts = {"earning": 0, "deduction": 0}
    issues = ["OCR_REQUIRES_USER_CONFIRMATION", "OCR_RULE_BASED_EXTRACTION"]

    def field(fid, raw, quote, page, unit, confidence):
        flags = []
        try:
            normalized = normalize_scalar(raw, unit)
        except ValueError, ArithmeticError:
            normalized = None
            flags.append("OCR_VALUE_REQUIRES_REVIEW")
        if confidence < 60:
            normalized = None
            flags.append("OCR_LOW_CONFIDENCE")
        return {
            "field_id": fid,
            "raw_value": raw,
            "normalized_value": normalized,
            "unit": unit,
            "page": page,
            "quote": quote,
            "issues": flags,
        }

    def summary(fid, item):
        previous = found.get(fid)
        if previous:
            if previous["raw_value"] != item["raw_value"]:
                previous["normalized_value"] = None
                if "OCR_CONFLICTING_VALUES" not in previous["issues"]:
                    previous["issues"].append("OCR_CONFLICTING_VALUES")
            return
        found[fid] = item
        fields.append(item)

    for page_number, page in enumerate(pages, 1):
        section = None
        for line in page["lines"]:
            quote, confidence = line["text"], line["confidence"]
            text = compact(quote)
            if text in ("지급항목", "지급내역", "지급", "수당내역"):
                section = "earning"
                continue
            if text in ("공제항목", "공제내역", "공제"):
                section = "deduction"
                continue
            dates = list(DATE.finditer(quote))
            if dates:
                if any(label in text for label in ("지급일", "지급일자", "급여지급일")) and len(dates) == 1:
                    summary(
                        "payment_date",
                        field("payment_date", dates[0][0].strip(), quote, page_number, "date", confidence),
                    )
                elif len(dates) == 2 and any(
                    label in text for label in ("대상기간", "급여기간", "산정기간", "근무기간")
                ):
                    for fid, date in zip(("period_start", "period_end"), dates):
                        summary(fid, field(fid, date[0].strip(), quote, page_number, "date", confidence))
                continue
            # Employee IDs, salary month, rate calculations and unrelated numbers are never amounts.
            if any(label in text for label in ("사번", "계좌", "주민등록", "생년월일", "계산방법", "산출식")):
                continue
            for match in PAIR.finditer(quote):
                label, amount = match["label"].strip(), match["amount"]
                tail = re.split(r"[가-힣]", quote[match.end() :], maxsplit=1)[0]
                if re.search(r"\d|[×÷*=]|\bx\b", tail, re.I):
                    # Multiple numeric columns or a calculation do not identify a single amount.
                    continue
                key = compact(label)
                total = next((fid for fid, aliases in TOTALS.items() if key in aliases), None)
                if total:
                    summary(total, field(total, amount, quote, page_number, "KRW", confidence))
                    # Preserve the printed rollup for review, while keeping its
                    # summary scalar separate. Net is not a section rollup.
                    if total == "net":
                        continue
                kind = (
                    "deduction"
                    if total == "deduction" or key in DEDUCTIONS
                    else "earning"
                    if total == "gross" or key in EARNINGS
                    else None
                )
                if kind is None and section and statement_row_role(label) != "detail":
                    kind = section
                # Unfamiliar labels only belong to a clearly separated single-column section.
                if kind is None and section and not any(c.isdigit() for c in label):
                    if not any(
                        token in key
                        for token in (
                            "합계",
                            "총액",
                            "지급액",
                            "수령",
                            "단가",
                            "시간",
                            "일수",
                            "연월",
                            "성명",
                            "소속",
                        )
                    ):
                        kind = section
                if kind:
                    counts[kind] += 1
                    prefix = f"{kind}_{counts[kind]}"
                    fields.extend(
                        [
                            field(prefix + "_label", label, quote, page_number, "text", confidence),
                            field(prefix + "_amount", amount, quote, page_number, "KRW", confidence),
                        ]
                    )
    for fid in PROFILES["payslip_ocr"]["fields"]:
        if fid not in found:
            fields.append(
                {
                    "field_id": fid,
                    "raw_value": None,
                    "normalized_value": None,
                    "unit": None,
                    "page": 1,
                    "quote": "",
                    "issues": [],
                }
            )
    if not any(item["raw_value"] is not None for item in fields):
        issues.append("OCR_LAYOUT_REQUIRES_MANUAL_REVIEW")
    return {"document_type": "payslip_ocr", "fields": fields, "issues": issues}
