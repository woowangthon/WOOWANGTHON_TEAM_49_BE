"""Source-preserving preparation of extracted documents for local comparisons.

This module never reads a PDF or calls a model. It combines saved candidates only;
reviewed confirmation values and the deterministic engine remain separate concerns.
"""

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from agent.services.comparison_context import source_summary
from agent.schemas.public.base import Period
from agent.schemas.public.inputs import (
    EvidenceRef,
    InputSnapshot,
    SourceRef,
    StatementItem,
    TransactionAllocation,
)
from agent.services.extraction_profiles import (
    CONTRACT_FIELDS,
    RATE_UNITS,
    STATEMENT_ROW_ROLES,
    statement_row_role,
)
from agent.services.extraction_review import candidate_value, row_requires_replacement, validation_metadata
from agent.services.hashing import digest, snapshot_hash

SELECTION_TYPES = {
    "contract_case_id": {"contract"},
    "attendance_case_ids": {"attendance"},
    "payslip_case_id": {"payslip", "payslip_ocr"},
    "deposit_case_ids": {"deposit"},
}
LIST_KEYS = {"attendance_case_ids", "deposit_case_ids"}
MONEY_PATTERN = re.compile(r"\d{1,13}(?:\.\d{1,2})?")
ROW_PATTERN = re.compile(r"(earning|deduction|transaction)_([1-9]\d{0,3})_([a-z_]+)")


def _selection(selection):
    if not isinstance(selection, dict) or set(selection) - set(SELECTION_TYPES):
        raise ValueError("자료 선택 형식을 확인해 주세요.")
    result = {}
    for key in SELECTION_TYPES:
        value = selection.get(key, [] if key in LIST_KEYS else None)
        if key in LIST_KEYS:
            if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
                raise ValueError("자료 목록에는 사례 ID가 필요합니다.")
            result[key] = list(value)
        elif value is None or isinstance(value, str) and value:
            result[key] = value
        else:
            raise ValueError("자료 선택에는 사례 ID가 필요합니다.")
    if not any(result.values()):
        raise ValueError("대조할 자료를 하나 이상 선택해 주세요.")
    return result


def _selected(selection):
    for key in SELECTION_TYPES:
        ids = selection[key] if key in LIST_KEYS else [selection[key]]
        for case_id in ids:
            if case_id:
                yield key, case_id


def _number(value, *, nullable=True, integral=False, signed=False):
    if value is None and nullable:
        return None
    magnitude = value[1:] if signed and isinstance(value, str) and value.startswith("-") else value
    if not isinstance(magnitude, str) or not MONEY_PATTERN.fullmatch(magnitude):
        raise ValueError("금액은 올바른 숫자 문자열이어야 합니다.")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("금액 형식을 확인해 주세요.") from exc
    if (
        not parsed.is_finite()
        or not signed
        and parsed < 0
        or integral
        and parsed != parsed.to_integral_value()
    ):
        raise ValueError("시간은 0 이상의 정수 분이어야 합니다.")
    return value


def _value(fields, field_id):
    return candidate_value(fields.get(field_id, {}))


def _refs(fields, *field_ids):
    return list(
        dict.fromkeys(ref for fid in field_ids for ref in fields.get(fid, {}).get("evidence_refs", []))
    )


def _version(case, document):
    return {
        "revision": case["revision"],
        "document_type": case["document_type"],
        "document_type_revision": case.get("document_type_revision", 1),
        "extraction_revision": case.get("extraction_revision", 1),
        "document_id": case["document_id"],
        "document_hash": document["sha256"],
        "extraction_hash": digest(case["extraction"]),
    }


def _rows(fields, prefix):
    rows = {}
    for fid, field in fields.items():
        match = ROW_PATTERN.fullmatch(fid)
        if match and match[1] == prefix:
            rows.setdefault(int(match[2]), {})[match[3]] = field
    return sorted(rows.items())


def _prepare_transactions(identities, warnings):
    """Canonicalize only explicit IDs; equal dates/amounts create review hints."""
    by_id = {}
    for row, _, _ in identities:
        tid = row["transaction_id"]
        if tid in by_id:
            previous = by_id[tid]
            previous["evidence_refs"] = list(dict.fromkeys(previous["evidence_refs"] + row["evidence_refs"]))
            previous["source_case_ids"] = list(
                dict.fromkeys(previous["source_case_ids"] + row["source_case_ids"])
            )
            previous.setdefault("validation_warnings", []).extend(row.get("validation_warnings", []))
            old_amount, new_amount = previous["transaction_amount"], row["transaction_amount"]
            equal_amounts = old_amount == new_amount or (
                old_amount is not None
                and new_amount is not None
                and Decimal(old_amount) == Decimal(new_amount)
            )
            if not equal_amounts:
                previous["amount"] = previous["transaction_amount"] = None
                warnings.append(
                    "같은 계좌·거래 식별자의 입금액이 원문마다 다릅니다. 원문 입금액을 확인해 입력해 주세요."
                )
            warnings.append(
                "같은 계좌와 거래 식별자가 반복되어 하나의 입금 거래로 연결했습니다. 모든 원문 근거를 보존했습니다."
            )
        else:
            by_id[tid] = row

    groups = _duplicate_groups(list(by_id.values()))
    for row in by_id.values():
        row["duplicate_candidate_group"] = groups[row["transaction_id"]]
    if any(groups.values()):
        warnings.append(
            "같은 날짜·금액의 입금이 여러 행에 있습니다. 중복 거래인지 확인하고, 별개 거래라면 확인을 표시해 주세요."
        )
    return list(by_id.values())


def _duplicate_groups(rows):
    """Find candidate components using supplied effective original amounts."""
    result = {row["transaction_id"]: None for row in rows}
    candidates = {}
    for row in rows:
        if row["date"] and row["transaction_amount"] is not None:
            key = (row["date"], Decimal(row["transaction_amount"]))
            candidates.setdefault(key, []).append(row)
    for entries in candidates.values():
        if len(entries) < 2:
            continue
        # A missing account acts as a wildcard. Party text is deliberately not
        # an identity key: OCR can omit it or banks can print different labels.
        # Connected components retain every row and only request user review.
        parent = {row["transaction_id"]: row["transaction_id"] for row in entries}

        def root(tid):
            while parent[tid] != tid:
                parent[tid] = parent[parent[tid]]
                tid = parent[tid]
            return tid

        for index, left in enumerate(entries):
            left_id = left["transaction_id"]
            left_account, left_reference = left.get("account_number"), left.get("reference")
            for right in entries[index + 1 :]:
                right_id = right["transaction_id"]
                right_account, right_reference = right.get("account_number"), right.get("reference")
                if left_account and right_account:
                    if left_account != right_account:
                        continue
                    if left_reference and right_reference and left_reference != right_reference:
                        continue
                parent[root(right_id)] = root(left_id)
        groups = {}
        for row in entries:
            groups.setdefault(root(row["transaction_id"]), []).append(row)
        for members in groups.values():
            if len(members) < 2:
                continue
            group = "candidate-" + digest(sorted(row["transaction_id"] for row in members))
            for row in members:
                result[row["transaction_id"]] = group
    return result


def reviewed_duplicate_groups(materials, review) -> dict:
    """Recheck equal-amount candidates after original OCR amounts are corrected.

    This does not mutate saved materials or discard rows. A null correction
    leaves the source amount in candidate detection; snapshot completeness
    independently handles an explicitly unresolved original amount.
    """
    if not isinstance(review, dict):
        raise ValueError("자료 확인 형식을 확인해 주세요.")
    changes = review.get("transactions", {})
    rows = materials["transactions"]
    if not isinstance(changes, dict) or set(changes) - {row["transaction_id"] for row in rows}:
        raise ValueError("존재하지 않는 입금 거래입니다.")
    effective_rows = []
    for row in rows:
        change = changes.get(row["transaction_id"], {})
        if not isinstance(change, dict) or set(change) - {"selected", "amount", "transaction_amount"}:
            raise ValueError("입금 거래 확인 형식을 확인해 주세요.")
        corrected = change.get("transaction_amount")
        effective_rows.append(
            {
                **row,
                "transaction_amount": _number(
                    corrected if corrected is not None else row["transaction_amount"]
                ),
            }
        )
    return _duplicate_groups(effective_rows)


def build_materials(store, selection: dict) -> dict:
    """Freeze saved source candidates without summing or filling missing values."""
    selection = _selection(selection)
    result = {
        "selection": selection,
        "source_versions": {},
        "sources": [],
        "suggested": {
            "values": {},
            "manual": {},
            "contract_unit": None,
            "slip_unit": None,
            "period_start": None,
            "period_end": None,
        },
        "earnings": [],
        "deductions": [],
        "transactions": [],
        "work_rows": [],
        "evidence_catalog": [],
        "field_evidence": {},
        "field_validation": {},
        "warnings": [],
    }
    seen_hashes, evidence_by_id, source_by_id = set(), {}, {}
    source_data, transaction_identities = [], []
    for selection_key, case_id in _selected(selection):
        if case_id not in store.cases:
            raise ValueError("선택한 자료를 찾을 수 없습니다.")
        case = store.cases[case_id]
        kind = case.get("document_type")
        extraction = case.get("extraction")
        if kind not in SELECTION_TYPES[selection_key]:
            raise ValueError("선택 위치와 문서 유형이 일치하지 않습니다.")
        if not extraction or extraction.get("document_type") != kind or case.get("extraction_needs_review"):
            raise ValueError("선택한 자료를 먼저 현재 문서 유형으로 추출해 주세요.")
        document_id = case["document_id"]
        document = store.documents[document_id]
        if document["sha256"] in seen_hashes:
            raise ValueError("동일한 파일을 여러 자료로 중복 선택할 수 없습니다.")
        seen_hashes.add(document["sha256"])
        ref = SourceRef.model_validate(store.document_ref(document_id))
        ref_json = ref.model_dump(mode="json")
        if ref.source_id in source_by_id and source_by_id[ref.source_id] != ref_json:
            raise ValueError("자료 식별자가 서로 충돌합니다.")
        source_by_id[ref.source_id] = ref_json
        fields = {field["field_id"]: field for field in extraction["fields"]}
        if len(fields) != len(extraction["fields"]):
            raise ValueError("중복된 추출 필드가 있습니다.")
        local_evidence_ids = set()
        for item in extraction.get("evidence_catalog", []):
            evidence = EvidenceRef.model_validate(item)
            if evidence.source_id != ref.source_id:
                raise ValueError("추출 근거가 선택한 원문에 속하지 않습니다.")
            serialized = evidence.model_dump(mode="json")
            if evidence.evidence_id in evidence_by_id and evidence_by_id[evidence.evidence_id] != serialized:
                raise ValueError("근거 식별자가 서로 충돌합니다.")
            evidence_by_id[evidence.evidence_id] = serialized
            local_evidence_ids.add(evidence.evidence_id)
        if any(set(field.get("evidence_refs", [])) - local_evidence_ids for field in fields.values()):
            raise ValueError("원문 근거가 없는 추출 필드가 있습니다.")
        result["source_versions"][case_id] = _version(case, document)
        result["sources"].append(
            {
                **source_summary(store, case),
                "case_id": case_id,
                "document_id": document_id,
                "title": document.get("title", document_id),
                "document_type": kind,
                "ref": ref_json,
                "period_start": _value(fields, "period_start"),
                "period_end": _value(fields, "period_end"),
                "page_count": extraction.get("page_count") or document.get("pages"),
            }
        )
        source_data.append((case_id, kind, document["sha256"], fields))
        if extraction.get("verification_method") in ("vision_unverified", "ocr_unverified"):
            result["warnings"].append(
                f"{document.get('title', document_id)}: 인식한 값을 원문과 대조해 확인해 주세요."
            )

    for case_id, kind, file_hash, fields in source_data:
        source = next(item for item in result["sources"] if item["case_id"] == case_id)

        def metadata(field):
            return validation_metadata(field, document_id=source["document_id"], title=source["title"])

        def row_warnings(row, *, required=(), row_prefix=""):
            warnings = [
                metadata(field) for field in row.values() if field.get("validation_status") == "needs_review"
            ]
            for name in required:
                field = row.get(name, {})
                if candidate_value(field) is None and field.get("validation_status") != "needs_review":
                    warnings.append(
                        metadata(
                            {
                                **field,
                                "field_id": field.get("field_id", row_prefix + "_" + name),
                                "validation_status": "missing",
                                "validation_reasons": ["REQUIRED_VALUE_MISSING"],
                            }
                        )
                    )
            return warnings

        scalar_ids = []
        if kind == "contract":
            for field_id in CONTRACT_FIELDS:
                scalar_ids.append(field_id)
                value = _value(fields, field_id)
                if value is not None:
                    result["suggested"]["values"][field_id] = value
            rate_unit = fields.get("contract_rate", {}).get("unit")
            if rate_unit in RATE_UNITS:
                result["suggested"]["contract_unit"] = rate_unit
        elif kind in ("payslip", "payslip_ocr"):
            for field_id in ("slip_rate", "slip_minutes", "gross", "deduction", "net"):
                scalar_ids.append(field_id)
                value = _value(fields, field_id)
                if value is not None:
                    result["suggested"]["manual"][field_id] = _number(
                        value,
                        integral=field_id == "slip_minutes",
                        signed=field_id in ("gross", "deduction", "net"),
                    )
            rate_unit = fields.get("slip_rate", {}).get("unit")
            if rate_unit in RATE_UNITS:
                result["suggested"]["slip_unit"] = rate_unit
            actual_field = fields.get("slip_actual_minutes", {})
            actual = candidate_value(actual_field)
            if actual is not None or actual_field.get("validation_status") == "needs_review":
                result["suggested"]["manual"]["slip_minutes"] = _number(actual, integral=True)
                result["suggested"]["slip_time_kind"] = "actual" if actual is not None else ""
                result["field_evidence"]["slip_minutes"] = _refs(fields, "slip_actual_minutes")
                result["field_validation"]["slip_minutes"] = metadata(actual_field)
                scalar_ids.remove("slip_minutes")
            if "slip_paid_minutes" in fields:
                result["field_validation"]["slip_paid_minutes"] = metadata(fields["slip_paid_minutes"])
            for prefix, output_key in (("earning", "earnings"), ("deduction", "deductions")):
                for index, row in _rows(fields, prefix):
                    label = (
                        _value(row, "label") or row.get("label", {}).get("raw_value") or "항목명 확인 필요"
                    )
                    role = statement_row_role(_value(row, "label"))
                    result[output_key].append(
                        {
                            "item_id": prefix + "-" + digest([file_hash, prefix, index]),
                            "label": label,
                            "amount": _number(_value(row, "amount"), signed=True),
                            "row_role": role,
                            "is_total": role in ("subtotal", "total"),
                            "excluded": False,
                            "evidence_refs": _refs(row, *row),
                            "source_case_id": case_id,
                            "validation_warnings": row_warnings(
                                row, required=("label",), row_prefix=f"{prefix}_{index}"
                            ),
                        }
                    )
                    if role != "detail":
                        result["warnings"].append(
                            f"{label}: 소계·총계 행은 세부 항목 합계에서 제외합니다. 항목 분류를 확인해 주세요."
                        )
        elif kind == "attendance":
            if len(selection["attendance_case_ids"]) == 1:
                scalar_ids.append("worked_minutes")
                value = _value(fields, "worked_minutes")
                if value is not None:
                    result["suggested"]["manual"]["worked_minutes"] = _number(value, integral=True)
            result["field_evidence"].setdefault("worked_minutes", []).extend(_refs(fields, "worked_minutes"))
            for index, row in _rows(fields, "work"):
                result["work_rows"].append(
                    {
                        "record_id": "work-" + digest([file_hash, index]),
                        **{
                            key: _value(row, key)
                            for key in ("date", "start", "end", "break_interval", "break_minutes", "note")
                        },
                        "evidence_refs": _refs(row, *row),
                        "source_case_id": case_id,
                        "validation_warnings": row_warnings(
                            row, required=("date", "start", "end"), row_prefix=f"work_{index}"
                        ),
                    }
                )
        elif kind == "deposit":
            account = _value(fields, "account_number")
            for index, row in _rows(fields, "transaction"):
                reference = _value(row, "reference")
                transaction_key = (
                    ["bank-reference", account, reference]
                    if account and reference
                    else [file_hash, "transaction", index]
                )
                amount = _number(_value(row, "deposit_amount"))
                transaction = {
                    "transaction_id": "transaction-" + digest(transaction_key),
                    "date": _value(row, "date"),
                    "description": " · ".join(
                        value for value in (_value(row, "description"), _value(row, "sender")) if value
                    ),
                    "amount": amount,
                    "transaction_amount": amount,
                    "evidence_refs": _refs(row, *row),
                    "source_case_id": case_id,
                    "source_case_ids": [case_id],
                    "account_number": account,
                    "reference": reference,
                    "duplicate_candidate_group": None,
                    "validation_warnings": row_warnings(
                        {
                            key: value
                            for key, value in row.items()
                            if key in {"date", "description", "sender", "reference", "deposit_amount"}
                        },
                        required=("date",),
                        row_prefix=f"transaction_{index}",
                    )
                    + row_warnings({"account_number": fields.get("account_number", {})}),
                }
                transaction_identities.append((transaction, account, reference))
        for field_id in scalar_ids:
            result["field_evidence"][field_id] = _refs(fields, field_id)
            if field_id in fields:
                result["field_validation"][field_id] = metadata(fields[field_id])

    period_source = next((s for s in result["sources"] if s["case_id"] == selection["payslip_case_id"]), None)
    if period_source is None and len(selection["attendance_case_ids"]) == 1:
        period_source = next(
            s for s in result["sources"] if s["case_id"] == selection["attendance_case_ids"][0]
        )
    if period_source:
        result["suggested"]["period_start"] = period_source["period_start"]
        result["suggested"]["period_end"] = period_source["period_end"]
        period_fields = next(
            fields for case_id, _, _, fields in source_data if case_id == period_source["case_id"]
        )
        for field_id in ("period_start", "period_end"):
            if field_id in period_fields:
                result["field_validation"][field_id] = validation_metadata(
                    period_fields[field_id],
                    document_id=period_source["document_id"],
                    title=period_source["title"],
                )
    if len(selection["attendance_case_ids"]) > 1:
        result["warnings"].append(
            "여러 근무기록의 총 근로시간은 자동 합산하지 않습니다. 기간과 시간을 확인해 입력해 주세요."
        )
    result["transactions"] = _prepare_transactions(transaction_identities, result["warnings"])
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    result["evidence_catalog"] = list(evidence_by_id.values())
    return result


def materials_current(store, materials) -> bool:
    """Draft edits do not invalidate candidates; source replacement/extraction does."""
    try:
        versions = materials["source_versions"]
        if not versions or set(versions) != {
            case_id for _, case_id in _selected(_selection(materials["selection"]))
        }:
            return False
        for case_id, previous in versions.items():
            case = store.cases[case_id]
            if not case.get("extraction") or case.get("extraction_needs_review"):
                return False
            current = _version(case, store.documents[case["document_id"]])
            if any(current[key] != previous[key] for key in current if key != "revision"):
                return False
        return True
    except KeyError, TypeError, ValueError:
        return False


def _merge_models(existing, additional, model, key):
    by_id = {getattr(item, key): item for item in existing}
    for raw in additional:
        item = model.model_validate(raw)
        identity = getattr(item, key)
        if identity in by_id and by_id[identity] != item:
            raise ValueError("자료 또는 근거 식별자가 충돌합니다.")
        by_id[identity] = item
    return list(by_id.values())


def _source_period(source):
    if source.get("period_start") and source.get("period_end"):
        return Period(start=source["period_start"], end=source["period_end"])
    return None


def _manual_statement_rows(snapshot, materials, rows, manual_evidence):
    """Keep user-added rows auditable as manual input tied to a document page."""
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError("수기 보완 항목은 200개 이하의 목록이어야 합니다.")
    source = next(
        (s for s in materials["sources"] if s["case_id"] == materials["selection"]["payslip_case_id"]),
        None,
    )
    if rows and source is None:
        raise ValueError("수기 보완 항목의 급여명세서를 먼저 선택해 주세요.")
    result, seen = {"earnings": [], "deductions": []}, set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"row_id", "group", "label", "amount", "row_role", "page"}:
            raise ValueError("수기 보완 항목 형식을 확인해 주세요.")
        row_id, group, label = row.get("row_id"), row.get("group"), row.get("label")
        role, page = row.get("row_role", "detail"), row.get("page")
        if not isinstance(row_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row_id) or row_id in seen:
            raise ValueError("수기 보완 항목 식별자는 서로 달라야 합니다.")
        seen.add(row_id)
        if (
            not isinstance(group, str)
            or group not in result
            or not isinstance(label, str)
            or not label.strip()
            or len(label) > 120
        ):
            raise ValueError("수기 보완 항목의 구분과 항목명을 확인해 주세요.")
        if role not in STATEMENT_ROW_ROLES:
            raise ValueError("명세서 항목 분류를 확인해 주세요.")
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or source.get("page_count") is not None
            and page > source["page_count"]
        ):
            raise ValueError("수기 보완 항목의 원문 페이지를 확인해 주세요.")
        amount_value = row.get("amount")
        amount = _number(None if amount_value in (None, "") else amount_value, signed=True)
        evidence = EvidenceRef(
            evidence_id="manual-row-"
            + digest([source["ref"]["source_id"], row_id, label, amount, page, role]),
            source_id=source["ref"]["source_id"],
            page=page,
            quote=f"사용자 보완: {label.strip()} · {amount + '원' if amount is not None else '금액 확인 필요'}",
            locator="manual-row:" + row_id,
            verification_method="manual_confirmed",
        )
        snapshot.evidence_catalog = _merge_models(
            snapshot.evidence_catalog, [evidence], EvidenceRef, "evidence_id"
        )
        if role != "excluded":
            result[group].append(
                StatementItem(
                    item_id="manual-" + row_id,
                    amount=amount,
                    evidence_refs=list(dict.fromkeys([evidence.evidence_id, *manual_evidence])),
                    confirmed=amount is not None,
                    is_total=role in ("subtotal", "total"),
                )
            )
    return result


def _attach_periods(snapshot, materials):
    sources = materials["sources"]
    slip = next((source for source in sources if source["document_type"] in ("payslip", "payslip_ocr")), None)
    attendance = [source for source in sources if source["document_type"] == "attendance"]
    for field in snapshot.confirmed_fields:
        if field.field_id in ("slip_rate", "slip_minutes", "gross", "deduction", "net") and slip:
            field.period = _source_period(slip) or snapshot.period
        elif field.field_id == "worked_minutes" and len(attendance) == 1:
            field.period = _source_period(attendance[0]) or snapshot.period
        elif field.field_id == "worked_minutes" and len(attendance) > 1:
            periods = [_source_period(source) for source in attendance]
            complete = all(period is not None for period in periods)
            if complete:
                periods.sort(key=lambda period: period.start)
                complete = (
                    periods[0].start == snapshot.period.start
                    and periods[-1].end == snapshot.period.end
                    and all(
                        right.start == left.end + timedelta(days=1)
                        for left, right in zip(periods, periods[1:])
                    )
                )
            if not complete:
                # One validated assignment avoids a temporary known/null state.
                field_copy = field.model_copy(update={"value": None, "state": "conflict"})
                snapshot.confirmed_fields = [
                    field_copy if item is field else item for item in snapshot.confirmed_fields
                ]
            else:
                field.period = snapshot.period


def enrich_snapshot(snapshot, materials, review: dict) -> InputSnapshot:
    """Attach reviewed original rows and provenance without asserting completeness."""
    snapshot = InputSnapshot.model_validate(snapshot).model_copy(deep=True)
    if not isinstance(review, dict) or set(review) - {
        "earnings",
        "deductions",
        "transactions",
        "distinct_transactions_confirmed",
        "row_roles",
        "manual_rows",
    }:
        raise ValueError("자료 확인 형식을 확인해 주세요.")
    distinct_confirmed = review.get("distinct_transactions_confirmed", False)
    if not isinstance(distinct_confirmed, bool):
        raise ValueError("별개 입금 거래 확인 여부는 참 또는 거짓이어야 합니다.")
    snapshot.authorized_sources = _merge_models(
        snapshot.authorized_sources, [s["ref"] for s in materials["sources"]], SourceRef, "source_id"
    )
    snapshot.evidence_catalog = _merge_models(
        snapshot.evidence_catalog, materials["evidence_catalog"], EvidenceRef, "evidence_id"
    )
    for field in snapshot.confirmed_fields:
        field.evidence_refs = list(
            dict.fromkeys(field.evidence_refs + materials["field_evidence"].get(field.field_id, []))
        )
    if not materials["selection"]["contract_case_id"]:
        confirmed_rate = next(
            (field for field in snapshot.confirmed_fields if field.field_id == "slip_rate"), None
        )
        unit = confirmed_rate.unit if confirmed_rate else materials["suggested"].get("slip_unit")
        if unit in ("KRW/hour", "KRW/month"):
            snapshot.profile = "M-STD" if unit == "KRW/month" else "H-STD"
    _attach_periods(snapshot, materials)
    manual_evidence = [
        item.evidence_id
        for item in snapshot.evidence_catalog
        if item.verification_method == "manual_confirmed"
    ]
    row_roles = review.get("row_roles", {})
    source_row_ids = {row["item_id"] for key in ("earnings", "deductions") for row in materials[key]}
    if not isinstance(row_roles, dict) or set(row_roles) - source_row_ids:
        raise ValueError("존재하지 않는 명세서 항목 분류입니다.")
    if any(role not in STATEMENT_ROW_ROLES for role in row_roles.values()):
        raise ValueError("명세서 항목 분류를 확인해 주세요.")
    manual_rows = _manual_statement_rows(snapshot, materials, review.get("manual_rows", []), manual_evidence)

    for output_key in ("earnings", "deductions"):
        changes = review.get(output_key, {})
        rows = materials[output_key]
        if not isinstance(changes, dict) or set(changes) - {row["item_id"] for row in rows}:
            raise ValueError("존재하지 않는 명세서 항목입니다.")
        if materials["selection"]["payslip_case_id"]:
            items = []
            unresolved_rows = False
            for row in rows:
                amount = _number(changes.get(row["item_id"], row["amount"]), signed=True)
                role = row_roles.get(
                    row["item_id"],
                    row.get("row_role")
                    or (
                        "excluded"
                        if row.get("excluded")
                        else "total"
                        if row.get("is_total")
                        else statement_row_role(row.get("label"))
                    ),
                )
                refs = list(row["evidence_refs"])
                if row["item_id"] in changes or row["item_id"] in row_roles:
                    refs = list(dict.fromkeys(refs + manual_evidence))
                if role == "excluded":
                    continue
                if row_requires_replacement(row, "amount"):
                    # A user-entered amount cannot repair an unknown label or row alignment.
                    unresolved_rows = True
                    amount = None
                items.append(
                    StatementItem(
                        item_id=row["item_id"],
                        amount=amount,
                        evidence_refs=refs,
                        confirmed=amount is not None,
                        is_total=role in ("subtotal", "total"),
                    )
                )
            items.extend(manual_rows[output_key])
            setattr(snapshot, output_key, items)
            details = [item for item in items if not item.is_total]
            if unresolved_rows or not details or any(item.amount is None for item in details):
                setattr(snapshot, output_key + "_complete", False)

    changes = review.get("transactions", {})
    if not isinstance(changes, dict) or set(changes) - {
        t["transaction_id"] for t in materials["transactions"]
    }:
        raise ValueError("존재하지 않는 입금 거래입니다.")
    duplicate_groups = reviewed_duplicate_groups(materials, review)
    allocations, unresolved, selected_candidates = [], False, {}
    for row in materials["transactions"]:
        change = changes.get(row["transaction_id"], {})
        if not isinstance(change, dict) or set(change) - {"selected", "amount", "transaction_amount"}:
            raise ValueError("입금 거래 확인 형식을 확인해 주세요.")
        selected = change.get("selected", False)
        if not isinstance(selected, bool):
            raise ValueError("입금 선택 여부는 참 또는 거짓이어야 합니다.")
        amount = _number(change.get("amount", row["amount"]))
        original = _number(change.get("transaction_amount", row["transaction_amount"]))
        if amount is not None and original is not None and Decimal(amount) > Decimal(original):
            raise ValueError("배분 금액이 원문 입금액을 초과합니다.")
        if selected:
            if row_requires_replacement(row, "deposit_amount"):
                unresolved = True
                continue
            if duplicate_groups[row["transaction_id"]]:
                selected_candidates.setdefault(duplicate_groups[row["transaction_id"]], set()).add(
                    row["transaction_id"]
                )
            if amount is None or original is None:
                unresolved = True
                continue
            refs = list(row["evidence_refs"])
            if "transaction_amount" in change or "amount" in change:
                refs = list(dict.fromkeys(refs + manual_evidence))
            allocations.append(
                TransactionAllocation(
                    transaction_id=row["transaction_id"],
                    amount=amount,
                    transaction_amount=original,
                    evidence_refs=refs,
                )
            )
    if materials["selection"]["deposit_case_ids"]:
        snapshot.transaction_allocations = allocations
        if (
            unresolved
            or not allocations
            or (not distinct_confirmed and any(len(ids) > 1 for ids in selected_candidates.values()))
        ):
            snapshot.payment_scope_confirmed = False
    snapshot.snapshot_hash = snapshot_hash(snapshot)
    return snapshot
