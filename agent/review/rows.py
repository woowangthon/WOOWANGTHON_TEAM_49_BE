"""Deterministic row linking: may a table row's cells be reviewed in one card?

The extraction agent already returns tables row by row (항목명·금액 in one row), so
no extra model call is needed. This check only confirms, from the stored source
text, that the cells really come from one printed row. It never confirms values.
"""

import re

ROW_FIELD = re.compile(r"^(work|earning|deduction|transaction)_(\d+)_(.+)$")
PRIMARY = {
    "earning": ("label", "amount"),
    "deduction": ("label", "amount"),
    "transaction": ("date", "deposit_amount"),
    "work": ("date",),
}
# Source-position problems make the row membership itself doubtful.
LINK_BLOCKERS = {
    "SOURCE_PAGE_INVALID",
    "SOURCE_QUOTE_MISMATCH",
    "TABLE_SOURCE_MISMATCH",
    "TABLE_SHAPE_INVALID",
    "DUPLICATE_FIELD_ID",
}
VALUE_MISMATCH = {"SOURCE_VALUE_MISMATCH", "TABLE_CELL_SOURCE_MISMATCH", "NORMALIZATION_INVALID"}


def _spaced(value):
    return r"\s*".join(re.escape(char) for char in re.sub(r"\s+", "", value))


def label_amount_adjacent(label, amount, text):
    """True when the printed label is immediately followed by the printed amount."""
    if not (label and amount and text):
        return False
    pattern = (
        r"(?<![\w가-힣])" + _spaced(label) + r"\s*[:：]?\s*(?:₩|\\)?\s*" + _spaced(amount) + r"(?![\d,.])"
    )
    return re.search(pattern, text) is not None


def _compact(value):
    return re.sub(r"\s+", "", value or "")


def _judge(prefix, cells):
    sourced = {key: f for key, f in cells.items() if f.get("raw_value")}
    if not sourced:
        if all("MANUAL" in f.get("issues", []) for f in cells.values()):
            return "manual", "직접 추가한 행입니다."
        return "unlinked", "원문에서 이 행의 값을 찾지 못했습니다."
    blocked = sorted({code for f in sourced.values() for code in f.get("issues", [])} & LINK_BLOCKERS)
    if blocked:
        return "unlinked", "원문 위치가 불확실해 칸별로 따로 확인합니다."
    pages = {(f.get("evidence") or {}).get("page") for f in sourced.values()}
    if len(pages) != 1 or None in pages:
        return "unlinked", "칸의 원문 페이지가 서로 달라 따로 확인합니다."
    texts = []
    for field in sourced.values():
        evidence = field.get("evidence") or {}
        texts.extend(t for t in (evidence.get("row_quote"), evidence.get("quote")) if t)
    primary = PRIMARY.get(prefix, ())
    if primary == ("label", "amount"):
        label, amount = sourced.get("label"), sourced.get("amount")
        if label and amount:
            if any(label_amount_adjacent(label["raw_value"], amount["raw_value"], t) for t in texts):
                return "linked", "원문에서 항목명 바로 뒤에 금액이 이어지는 같은 행입니다."
            return "unlinked", "원문에서 항목명과 금액이 붙어 있지 않아 따로 확인합니다."
        return "partial", "같은 행에서 " + ("금액" if label else "항목명") + "을 찾지 못했습니다."
    # Other tables: every sourced cell must share one source row span and appear in it.
    quotes = {(f.get("evidence") or {}).get("quote") or "" for f in sourced.values()}
    if len(quotes) != 1 or not next(iter(quotes)):
        return "unlinked", "칸마다 원문 근거가 달라 따로 확인합니다."
    quote = _compact(next(iter(quotes)))
    # A cell whose value spelling differs is flagged on that cell; its row position is still known.
    checked = [f for f in sourced.values() if not set(f.get("issues", [])) & VALUE_MISMATCH]
    if not checked or not all(_compact(f["raw_value"]) in quote for f in checked):
        return "unlinked", "일부 칸의 값이 같은 행 원문에 없어 따로 확인합니다."
    if any(key not in sourced for key in primary):
        return "partial", "같은 행에서 일부 주요 칸을 찾지 못했습니다."
    return "linked", "원문의 같은 행에서 읽은 값입니다."


def link_rows(part):
    """Return row groups in field order; fields stay individually stored and confirmed."""
    rows, order = {}, []
    for field in part.get("fields", []):
        match = ROW_FIELD.match(field.get("field_id", ""))
        if not match:
            continue
        key = (match[1], int(match[2]))
        if key not in rows:
            rows[key] = {}
            order.append(key)
        rows[key][match[3]] = field
    result = []
    for prefix, index in order:
        cells = rows[(prefix, index)]
        link, reason = _judge(prefix, cells)
        result.append(
            {
                "id": f"{prefix}_{index}",
                "prefix": prefix,
                "index": index,
                "primary": [key for key in PRIMARY.get(prefix, ()) if key in cells],
                "cells": {key: field["id"] for key, field in cells.items()},
                "link": link,
                "reason": reason,
            }
        )
    return result
