"""Rebase working corrections onto refreshed candidates without trusting row indexes."""

import copy
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal

from agent.services.extraction_profiles import statement_row_role
from agent.services.extraction_review import row_requires_replacement


def same_selection(before, after):
    def key(selection):
        return (
            selection.get("contract_case_id"),
            selection.get("payslip_case_id"),
            tuple(sorted(selection.get("attendance_case_ids", []))),
            tuple(sorted(selection.get("deposit_case_ids", []))),
        )

    return key(before) == key(after)


def material_candidate(default, materials):
    """Reconstruct the untouched suggestion baseline, including older saved materials."""
    draft = copy.deepcopy(default)
    for key, value in materials["suggested"].items():
        if key in draft and value is not None:
            draft[key] = copy.deepcopy(value)
    draft["material_review"] = {
        "earnings": {row["item_id"]: row["amount"] for row in materials["earnings"]},
        "deductions": {row["item_id"]: row["amount"] for row in materials["deductions"]},
        "row_roles": {
            row["item_id"]: _role(row) for key in ("earnings", "deductions") for row in materials[key]
        },
        "manual_rows": [],
        "transactions": {
            row["transaction_id"]: {
                "selected": False,
                "amount": row["amount"],
                "transaction_amount": row["transaction_amount"],
            }
            for row in materials["transactions"]
        },
        "distinct_transactions_confirmed": False,
    }
    return draft


def _role(row):
    return row.get("row_role") or (
        "excluded"
        if row.get("excluded")
        else "total"
        if row.get("is_total")
        else statement_row_role(row.get("label"))
    )


def _source(materials, case_id):
    source = next((s for s in materials["sources"] if s["case_id"] == case_id), None)
    if source is None:
        return None
    ref = source["ref"]
    return case_id, ref["document_id"], ref["content_hash"]


def _pages(materials, row):
    catalog = {item["evidence_id"]: item for item in materials["evidence_catalog"]}
    pages = {
        catalog[ref]["page"]
        for ref in row.get("evidence_refs", [])
        if ref in catalog and isinstance(catalog[ref].get("page"), int)
    }
    return tuple(sorted(pages))


def _statement_key(materials, row):
    label = re.sub(r"\s+", " ", unicodedata.normalize("NFC", row.get("label") or "")).strip()
    source, pages = _source(materials, row.get("source_case_id")), _pages(materials, row)
    if not label or label == "항목명 확인 필요" or source is None or not pages:
        return None
    return source, label, pages


def _transaction_key(materials, row):
    account, reference = row.get("account_number"), row.get("reference")
    if account and reference:
        return "bank-reference", account, reference
    sources = tuple(
        sorted(
            source
            for case_id in row.get("source_case_ids", [row.get("source_case_id")])
            if (source := _source(materials, case_id)) is not None
        )
    )
    if not sources:
        return None
    if reference:
        return "source-reference", sources, reference
    pages, amount = _pages(materials, row), row.get("transaction_amount")
    if not row.get("date") or amount is None or not pages:
        return None
    return "source-row", sources, account, row["date"], row.get("description", ""), pages, Decimal(amount)


def _matches(before, after, group, key):
    previous, incoming = defaultdict(list), defaultdict(list)
    for materials, output in ((before, previous), (after, incoming)):
        for row in materials[group]:
            identity = key(materials, row)
            if identity is not None:
                output[identity].append(row)
    return {
        rows[0]["transaction_id" if group == "transactions" else "item_id"]: incoming[identity][0]
        for identity, rows in previous.items()
        if len(rows) == 1 and len(incoming[identity]) == 1
    }


def rebase_review(before, after, current, candidate, confirmed=None):
    """Preserve corrections only when independent source attributes identify one row."""
    result, warnings = copy.deepcopy(candidate), []
    confirmed = confirmed or {}
    unresolved = 0
    for group in ("earnings", "deductions"):
        matches = _matches(before, after, group, _statement_key)
        for row in before[group]:
            item_id = row["item_id"]
            value = current.get(group, {}).get(item_id, row["amount"])
            role = current.get("row_roles", {}).get(item_id, _role(row))
            locked = confirmed.get(group, {})
            keep_amount = value != row["amount"] or (
                value not in (None, "") and item_id in locked and locked[item_id] == value
            )
            keep_role = role != _role(row)
            if not keep_amount and not keep_role:
                continue
            target = matches.get(item_id)
            if target is None:
                unresolved += 1
                continue
            target_id = target["item_id"]
            if keep_amount:
                result[group][target_id] = value
                if value != target["amount"]:
                    warnings.append(
                        f"{row['label']}: 이전 확인·수정 금액 {value if value not in (None, '') else '미입력'}"
                        f"을 보존했습니다. 새 추출 후보 {target['amount'] if target['amount'] is not None else '미인식'}"
                        "과 원문을 확인해 주세요."
                    )
            if keep_role:
                result["row_roles"][target_id] = role
    matches = _matches(before, after, "transactions", _transaction_key)
    old_transactions = {row["transaction_id"]: row for row in before["transactions"]}
    for transaction_id, changes in current.get("transactions", {}).items():
        row = old_transactions.get(transaction_id)
        if row is None:
            unresolved += 1
            continue
        old_default = {
            "selected": False,
            "amount": row["amount"],
            "transaction_amount": row["transaction_amount"],
        }
        protected = {
            key: value
            for key, value in changes.items()
            if value != old_default.get(key)
            or key == "amount"
            and changes.get("selected")
            or key != "selected"
            and value not in (None, "")
            and confirmed.get("transactions", {}).get(transaction_id, {}).get(key) == value
        }
        if not protected:
            continue
        target = matches.get(transaction_id)
        if target is None:
            unresolved += 1
            continue
        rebased = result["transactions"][target["transaction_id"]]
        rebased.update(protected)
        if rebased.get("selected") and row_requires_replacement(target, "deposit_amount"):
            rebased["selected"] = False
            warnings.append(
                "거래일·거래 식별정보에 확인이 필요한 새 후보는 선택을 해제했습니다. 입력한 금액은 보존했습니다."
            )
        if (
            rebased.get("selected")
            and rebased.get("amount") not in (None, "")
            and rebased.get("transaction_amount") not in (None, "")
            and Decimal(rebased["amount"]) > Decimal(rebased["transaction_amount"])
        ):
            rebased["selected"] = False
            warnings.append(
                "새 입금 후보보다 이전 배분액이 커 선택을 해제했습니다. 보존한 배분액을 확인해 주세요."
            )
    old_slip, new_slip = before["selection"].get("payslip_case_id"), after["selection"].get("payslip_case_id")
    same_slip = old_slip and old_slip == new_slip and _source(before, old_slip) == _source(after, new_slip)
    slip = next((s for s in after["sources"] if s["case_id"] == new_slip), {})
    for row in current.get("manual_rows", []):
        if same_slip and isinstance(slip.get("page_count"), int) and 1 <= row["page"] <= slip["page_count"]:
            result["manual_rows"].append(copy.deepcopy(row))
        else:
            unresolved += 1
    if unresolved:
        warnings.append(
            f"이전 수정·보완 {unresolved}개 항목을 새 원문 행과 확실히 연결하지 못했습니다. "
            "이전 초안 이력에 보관했으니 원문을 확인해 다시 반영해 주세요."
        )
    # Duplicate transaction declarations apply to a reviewed candidate set.
    result["distinct_transactions_confirmed"] = False
    return result, warnings
