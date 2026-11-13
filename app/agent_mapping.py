"""Lossless backend / public-agent contract conversion.

Money remains a decimal string, minutes are explicit, and missing facts never
become zero or a standard work condition. Database IDs belong to the backend;
content hashes and payroll meanings belong to the public agent contract.
"""

from copy import deepcopy
from decimal import Decimal
import re

from agent.config import ENGINE_VERSION
from agent.rules.wages import default_rule_registry
from agent.schemas.public.base import Period
from agent.schemas.public.inputs import EvidenceRef, InputSnapshot, SourceRef
from agent.schemas.public.results import AnalysisResult, AnalysisResultCandidate, ExtractionCandidate
from agent.schemas.public.tasks import AnalysisJobInput
from agent.services.calculation_context import build_calculation_context
from agent.services.comparison_context import comparison_compatibility
from agent.services.extraction_profiles import IDENTITY_FIELDS
from agent.services.extraction_review import candidate_value
from agent.services.facts import COMPARISON_SIDES
from agent.services.hashing import digest, result_hash, snapshot_hash
from app.db import load
from app.errors import ApiError


SERVER_FIELDS = {
    "snapshot_id", "snapshot_hash", "input_version", "owner_id", "workplace_id", "case_id",
    "period", "authorized_sources", "rule_bundle_version", "support_manifest_version",
}
CONFIRMATION_METADATA = {"selected_transaction_ids", "subjects_confirmed", "source_identity"}
CONTENT_FIELDS = set(InputSnapshot.model_fields) - SERVER_FIELDS


def build_snapshot(confirmed, *, snapshot_id, input_version, owner_id, workplace_id,
                   case_id, period, authorized_sources, evidence_catalog=None):
    """Freeze confirmed and separately tagged extracted inputs without guessing semantics.

The caller supplies trusted identities and source permissions. This pure mapper
does not read candidates, infer wages, or change source evidence.
"""
    if not isinstance(confirmed, dict):
        raise ValueError("Confirmed input must be an object")
    unknown = set(confirmed) - CONTENT_FIELDS - CONFIRMATION_METADATA
    if unknown:
        raise ValueError("Unsupported or server-owned input fields: " + ", ".join(sorted(unknown)))
    if confirmed.get("profile") not in {"H-STD", "M-STD"}:
        raise ValueError("An explicit hourly or monthly profile is required")
    registry = default_rule_registry()
    data = {key: deepcopy(value) for key, value in confirmed.items() if key in CONTENT_FIELDS}
    if evidence_catalog is not None:
        data["evidence_catalog"] = deepcopy(evidence_catalog)
    snapshot = InputSnapshot.model_validate({
        **data,
        "snapshot_id": str(snapshot_id), "input_version": input_version,
        "owner_id": str(owner_id), "workplace_id": str(workplace_id), "case_id": str(case_id),
        "period": period, "authorized_sources": authorized_sources,
        "rule_bundle_version": registry.bundle_version,
        "support_manifest_version": registry.manifest_version,
    })
    snapshot.snapshot_hash = snapshot_hash(snapshot)
    # Validate evidence, source ownership, intervals, states and hash now, before
    # saving the immutable snapshot. The same validation runs in the real graph.
    build_calculation_context(snapshot, str(owner_id), str(case_id))
    return snapshot


def analysis_payload(snapshot):
    snapshot = InputSnapshot.model_validate(snapshot)
    return AnalysisJobInput(snapshot=snapshot, snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.snapshot_hash, rule_bundle_version=snapshot.rule_bundle_version,
        support_manifest_version=snapshot.support_manifest_version, engine_version=ENGINE_VERSION)


def confirm_result(candidate, snapshot, *, result_id, result_version, freshness, created_at):
    """Assign backend result identity only after validated content/lineage checks.

The caller must separately recheck execution lease, cancellation, ownership,
deletion and current source versions in its final write transaction.
"""
    candidate = AnalysisResultCandidate.model_validate(candidate)
    snapshot = InputSnapshot.model_validate(snapshot)
    if snapshot.snapshot_hash != snapshot_hash(snapshot):
        raise ValueError("Stored snapshot hash mismatch")
    if candidate.result_hash != result_hash(candidate):
        raise ValueError("Agent result hash mismatch")
    for field in ("owner_id", "case_id", "workplace_id", "input_version", "snapshot_id", "snapshot_hash",
                  "rule_bundle_version", "support_manifest_version"):
        if getattr(candidate, field) != getattr(snapshot, field):
            raise ValueError("Result belongs to a different snapshot: " + field)
    if candidate.engine_version != ENGINE_VERSION:
        raise ValueError("Agent engine version mismatch")
    return AnalysisResult(**candidate.model_dump(), result_id=str(result_id), result_version=result_version,
                          freshness=freshness, created_at=created_at)


def result_response(result):
    """Preserve the signed public result, adding display metadata beside it.

R-01 is statement minus contract; R-02 is statement minus actual attendance;
R-06 is statement net minus confirmed nonduplicate bank allocations.
"""
    result = AnalysisResult.model_validate(result)
    if result_hash(result) != result.result_hash:
        raise ValueError("Stored result hash mismatch")
    data = result.model_dump(mode="json")
    data["presentation"] = {
        "money_format": "decimal_string", "currency": "KRW", "time_unit": "minute",
        "delta_formula": "lhs - rhs", "calculation_status_path": "wage_section.items[].status",
        "comparison_status_path": "comparisons[].status",
        "agent_completed_means": "processing_complete; not all payroll items calculated",
        "comparisons": {
            rule_id: {"lhs_label": labels[0], "rhs_label": labels[1]}
            for rule_id, labels in COMPARISON_SIDES.items()
        },
        "payment_scope": "Only confirmed, linked, nonduplicate bank allocations; cash memos are separate",
    }
    return data


def extraction_fields(candidate):
    """Expose candidates intact; a valid candidate is still not user-confirmed."""
    data = ExtractionCandidate.model_validate(candidate).model_dump(mode="json")
    return {**data, "confirmation_required": True}


def prepare_transaction_provenance(con, workplace_id, data):
    """Resolve ledger provenance from stored source rows, never a caller's label.

DOCUMENT uses a current A candidate as its row identity. Individually confirmed
current A/B/manual field choices may supply the effective row values. Bank
references on USER_INPUT remain explicitly user-reported/confirmed identifiers.
    """
    bank_reference = data.get("bankReference")
    if bank_reference is not None and (not isinstance(bank_reference, str) or not bank_reference.strip()
                                       or len(bank_reference) > 200):
        raise ApiError(422, "INVALID_BANK_REFERENCE")
    if data.get("source", "USER_INPUT") != "DOCUMENT":
        if any(data.get(key) is not None for key in ("sourceDocumentId", "sourceCandidateId", "sourceRowId")):
            raise ApiError(422, "SOURCE_REQUIRES_DOCUMENT_KIND")
        return {"kind": "manual", "bank_reference": bank_reference.strip() if bank_reference else None,
                "verification": "user_asserted"}
    doc_id, candidate_id, row_id = (data.get(key) for key in ("sourceDocumentId", "sourceCandidateId", "sourceRowId"))
    if type(doc_id) is not int or not isinstance(candidate_id, str) or not isinstance(row_id, str) or not re.fullmatch(r"transaction_[1-9]\d{0,3}", row_id):
        raise ApiError(422, "DOCUMENT_TRANSACTION_PROVENANCE_REQUIRED")
    workplace = con.execute("SELECT user_id FROM workplaces WHERE id=? AND deleted_at IS NULL", (workplace_id,)).fetchone()
    if workplace is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    from app.integration_store import owned_document, source_ref

    doc = owned_document(con, doc_id, workplace[0])
    if doc["workplace_id"] != workplace_id:
        raise ApiError(422, "TRANSACTION_SOURCE_WORKPLACE_MISMATCH")
    ref = source_ref(con, doc_id, workplace[0])
    candidate = con.execute("SELECT * FROM extraction_candidates WHERE id=? AND document_id=?", (candidate_id, doc_id)).fetchone()
    latest = con.execute("""SELECT e.id FROM extraction_candidates e JOIN document_ocr o ON o.id=e.ocr_result_id
        WHERE e.document_id=? AND e.document_version=? AND e.method='text' AND o.content_hash=?
        ORDER BY e.created_at DESC,e.id DESC LIMIT 1""", (doc_id, doc["document_version"], ref.content_hash)).fetchone()
    reading = con.execute("""SELECT id FROM document_ocr WHERE document_id=? AND document_version=?
        AND content_hash=? ORDER BY created_at DESC,id DESC LIMIT 1""", (doc_id, doc["document_version"], ref.content_hash)).fetchone()
    if candidate is None or latest is None or candidate["id"] != latest[0] or reading is None or candidate["ocr_result_id"] != reading[0]:
        raise ApiError(409, "TRANSACTION_SOURCE_RECONFIRMATION_REQUIRED")
    payload = load(candidate["data_json"])
    if payload.get("document_type") != "deposit":
        raise ApiError(422, "TRANSACTION_SOURCE_MUST_BE_DEPOSIT")
    fields = {item["field_id"]: item for item in payload["fields"]}
    catalog = {item["evidence_id"]: item for item in payload["evidence_catalog"]}
    for review in con.execute("SELECT * FROM fields WHERE document_id=? AND status='CONFIRMED'", (doc_id,)):
        provenance = load(review["source_json"]) or {}
        if provenance.get("baseCandidateId") != candidate_id or provenance.get("documentHash") != ref.content_hash:
            continue
        if review["key"] in fields:
            fields[review["key"]] = fields[review["key"]] | {"normalized_value": load(review["value_json"]),
                "validation_status": "valid", "evidence_refs": provenance.get("evidenceRefs", [])}
        audit = con.execute("SELECT data_json FROM document_confirmations WHERE id=?", (provenance.get("confirmationId"),)).fetchone()
        if audit:
            for item in load(audit[0]).get("evidence_catalog", []):
                catalog[item["evidence_id"]] = item
    row = {key.removeprefix(row_id + "_"): value for key, value in fields.items() if key.startswith(row_id + "_")}
    values = {key: candidate_value(value) for key, value in row.items()}
    if not values.get("date") or values.get("deposit_amount") is None or not any(values.get(key) for key in ("reference", "sender", "description")):
        raise ApiError(422, "TRANSACTION_ROW_REVIEW_REQUIRED")
    if values["date"] != str(data["transactionDate"]) or Decimal(values["deposit_amount"]) != Decimal(str(data["amount"])):
        raise ApiError(422, "TRANSACTION_ROW_VALUE_MISMATCH")
    if data.get("sender") is not None and data["sender"].strip() != (values.get("sender") or "").strip():
        raise ApiError(422, "TRANSACTION_SENDER_MISMATCH")
    if data["paymentMethod"] != "BANK_TRANSFER":
        raise ApiError(422, "DOCUMENT_TRANSACTION_MUST_BE_BANK_TRANSFER")
    reference = values.get("reference")
    if bank_reference is not None and bank_reference.strip() != reference:
        raise ApiError(422, "TRANSACTION_REFERENCE_MISMATCH")
    refs = sorted({eid for field in row.values() for eid in field.get("evidence_refs", [])})
    if not refs or any(eid not in catalog for eid in refs):
        raise ApiError(422, "TRANSACTION_ROW_EVIDENCE_REQUIRED")
    source_range = None
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='review_export_documents' AND type='table'").fetchone():
        part = con.execute('SELECT start_page,end_page FROM review_export_documents WHERE document_id=?', (doc_id,)).fetchone()
        if part:
            source_range = [part['start_page'], part['end_page']]
    return {"kind": "document", "document_id": doc_id, "document_version": ref.document_version,
        "content_hash": ref.content_hash, "candidate_id": candidate_id, "ocr_result_id": reading[0],
        "source_row_id": row_id, "source_range": source_range, "bank_reference": reference, "date": values["date"],
        "transaction_amount": values["deposit_amount"], "evidence_refs": refs,
        "evidence_catalog": [catalog[eid] for eid in refs], "verification": "source_row_validated"}


def same_document_row(left, right):
    if not (left.get('kind') == right.get('kind') == 'document'
            and left.get('content_hash') == right.get('content_hash')
            and left.get('source_row_id') == right.get('source_row_id')):
        return False
    first, second = left.get('source_range'), right.get('source_range')
    return not first or not second or first[0] <= second[1] and second[0] <= first[1]


def _attach_manual_evidence(confirmed, evidence_id):
    """An authenticated confirmation gets an audit source, never guessed facts."""
    rows = [item for key in ("confirmed_fields", "earnings", "deductions", "work_records")
            for item in confirmed.get(key, [])]
    wage = confirmed.get("wage_conditions") or {}
    rows.extend(value for value in wage.values() if isinstance(value, dict) and "state" in value)
    rows.extend(item for key in ("days", "included_allowances", "statement_mappings")
                for item in wage.get(key, []))
    for row in rows:
        is_confirmed = row.get("state", "known") == "known" and row.get("confirmed", True)
        if is_confirmed and not row.get("evidence_refs"):
            row["evidence_refs"] = [evidence_id]


def _validate_evidence_catalog(con, confirmed, sources):
    """Clients may attest manual citations, but cannot mint AI/OCR verification."""
    allowed = {source.source_id: source for source in sources}
    stored = {}
    for source in sources:
        for row in con.execute("""SELECT e.data_json FROM extraction_candidates e
            JOIN document_ocr o ON o.id=e.ocr_result_id WHERE e.document_id=?
            AND e.document_version=? AND o.content_hash=?""",
            (source.document_id, source.document_version, source.content_hash)):
            for evidence in (load(row[0]) or {}).get("evidence_catalog", []):
                stored.setdefault(evidence["evidence_id"], set()).add(digest(evidence))
        for row in con.execute("""SELECT data_json FROM document_confirmations
            WHERE document_id=? AND document_version=? AND document_hash=? AND event='selected'""",
            (source.document_id, source.document_version, source.content_hash)):
            for evidence in (load(row[0]) or {}).get("evidence_catalog", []):
                stored.setdefault(evidence["evidence_id"], set()).add(digest(evidence))
    seen = set()
    for raw in confirmed.get("evidence_catalog", []):
        evidence = EvidenceRef.model_validate(raw)
        source = allowed.get(evidence.source_id)
        if source is None or evidence.evidence_id in seen:
            raise ApiError(422, "UNAUTHORIZED_OR_DUPLICATE_EVIDENCE")
        seen.add(evidence.evidence_id)
        doc = con.execute("SELECT page_count FROM documents WHERE id=?", (source.document_id,)).fetchone()
        if evidence.page is None or evidence.page < 1 or (doc[0] is not None and evidence.page > doc[0]):
            raise ApiError(422, "EVIDENCE_PAGE_OUT_OF_RANGE")
        if evidence.verification_method != "manual_confirmed" and digest(evidence) not in stored.get(evidence.evidence_id, set()):
            raise ApiError(422, "UNTRUSTED_EVIDENCE_VERIFICATION")


def _source_summaries(con, selected):
    """Read candidate subject metadata for compatibility, without confirming it."""
    summaries = []
    for item in selected:
        doc = con.execute("SELECT * FROM documents WHERE id=?", (item["document_id"],)).fetchone()
        row = con.execute("""SELECT data_json FROM extraction_candidates WHERE document_id=?
            AND document_version=? AND method='text' ORDER BY created_at DESC,id DESC LIMIT 1""",
            (doc["id"], doc["document_version"])).fetchone()
        candidate = load(row[0]) if row else {}
        fields = {value["field_id"]: value for value in candidate.get("fields", [])}
        # User corrections are a separate projection. Read only still-current
        # confirmations; neither candidates nor document bytes are overwritten.
        for review in con.execute("SELECT * FROM fields WHERE document_id=? AND status='CONFIRMED'",
                                  (doc["id"],)):
            provenance = load(review["source_json"]) or {}
            if provenance.get("documentVersion") == doc["document_version"] and review["key"] in fields:
                fields[review["key"]] = fields[review["key"]] | {
                    "normalized_value": load(review["value_json"]), "validation_status": "valid",
                    "unit": provenance.get("unit") or fields[review["key"]].get("unit"),
                    "evidence_refs": provenance.get("evidenceRefs", []),
                }
        def value(key):
            return candidate_value(fields.get(key, {}))
        kind = candidate.get("document_type") or doc["document_type"].lower()
        identity = {key: value(key) for key in IDENTITY_FIELDS}
        if kind == "deposit" and not identity["employee_name"]:
            identity["employee_name"] = value("account_holder")
        summaries.append({
            "case_id": str(doc["id"]), "document_id": str(doc["id"]), "title": doc["file_name"],
            "document_type": kind, "identity": identity,
            **{key: value(key) for key in ("period_start", "period_end", "contract_start", "contract_end")},
            "summary": {"contract_unit": fields.get("contract_rate", {}).get("unit"),
                        "slip_unit": fields.get("slip_rate", {}).get("unit")},
        })
    return summaries


def _preserve_source_conflicts(confirmed, compatibility, summaries):
    """Scope explicit source-period conflicts to affected fields, retaining other checks."""
    conflicted = {source_id for issue in compatibility["issues"]
                  if issue["code"] == "PERIOD_MISMATCH" for source_id in issue["source_case_ids"]}
    evidence = {item["evidence_id"]: item for item in confirmed.get("evidence_catalog", [])}
    conditions = {item["field_id"]: item for item in confirmed.get("condition_states", [])}
    kinds = {source["case_id"]: source["document_type"] for source in summaries}
    period_fields = {
        "contract": {"contract_rate"}, "attendance": {"worked_minutes"},
        "payslip": {"slip_rate", "slip_minutes", "gross", "deduction", "net"},
        "payslip_ocr": {"slip_rate", "slip_minutes", "gross", "deduction", "net"},
    }
    unit_conflict = any(issue["code"] == "UNIT_MISMATCH" for issue in compatibility["issues"])
    for field in confirmed.get("confirmed_fields", []):
        source_ids = {evidence[ref]["source_id"] for ref in field.get("evidence_refs", []) if ref in evidence}
        affected = {"doc:" + doc_id for doc_id in conflicted
                    if "doc:" + doc_id in source_ids or field["field_id"] in period_fields.get(kinds[doc_id], set())}
        if affected or (unit_conflict and field["field_id"] in {"contract_rate", "slip_rate"}):
            conditions[field["field_id"]] = {"field_id": field["field_id"], "state": "conflict",
                "confirmed_field_ref": None, "conflicting_source_refs": sorted(affected or source_ids)}
    confirmed["condition_states"] = list(conditions.values())
    wage = confirmed.get("wage_conditions")
    if wage:
        # A broad confirmation cannot erase an explicit source-period conflict.
        # The original answers remain in saved confirmed_json; the frozen input
        # records the conflict and permits unrelated statement/payment checks.
        for doc_id in conflicted:
            condition_key = {"contract": "contract_unchanged", "attendance": "records_complete"}.get(kinds[doc_id])
            if condition_key:
                wage[condition_key] = {"state": "conflict", "value": None, "evidence_refs": []}


def group_materials(con, group_id, owner_id):
    """Return source candidates for explicit review; never auto-confirm values."""
    from app.integration_store import owned_group, source_ref

    group = owned_group(con, group_id, owner_id)
    case = con.execute("SELECT * FROM cases WHERE id=?", (group["case_id"],)).fetchone()
    selected = con.execute("SELECT * FROM group_documents WHERE group_id=? ORDER BY document_id",
                           (group_id,)).fetchall()
    from app.review_store import validate_part_selection
    validate_part_selection(con, [item['document_id'] for item in selected])
    summaries = _source_summaries(con, selected)
    candidates = []
    for selected_doc in selected:
        ref = source_ref(con, selected_doc["document_id"], owner_id, case["id"])
        rows = con.execute("""SELECT * FROM extraction_candidates WHERE document_id=?
            ORDER BY created_at,id""", (selected_doc["document_id"],)).fetchall()
        candidates.append({"source": ref.model_dump(mode="json"), "candidates": [{
            "candidate_id": row["id"], "document_version": row["document_version"],
            "ocr_result_id": row["ocr_result_id"], "method": row["method"],
            "base_candidate_id": row["base_candidate_id"], "payload": load(row["data_json"]),
            "current_document": row["document_version"] == ref.document_version,
        } for row in rows]})
    confirmed = load(group["confirmed_json"]) or {}
    return {"group_id": group_id, "version": group["version"], "sources": summaries,
        "documents": candidates, "confirmed_input": confirmed,
        "needs_review": bool(group["needs_review"]),
        "compatibility": comparison_compatibility({"sources": summaries}, {
            "subjects_confirmed": confirmed.get("subjects_confirmed", False),
            "source_identity": confirmed.get("source_identity", {}),
            "period_start": case["period_start"], "period_end": case["period_end"],
        }), "confirmation_required": True,
        "input_contract": {"schema": "agent.InputSnapshot semantic fields",
            "ledger_selection": "selected_transaction_ids", "money": "decimal string",
            "source_identity_keys": "document ID string",
            "unknown_states": ["missing", "unknown", "pending", "conflict"]}}


def snapshot_from_group(con, group_id, owner_id, snapshot_id):
    """Build from owned group state and the shared case-level allocation ledger.

This runs inside the short snapshot transaction. Multiple groups reference the
same allocation; selecting it does not consume the transaction a second time.
"""
    from app.integration_store import owned_group, source_ref

    group = owned_group(con, group_id, owner_id)
    case = con.execute("SELECT * FROM cases WHERE id=?", (group["case_id"],)).fetchone()
    if group["needs_review"]:
        raise ApiError(409, "GROUP_RECONFIRMATION_REQUIRED")
    confirmed = deepcopy(load(group["confirmed_json"]) or {})
    if not confirmed:
        raise ApiError(409, "CONFIRMED_INPUT_REQUIRED")
    unknown = set(confirmed) - CONTENT_FIELDS - CONFIRMATION_METADATA
    if unknown or "transaction_allocations" in confirmed or confirmed.get("extracted_fields"):
        raise ApiError(422, "INVALID_CONFIRMED_INPUT", details={"unsupported_fields": sorted(unknown),
            "allocation_policy": "Use selected_transaction_ids from the case allocation ledger"})
    profile = {"HOURLY": "H-STD", "MONTHLY": "M-STD"}.get(case["pay_type"])
    if profile is None or (confirmed.get("profile") and confirmed["profile"] != profile):
        raise ApiError(422, "WAGE_PROFILE_MISMATCH")
    # The case pay type is a user-supplied classification, not an assertion that
    # any standard wage condition is known or supported.
    confirmed["profile"] = profile
    period = Period(start=case["period_start"], end=case["period_end"])
    if "subjects_confirmed" in confirmed and type(confirmed["subjects_confirmed"]) is not bool:
        raise ApiError(422, "INVALID_SUBJECT_CONFIRMATION")
    identities = confirmed.get("source_identity", {})
    if not isinstance(identities, dict) or any(
        not isinstance(key, str) or not isinstance(identity, dict)
        or set(identity) - set(IDENTITY_FIELDS)
        or any(not isinstance(value, str) or len(value) > 1000 for value in identity.values())
        for key, identity in identities.items()
    ):
        raise ApiError(422, "INVALID_SOURCE_IDENTITY")
    # Validate nested shapes before provenance helpers inspect them. This also
    # gives malformed work/break/condition input a 422 rather than a server error.
    parsed = InputSnapshot.model_validate({
        **{key: value for key, value in confirmed.items() if key in CONTENT_FIELDS},
        "snapshot_id": str(snapshot_id), "input_version": case["input_version"],
        "owner_id": str(owner_id), "workplace_id": str(case["workplace_id"]),
        "case_id": str(case["id"]), "period": period,
    })
    confirmed.update({key: value for key, value in parsed.model_dump(mode="json").items() if key in CONTENT_FIELDS})
    selected = con.execute("SELECT * FROM group_documents WHERE group_id=? ORDER BY document_id",
                           (group_id,)).fetchall()
    from app.review_store import validate_part_selection
    validate_part_selection(con, [item['document_id'] for item in selected])
    if set(identities) - {str(item["document_id"]) for item in selected}:
        raise ApiError(422, "SOURCE_IDENTITY_NOT_SELECTED")
    sources = []
    for item in selected:
        ref = source_ref(con, item["document_id"], owner_id, case["id"])
        if ref.document_version != item["document_version"] or ref.link_version != item["link_version"]:
            raise ApiError(409, "SOURCE_RECONFIRMATION_REQUIRED")
        sources.append(ref)
    _validate_evidence_catalog(con, confirmed, sources)
    summaries = _source_summaries(con, selected)
    compatibility = comparison_compatibility({"sources": summaries}, {
        "subjects_confirmed": confirmed.get("subjects_confirmed", False),
        "source_identity": confirmed.get("source_identity", {}),
        "period_start": str(period.start), "period_end": str(period.end),
    })
    if compatibility["blocked"] or compatibility["requires_subject_confirmation"]:
        raise ApiError(409, "SOURCE_SUBJECT_REVIEW_REQUIRED", details=compatibility)
    _preserve_source_conflicts(confirmed, compatibility, summaries)
    manual_id = "manual:" + str(group_id)
    manual_evidence_id = "manual-confirmation:" + str(group_id) + ":" + str(group["version"])
    # Boundary-week attendance is explicit confirmation context, not another
    # payroll period. Do not falsely label its manual evidence case-period-only.
    confirmed_coverage = [period, *(Period.model_validate(item) for item in confirmed.get("coverage", []))]
    manual_period = Period(start=min(item.start for item in confirmed_coverage),
                           end=max(item.end for item in confirmed_coverage))
    sources.append(SourceRef(source_id=manual_id, owner_id=str(owner_id), workplace_id=str(case["workplace_id"]),
        link_id="group:" + str(group_id), link_version=group["version"], document_id=manual_id,
        document_version=group["version"], content_hash=digest(load(group["confirmed_json"])),
        applicable_period=manual_period, kind="manual"))
    manual_evidence = EvidenceRef(evidence_id=manual_evidence_id, source_id=manual_id,
        quote="인증된 사용자가 이 비교 묶음에서 확인한 수기 입력", locator="group-confirmation:" + str(group["version"]),
        verification_method="manual_confirmed")
    catalog = confirmed.setdefault("evidence_catalog", [])
    if any(item["evidence_id"] == manual_evidence_id for item in catalog):
        raise ApiError(422, "RESERVED_EVIDENCE_ID")
    catalog.append(manual_evidence.model_dump(mode="json"))
    _attach_manual_evidence(confirmed, manual_evidence_id)
    transaction_ids = confirmed.pop("selected_transaction_ids", [])
    if not isinstance(transaction_ids, list) or any(type(item) is not int for item in transaction_ids):
        raise ApiError(422, "INVALID_TRANSACTION_SELECTION")
    if len(set(transaction_ids)) != len(transaction_ids):
        raise ApiError(422, "DUPLICATE_TRANSACTION_SELECTION")
    transactions = []
    source_by_document = {source.document_id: source for source in sources if source.kind == "document"}
    for transaction_id in transaction_ids:
        row = con.execute("""SELECT t.*,a.allocated_amount,a.id AS allocation_id FROM payment_transactions t
            JOIN allocations a ON a.transaction_id=t.id WHERE a.case_id=? AND t.id=? AND t.workplace_id=?""",
            (case["id"], transaction_id, case["workplace_id"])).fetchone()
        if row is None:
            raise ApiError(422, "TRANSACTION_NOT_ALLOCATED_TO_CASE")
        total = con.execute("SELECT SUM(allocated_amount) FROM allocations WHERE transaction_id=?",
                            (transaction_id,)).fetchone()[0]
        if total > row["amount"]:
            raise ApiError(409, "ALLOCATION_EXCEEDS_TRANSACTION")
        provenance = load(row["provenance_json"]) if "provenance_json" in row.keys() else None
        provenance = provenance or {"kind": "manual", "bank_reference": None}
        refs = [manual_evidence_id]
        row_identity = None
        if row["source"] == "DOCUMENT":
            if provenance.get("kind") != "document":
                raise ApiError(422, "DOCUMENT_TRANSACTION_PROVENANCE_REQUIRED")
            ref = source_by_document.get(str(provenance.get("document_id")))
            if ref is None:
                raise ApiError(422, "TRANSACTION_SOURCE_NOT_SELECTED")
            if (provenance.get("document_version") != ref.document_version
                    or provenance.get("content_hash") != ref.content_hash
                    or provenance.get("date") != row["transaction_date"]
                    or Decimal(str(provenance.get("transaction_amount"))) != Decimal(row["amount"])):
                raise ApiError(409, "TRANSACTION_SOURCE_RECONFIRMATION_REQUIRED")
            if ref.usage == "context":
                raise ApiError(422, "CONTEXT_SOURCE_CANNOT_SUPPLY_PAYMENT")
            _validate_evidence_catalog(con, {"evidence_catalog": provenance["evidence_catalog"]}, [ref])
            existing = {item["evidence_id"]: item for item in catalog}
            for evidence in provenance["evidence_catalog"]:
                if evidence["evidence_id"] in existing and digest(existing[evidence["evidence_id"]]) != digest(evidence):
                    raise ApiError(422, "CONFLICTING_EVIDENCE_ID")
                if evidence["evidence_id"] not in existing:
                    catalog.append(evidence)
                    existing[evidence["evidence_id"]] = evidence
            refs = list(dict.fromkeys(provenance["evidence_refs"] + refs))
            row_identity = (ref.content_hash, provenance["source_row_id"])
        transactions.append(dict(row) | {"provenance": provenance, "evidence_refs": refs,
                                         "row_identity": row_identity})
    duplicate_ids = set()
    for index, left in enumerate(transactions):
        if left["confirmation_status"] != "USER_CONFIRMED" or left["payment_method"] != "BANK_TRANSFER":
            continue
        for right in transactions[index + 1:]:
            if right["confirmation_status"] != "USER_CONFIRMED" or right["payment_method"] != "BANK_TRANSFER":
                continue
            left_ref, right_ref = (item["provenance"].get("bank_reference") for item in (left, right))
            same_row = same_document_row(left["provenance"], right["provenance"])
            same_reference = bool(left_ref and right_ref and left_ref == right_ref)
            ambiguous = (left["transaction_date"] == right["transaction_date"] and left["amount"] == right["amount"]
                         and not (left_ref and right_ref and left_ref != right_ref))
            if same_row or same_reference or ambiguous:
                duplicate_ids.update((left["id"], right["id"]))
    confirmed["transaction_allocations"] = [{
        "transaction_id": str(row["id"]), "amount": str(row["allocated_amount"]),
        "transaction_amount": str(row["amount"]), "evidence_refs": row["evidence_refs"],
        "confirmed": row["confirmation_status"] == "USER_CONFIRMED", "linked": True,
        "duplicate": row["id"] in duplicate_ids,
        "kind": "bank" if row["payment_method"] == "BANK_TRANSFER" else "cash_memo",
    } for row in transactions]
    if duplicate_ids:
        confirmed["payment_scope_confirmed"] = False
    return build_snapshot(confirmed, snapshot_id=snapshot_id, input_version=case["input_version"],
        owner_id=owner_id, workplace_id=case["workplace_id"], case_id=case["id"], period=period,
        authorized_sources=sources)
