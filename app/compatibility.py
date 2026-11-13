"""Legacy Case API adapters over the real agent, with independent immutable views.

User-confirmed inputs and eligible optional extractions retain separate trust
contracts. Missing wage conditions are never invented.
"""

import re
import time
import uuid
from datetime import datetime
from decimal import Decimal

from agent.services.hashing import digest
from app.db import dump, load, now
from app.errors import ApiError

KINDS = {
    "calculation": "WAGE_CALCULATION",
    "comparison": "CONTRACT_ATTENDANCE_COMPARISON",
    "payslipValidation": "PAYSLIP_VALIDATION",
    "depositComparison": "DEPOSIT_COMPARISON",
}
IDS = {
    "calculation": "calculationId",
    "comparison": "comparisonId",
    "payslipValidation": "validationId",
    "depositComparison": "depositComparisonId",
}
STATUS = {
    "match": "MATCH",
    "difference": "DIFFERENCE",
    "insufficient": "INSUFFICIENT_INFORMATION",
    "needs_review": "REVIEW_REQUIRED",
}
FIELDS = {
    "contract_rate": "hourlyWage",
    "slip_rate": "hourlyWage",
    "gross": "grossPay",
    "deduction": "totalDeductions",
    "net": "netPay",
    "worked_minutes": "workedMinutes",
    "slip_minutes": "workedMinutes",
    "monthly_base": "monthlyBasePay",
    "base": "basePay",
}
SCALARS = {
    "CONTRACT": {"hourlyWage": ("contract_rate", "KRW/hour"), "monthlyBasePay": ("monthly_base", "KRW")},
    "PAYSLIP": {
        "grossPay": ("gross", "KRW"),
        "totalDeductions": ("deduction", "KRW"),
        "netPay": ("net", "KRW"),
        "hourlyWage": ("slip_rate", "KRW/hour"),
        "workedMinutes": ("slip_minutes", "minute"),
        "basePay": ("base", "KRW"),
    },
    "ATTENDANCE": {"workedMinutes": ("worked_minutes", "minute")},
}


def money(value):
    if value is None:
        return None
    number = Decimal(str(value))
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError("Expected an integer amount or duration")
    return int(number)


def public_field(field):
    result = dict(field)
    key = FIELDS.get(result["key"], result["key"])
    if result["key"] == "contract_rate" and (result.get("source") or {}).get("unit") == "KRW/month":
        key = "monthlyBasePay"
    unit = (result.get("source") or {}).get("unit")
    if key == "hourlyWage" and unit and unit != "KRW/hour":
        result["status"], result["value"] = "UNREADABLE", None
    result["agentKey"] = result["key"]
    result["key"] = key
    if key in {
        "hourlyWage",
        "monthlyBasePay",
        "grossPay",
        "totalDeductions",
        "netPay",
        "basePay",
        "amount",
        "workedMinutes",
    }:
        try:
            result["value"] = money(result["value"])
            result["valueType"] = "MINUTES" if key == "workedMinutes" else "MONEY_KRW"
        except ValueError, TypeError, ArithmeticError:
            result["status"] = "UNREADABLE"
            result["value"] = None
    if result["status"] in {"DEFERRED", "NEEDS_RECONFIRMATION"}:
        result["agentStatus"] = result["status"]
        result["status"] = "UNCONFIRMED"
    return result


def collect(con, case_id):
    from app.engine import effective_fields

    case = con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    groups = {"CONTRACT": [], "ATTENDANCE": [], "PAYSLIP": []}
    for row in con.execute(
        """SELECT d.*,l.applicable_from,l.applicable_to,l.purpose,l.id AS link_id,l.link_version
        FROM documents d JOIN document_links l ON d.id=l.document_id WHERE l.case_id=?
        AND d.deleted_at IS NULL ORDER BY d.id""",
        (case_id,),
    ):
        if row["document_type"] not in groups or row["purpose"] == "context" or row["processing_status"] not in {"READY", "REVIEW_REQUIRED"}:
            continue
        fields = [public_field(f) for f in effective_fields(con, case_id, row["id"])]
        scalar_keys = set(SCALARS.get(row["document_type"], {}))
        if row["document_type"] == "PAYSLIP":
            scalar_keys.update({"paymentItems", "deductionItems"})
            scalar_keys.update(f["key"] for f in fields if re.fullmatch(r"(?:earning|deduction)_[1-9]\d*_(?:label|amount)", f["key"]))
        confirmed = [f for f in fields if f["usableForAnalysis"] and f["key"] in scalar_keys]
        if not confirmed:
            continue
        narrowed = (row["applicable_from"] and row["applicable_from"] > case["period_start"]) or (
            row["applicable_to"] and row["applicable_to"] < case["period_end"]
        )
        sources = [
            {
                "documentId": row["id"],
                "fieldId": f["fieldId"],
                "key": f["key"],
                "page": (f["source"] or {}).get("page"),
                "boundingBox": (f["source"] or {}).get("boundingBox"),
                "confirmedAt": f["confirmedAt"],
                "analysisValueOrigin": f["analysisValueOrigin"],
                "sourceType": row["document_type"],
                "linkId": row["link_id"],
                "linkVersion": row["link_version"],
                "documentHash": row["sha256"],
                "documentVersion": row["document_version"],
            }
            for f in confirmed
        ]
        groups[row["document_type"]].append(
            {
                "values": {f["key"]: f["value"] for f in confirmed},
                "sources": sources,
                "conflict": bool(narrowed),
            }
        )
    for row in con.execute("SELECT * FROM manual_records WHERE case_id=? ORDER BY id", (case_id,)):
        if row["record_type"] not in groups:
            continue
        values = load(row["data_json"])
        interval = None
        if row["record_type"] == "ATTENDANCE":
            if not case["period_start"] <= values["workDate"] <= case["period_end"]:
                continue
            interval = [values["clockIn"], values["clockOut"]]
            minutes = (
                datetime.fromisoformat(values["clockOut"]) - datetime.fromisoformat(values["clockIn"])
            ).total_seconds() // 60
            values = {"workedMinutes": int(minutes) - values["breakMinutes"]}
        groups[row["record_type"]].append(
            {
                "values": values,
                "interval": interval,
                "conflict": False,
                "sources": [
                    {
                        "recordId": row["id"],
                        "source": "USER_INPUT",
                        "sourceType": row["record_type"],
                        "confirmedAt": row["created_at"],
                    }
                ],
            }
        )
    payments = [
        dict(row)
        for row in con.execute(
            """SELECT t.*,a.id AS allocation_id,a.allocated_amount
        FROM allocations a JOIN payment_transactions t ON t.id=a.transaction_id WHERE a.case_id=? ORDER BY a.id""",
            (case_id,),
        )
    ]
    return {
        "case": dict(case),
        "groups": groups,
        "conditions": load(case["conditions"]),
        "payments": payments,
    }


def fingerprint(con, case_id, kind, data=None):
    data = data or collect(con, case_id)
    body = {
        "period": [data["case"]["period_start"], data["case"]["period_end"]],
        "payType": data["case"]["pay_type"],
        "adapterVersion": "legacy-agent-3",
    }
    groups, conditions = data["groups"], data["conditions"]

    def payslip_inputs(keys, details=False):
        return [
            {**row,
             "values": {k: v for k, v in row["values"].items() if k in keys or details and re.fullmatch(r"(?:earning|deduction)_[1-9]\d*_(?:label|amount)", k)},
             "sources": [s for s in row["sources"] if s.get("key") in keys or details and re.fullmatch(r"(?:earning|deduction)_[1-9]\d*_(?:label|amount)", s.get("key", "")) or "recordId" in s]}
            for row in groups["PAYSLIP"]
        ]

    if kind == "calculation":
        body.update(
            groups={k: groups[k] for k in ("CONTRACT", "ATTENDANCE")},
            conditions={k: conditions.get(k) for k in ("base_scope_confirmed", "regular_minutes", "comparison_basis_confirmed")},
        )
    elif kind == "comparison":
        body.update(groups={**groups, "PAYSLIP": payslip_inputs({"hourlyWage", "workedMinutes", "basePay"})}, conditions={k: conditions.get(k) for k in (
            "comparison_basis_confirmed", "hours_basis_confirmed", "base_pay_basis_confirmed",
            "base_scope_confirmed", "regular_minutes",
        )})
    elif kind == "payslipValidation":
        body["payslip"] = payslip_inputs({"grossPay", "totalDeductions", "netPay", "paymentItems", "deductionItems"}, details=True)
        body["statementScopes"] = conditions.get("statement_scopes", {})
    else:
        body.update(payslip=payslip_inputs({"netPay"}), payments=data["payments"])
    default = con.execute(
        "SELECT confirmed_json,version,needs_review FROM comparison_groups WHERE id=?", (f"default-{case_id}",)
    ).fetchone()
    confirmed = load(default[0]) if default and not default["needs_review"] else {}
    body["inputMode"] = "CONFIRMED_GROUP" if confirmed else "CASE_FIELDS"
    if confirmed:
        types = {
            "calculation": {"CONTRACT", "ATTENDANCE"},
            "comparison": {"CONTRACT", "ATTENDANCE", "PAYSLIP"},
            "payslipValidation": {"PAYSLIP"},
            "depositComparison": {"PAYSLIP", "DEPOSIT"},
        }[kind]
        body["groupSources"] = [
            dict(r)
            for r in con.execute(
                "SELECT d.id,d.sha256,d.document_version,d.document_type,l.id AS link_id,l.link_version,l.purpose,l.applicable_from,l.applicable_to FROM group_documents g JOIN documents d ON d.id=g.document_id JOIN document_links l ON l.document_id=d.id AND l.case_id=? WHERE g.group_id=? ORDER BY d.id",
                (case_id, f"default-{case_id}"),
            )
            if r["document_type"] in types
        ]
    keys = {
        "calculation": {"wage_conditions", "work_records", "coverage", "context_requirements"},
        "comparison": {"wage_conditions", "work_records", "condition_states"},
        "payslipValidation": {"earnings", "deductions", "earnings_complete", "deductions_complete"},
        "depositComparison": {"selected_transaction_ids", "payment_scope_confirmed"},
    }[kind]
    allowed_fields = {
        "calculation": {"contract_rate", "monthly_base", "regular_minutes"},
        "comparison": {"contract_rate", "slip_rate", "worked_minutes", "slip_minutes", "base"},
        "payslipValidation": {"gross", "deduction", "net"},
        "depositComparison": {"net"},
    }[kind]
    body["groupInput"] = {k: v for k, v in (confirmed or {}).items() if k in keys}
    body["groupFields"] = [
        f for f in (confirmed or {}).get("confirmed_fields", []) if f.get("field_id") in allowed_fields
    ]
    if confirmed:
        # Identity and evidence validation affect every check, even when amounts
        # are unchanged. Keep runtime group versions out of semantic identity.
        body["groupContext"] = {k: confirmed.get(k) for k in (
            "subjects_confirmed", "source_identity", "evidence_catalog",
        )}
        body["groupConditionStates"] = [
            f for f in confirmed.get("condition_states", []) if f.get("field_id") in allowed_fields
        ]
    return digest(body)


def schedule(con, case_id, checks=None, delay=0.3, increment=True):
    from app.integration_store import ensure_default_group

    from app.review_policy import sync_reviews
    sync_reviews(con, case_id)
    checks = checks or list(KINDS.values())
    if not set(checks) <= set(KINDS.values()):
        raise ApiError(422, "INVALID_CHECK_TYPE")
    ensure_default_group(con, case_id)
    if increment:
        con.execute(
            "UPDATE cases SET input_version=input_version+1,updated_at=? WHERE id=?", (now(), case_id)
        )
        con.execute(
            "UPDATE comparison_groups SET version=version+1,needs_review=1,updated_at=? WHERE case_id=?",
            (now(), case_id),
        )
    version = con.execute("SELECT input_version FROM cases WHERE id=?", (case_id,)).fetchone()[0]
    if not increment:
        for active in con.execute(
            "SELECT * FROM jobs WHERE case_id=? AND input_version=? AND status IN ('PENDING','RUNNING') ORDER BY created_at DESC",
            (case_id, version),
        ):
            if set(checks) <= set(load(active["checks_json"])):
                return {
                    "inputVersion": version,
                    "recompute": {"scheduled": True, "jobId": active["id"], "affectedChecks": load(active["checks_json"])},
                }
    wanted = set(checks)
    for row in con.execute(
        "SELECT checks_json FROM jobs WHERE case_id=? AND status IN ('PENDING','RUNNING')", (case_id,)
    ):
        wanted.update(load(row[0]))
    con.execute(
        "UPDATE jobs SET status='SUPERSEDED',completed_at=? WHERE case_id=? AND status IN ('PENDING','RUNNING')",
        (now(), case_id),
    )
    jid = "job-" + uuid.uuid4().hex
    con.execute(
        "INSERT INTO jobs(id,case_id,input_version,checks_json,status,created_at,ready_at) VALUES(?,?,?,?,?,?,?)",
        (jid, case_id, version, dump(sorted(wanted)), "PENDING", now(), time.time() + delay),
    )
    return {
        "inputVersion": version,
        "recompute": {"scheduled": True, "jobId": jid, "affectedChecks": sorted(wanted)},
    }


def job_dto(row):
    return {
        "jobId": row["id"],
        "caseId": row["case_id"],
        "requestedInputVersion": row["input_version"],
        "affectedChecks": load(row["checks_json"]),
        "status": row["status"],
        "createdAt": row["created_at"],
        "completedAt": row["completed_at"],
        "errorCode": row["error_code"],
    }


def _single(data, kind):
    rows = data["groups"][kind]
    if kind == "ATTENDANCE" and rows and all(r.get("interval") for r in rows):
        intervals = sorted(
            (datetime.fromisoformat(r["interval"][0]), datetime.fromisoformat(r["interval"][1])) for r in rows
        )
        conflict = any(start < intervals[i - 1][1] for i, (start, _) in enumerate(intervals) if i)
        return (
            {"workedMinutes": sum(r["values"]["workedMinutes"] for r in rows)},
            conflict,
            [s for r in rows for s in r["sources"]],
        )
    return (
        (rows[0]["values"] if len(rows) == 1 else {}),
        len(rows) > 1 or any(r["conflict"] for r in rows),
        [s for r in rows for s in r["sources"]],
    )


def prepare_snapshot(con, case_id, snapshot_id):
    """Translate stored confirmations to a typed, source-authorized agent input."""
    from agent.schemas.public.inputs import SourceRef
    from app.agent_mapping import build_snapshot
    from app.integration_store import source_ref

    data = collect(con, case_id)
    c = data["case"]
    uid = con.execute("SELECT user_id FROM workplaces WHERE id=?", (c["workplace_id"],)).fetchone()[0]
    gid = "compat-" + str(case_id)
    con.execute(
        "INSERT OR IGNORE INTO comparison_groups(id,case_id,name,created_at,updated_at) VALUES(?,?,'Case API',?,?)",
        (gid, case_id, now(), now()),
    )
    con.execute(
        "UPDATE comparison_groups SET needs_review=0,updated_at=? WHERE id=? AND needs_review!=0", (now(), gid)
    )
    group = con.execute("SELECT * FROM comparison_groups WHERE id=?", (gid,)).fetchone()
    mid = "legacy-api:" + str(case_id)
    refs = [
        SourceRef(
            source_id=mid,
            owner_id=str(uid),
            workplace_id=str(c["workplace_id"]),
            kind="manual",
            link_id="group:" + gid,
            link_version=group["version"],
            document_id=mid,
            document_version=group["version"],
            content_hash=digest(data),
        )
    ]
    by_doc = {}
    for row in con.execute("SELECT document_id FROM document_links WHERE case_id=?", (case_id,)):
        doc = con.execute("SELECT deleted_at FROM documents WHERE id=?", (row[0],)).fetchone()
        if doc and doc[0] is None:
            ref = source_ref(con, row[0], uid, case_id)
            refs.append(ref)
            by_doc[row[0]] = ref
    catalog, fields, extracted, conditions = [], [], [], []

    def evidence(source):
        eid = "legacy:" + digest(source)[:28]
        if not any(e["evidence_id"] == eid for e in catalog):
            ref = by_doc.get(source.get("documentId"))
            catalog.append(
                {
                    "evidence_id": eid,
                    "source_id": ref.source_id if ref and (source.get("page") or source.get("analysisValueOrigin") == "SYSTEM_EXTRACTED_UNVERIFIED") else mid,
                    "page": source.get("page"),
                    "quote": "자동 추출 미확인 값" if source.get("analysisValueOrigin") == "SYSTEM_EXTRACTED_UNVERIFIED" else "사용자가 확인한 입력",
                    "locator": dump(source),
                    "verification_method": "ocr_unverified" if source.get("analysisValueOrigin") == "SYSTEM_EXTRACTED_UNVERIFIED" else "manual_confirmed",
                }
            )
        return eid

    all_sources = []
    for kind, mapping in SCALARS.items():
        values, conflict, sources = _single(data, kind)
        all_sources.extend(sources)
        for name, (key, unit) in mapping.items():
            value = values.get(name)
            if value is None and not conflict:
                continue
            allowed = True
            if key in ("contract_rate", "slip_rate"):
                allowed = data["conditions"].get("comparison_basis_confirmed") is True
            if key in ("worked_minutes", "slip_minutes"):
                allowed = data["conditions"].get("hours_basis_confirmed") is True
            relevant = [s for s in sources if s.get("key", name) == name]
            evs = [evidence(s) for s in relevant]
            automatic = any(s.get("analysisValueOrigin") == "SYSTEM_EXTRACTED_UNVERIFIED" for s in relevant) and not conflict
            (extracted if automatic else fields).append(
                {
                    "field_id": key,
                    "value": str(value) if value is not None and not conflict else None,
                    "unit": unit,
                    **({"analysis_value_origin": "SYSTEM_EXTRACTED_UNVERIFIED"} if automatic else {"confirmation_id": "legacy-user"}),
                    "state": "conflict" if conflict else "known",
                    "period": {"start": c["period_start"], "end": c["period_end"]},
                    "evidence_refs": evs,
                    **({"time_kind": "actual"} if unit == "minute" else {}),
                }
            )
            if not allowed:
                conditions.append({"field_id": key, "state": "pending"})
    manual_evidence = evidence({"caseId": case_id, "conditions": data["conditions"]})
    for name, value, unit in [
        ("legacy_base_confirmed", int(data["conditions"].get("base_scope_confirmed") is True), "boolean"),
        ("legacy_base_compare", int(data["conditions"].get("base_pay_basis_confirmed") is True), "boolean"),
        ("regular_minutes", data["conditions"].get("regular_minutes"), "minute"),
    ]:
        if type(value) is int:
            fields.append(
                {
                    "field_id": name,
                    "value": str(value),
                    "unit": unit,
                    "confirmation_id": "legacy-condition",
                    "period": {"start": c["period_start"], "end": c["period_end"]},
                    "evidence_refs": [manual_evidence],
                }
            )
    payslip, payslip_conflict, payslip_sources = _single(data, "PAYSLIP")

    def items(key):
        from agent.services.extraction_profiles import statement_row_role

        if payslip_conflict:
            return []
        prefix = "earning" if key == "paymentItems" else "deduction"
        rows = [(name, value) for name, value in payslip.items()
                if re.fullmatch(prefix + r"_[1-9]\d*_amount", name)]
        if not rows:
            rows = [(key, value) for value in payslip.get(key, [])]
        result = []
        for i, (name, value) in enumerate(rows):
            relevant = [s for s in payslip_sources if s.get("key", name) == name]
            automatic = any(s.get("analysisValueOrigin") == "SYSTEM_EXTRACTED_UNVERIFIED" for s in relevant)
            try:
                amount = money(value)
            except (ValueError, TypeError, ArithmeticError):
                amount = None
            role = statement_row_role(payslip.get(name.removesuffix("_amount") + "_label", ""))
            result.append({
                "item_id": key + ":" + str(i), "amount": str(amount) if amount is not None else None,
                "evidence_refs": [evidence(s) for s in relevant], "confirmed": not automatic,
                "is_total": role in {"total", "subtotal"},
                "analysis_value_origin": "SYSTEM_EXTRACTED_UNVERIFIED" if automatic else "USER_CONFIRMED",
            })
        return result

    def items_complete(key):
        if payslip_conflict:
            return False
        scope = data["conditions"].get("statement_scopes", {}).get(key)
        if scope:
            from app.statement_scope import scope_hash
            if scope["hash"] == scope_hash(con, case_id, scope["documentId"]):
                return bool(items(key)) and all(i["amount"] is not None for i in items(key))
        prefix = "earning" if key == "paymentItems" else "deduction"
        if key not in payslip or any(re.fullmatch(prefix + r"_[1-9]\d*_amount", name) for name in payslip):
            return False
        relevant = [s for s in payslip_sources if s.get("key", key) == key]
        # Neither presence nor a matching sum proves all source rows were extracted.
        return bool(relevant) and all(
            s.get("analysisValueOrigin", "USER_CONFIRMED") == "USER_CONFIRMED" for s in relevant
        )

    transactions = []
    missing_payment_source = False
    for row in data["payments"]:
        provenance = load(row["provenance_json"]) or {}
        did = provenance.get("document_id")
        if row["source"] == "DOCUMENT" and (
            did not in by_doc or provenance.get("content_hash") != by_doc[did].content_hash
            or provenance.get("document_version") != by_doc[did].document_version
        ):
            missing_payment_source = True
            continue
        ev = evidence(
            {
                "transactionId": row["id"],
                "allocationId": row["allocation_id"],
                "documentId": did,
                "page": (provenance.get("evidence_catalog") or [{}])[0].get("page"),
            }
        )
        original_refs = provenance.get("evidence_refs", [])
        for item in provenance.get("evidence_catalog", []):
            if not any(e["evidence_id"] == item["evidence_id"] for e in catalog):
                catalog.append(item)
        transactions.append(
            {
                "transaction_id": str(row["id"]),
                "amount": str(row["allocated_amount"]),
                "transaction_amount": str(row["amount"]),
                "kind": "bank" if row["payment_method"] == "BANK_TRANSFER" else "cash_memo",
                "confirmed": row["confirmation_status"] == "USER_CONFIRMED",
                "evidence_refs": list(dict.fromkeys([ev] + original_refs)),
            }
        )
    from app.agent_mapping import same_document_row
    duplicate_ids = set()
    for i, left in enumerate(data["payments"]):
        for right in data["payments"][i + 1:]:
            lp, rp = (load(r["provenance_json"]) or {} for r in (left, right))
            lr, rr = lp.get("bank_reference"), rp.get("bank_reference")
            if left["payment_method"] == right["payment_method"] and (
                same_document_row(lp, rp) or lr and lr == rr or
                left["transaction_date"] == right["transaction_date"] and left["amount"] == right["amount"]
                and not (lr and rr and lr != rr)
            ):
                duplicate_ids.update((str(left["id"]), str(right["id"])))
    for transaction in transactions:
        transaction["duplicate"] = transaction["transaction_id"] in duplicate_ids
    snapshot = build_snapshot(
        {
            "confirmed_fields": fields,
            "extracted_fields": extracted,
            "condition_states": conditions,
            "evidence_catalog": catalog,
            "earnings": items("paymentItems"),
            "deductions": items("deductionItems"),
            "earnings_complete": items_complete("paymentItems"),
            "deductions_complete": items_complete("deductionItems"),
            "payment_scope_confirmed": bool(transactions) and not missing_payment_source,
            "transaction_allocations": transactions,
            "profile": "H-STD" if c["pay_type"] == "HOURLY" else "M-STD",
        },
        snapshot_id=snapshot_id,
        input_version=c["input_version"],
        owner_id=str(uid),
        workplace_id=str(c["workplace_id"]),
        case_id=str(case_id),
        period={"start": c["period_start"], "end": c["period_end"]},
        authorized_sources=refs,
    )
    con.execute("UPDATE comparison_groups SET confirmed_json=? WHERE id=?", (dump({"legacy": True}), gid))
    return snapshot, data, gid, uid, group["version"]


def _sources(result):
    evidence = {e["evidence_id"]: e for e in result["evidence_catalog"]}

    def convert(ids):
        values = []
        for eid in ids:
            item = evidence.get(eid, {})
            try:
                value = load(item.get("locator")) or {}
            except ValueError, TypeError:
                value = {}
            sid = item.get("source_id", "")
            if not value and sid.startswith("doc:"):
                value = {"documentId": int(sid.split(":")[1]), "page": item.get("page")}
            if value and value not in values:
                values.append(value)
        return values

    return convert


def project(con, result, checks=None, fingerprints=None):
    """Only translate numbers/statuses; all arithmetic results came from agent."""

    def integer_value(value):
        try:
            return money(value)
        except ValueError, ArithmeticError:
            return None

    checks = checks or list(KINDS.values())
    rid = result["result_id"]
    row = con.execute("SELECT * FROM agent_results WHERE id=?", (rid,)).fetchone()
    c = con.execute("SELECT * FROM cases WHERE id=?", (row["case_id"],)).fetchone()
    convert = _sources(result)
    comparisons = {item["rule_id"]: item for item in result["comparisons"]}

    def check(rule):
        item = comparisons.get(rule, {})
        fractional = any(
            value is not None and integer_value(value) is None
            for value in (item.get("lhs"), item.get("rhs"), item.get("delta"))
        )
        return {
            "status": "REVIEW_REQUIRED"
            if fractional
            else STATUS.get(item.get("status"), "INSUFFICIENT_INFORMATION"),
            "calculated": integer_value(item.get("lhs")),
            "stated": integer_value(item.get("rhs")),
            "difference": None if fractional else integer_value(item.get("delta")),
            "itemSum": integer_value(item.get("lhs")),
            "statedTotal": integer_value(item.get("rhs")),
            "sources": convert(item.get("evidence_refs", [])),
            "reasonCodes": item.get("reason_codes", []),
        }

    wage_names = {
        "W-01": ("BASE_PAY", "기본급"),
        "W-02": ("WEEKLY_HOLIDAY_PAY", "주휴수당"),
        "W-03": ("OVERTIME_PAY", "연장수당"),
        "W-04": ("NIGHT_PAY", "야간수당"),
        "W-05": ("HOLIDAY_PAY", "휴일수당"),
    }
    from agent.schemas.public.results import WageItemResult
    from agent.services.facts import wage_formula_text

    wage_items = []
    for item in result["wage_section"]["items"]:
        key, label = wage_names[item["rule_id"]]
        wage_items.append(
            {
                "type": key,
                "label": label,
                "status": {
                    "calculated": "CALCULATED",
                    "not_applicable": "NOT_APPLICABLE",
                    "insufficient": "INSUFFICIENT_INFORMATION",
                    "needs_review": "REVIEW_REQUIRED",
                }[item["status"]],
                "amount": integer_value(item["amount"]),
                "currency": "KRW",
                "formula": {
                    "inputs": item.get("operands", {}),
                    "displayText": wage_formula_text(WageItemResult.model_validate(item)),
                },
                "rule": {
                    "ruleId": item["rule_id"],
                    "ruleVersion": item.get("rule_version"),
                    "verified": bool(item.get("official_sources")),
                },
                "reasonCode": next(iter(item.get("reason_codes", [])), None),
                "sources": convert(item["evidence_refs"]),
            }
        )
    amounts = [i for i in wage_items if i["status"] == "CALCULATED"]
    calculation = {
        "status": "COMPLETE"
        if result["wage_section"]["scope_complete"]
        else ("PARTIAL" if amounts else "REVIEW_REQUIRED"),
        "items": wage_items,
        "calculatedSubtotal": integer_value(result["wage_section"]["subtotal"]) if amounts else None,
        "grossPay": integer_value(result["wage_section"]["gross_total"]),
    }
    comparison_items = []
    for rule, name, unit in [
        ("R-01", "HOURLY_WAGE", "KRW_PER_HOUR"),
        ("R-02", "WORKED_MINUTES", "MINUTE"),
        ("R-07", "BASE_PAY", "KRW"),
    ]:
        item = comparisons.get(rule, {})
        comparison_items.append(
            {
                "type": name,
                **check(rule),
                "contractValue": integer_value(item.get("lhs") if rule == "R-07" else item.get("rhs")),
                "payslipValue": integer_value(item.get("rhs") if rule == "R-07" else item.get("lhs")),
                "attendanceValue": integer_value(item.get("rhs")) if rule == "R-02" else None,
                "comparisonBasis": {
                    "periodStart": c["period_start"],
                    "periodEnd": c["period_end"],
                    "unit": unit,
                    "semanticMatch": item.get("status") in ("match", "difference"),
                },
                "sources": convert(item.get("evidence_refs", [])),
            }
        )
    data = collect(con, c["id"])
    rows = data["payments"]
    frozen = load(con.execute(
        "SELECT data_json FROM agent_snapshots WHERE id=?", (row["snapshot_id"],)
    ).fetchone()[0])
    eligible = [t for t in frozen.get("transaction_allocations", [])
                if t.get("linked", True) and t.get("confirmed", True) and not t.get("duplicate", False)]
    bank = (
        sum(money(t["amount"]) for t in eligible if t.get("kind", "bank") == "bank") if eligible else None
    )
    cash = sum(money(t["amount"]) for t in eligible if t.get("kind") == "cash_memo") if eligible else None
    payment = check("R-06")
    bodies = {
        "calculation": calculation,
        "comparison": {
            "status": "COMPLETE"
            if all(i["status"] in ("MATCH", "DIFFERENCE") for i in comparison_items)
            else "PARTIAL",
            "items": comparison_items,
        },
        "payslipValidation": {
            "paymentTotal": check("R-03"),
            "deductionTotal": check("R-04"),
            "netPay": check("R-05"),
        },
        "depositComparison": {
            "status": payment["status"],
            "reasonCodes": payment["reasonCodes"],
            "payslipNetPay": payment["calculated"],
            "linkedBankDepositTotal": bank if payment["stated"] is not None else None,
            "linkedCashPaymentTotal": cash if payment["stated"] is not None else None,
            "linkedPaymentTotal": payment["stated"],
            "difference": payment["difference"],
            "transactionIds": [r["id"] for r in rows],
            "deposits": [
                {
                    "transactionId": r["id"],
                    "allocationId": r["allocation_id"],
                    "allocatedAmount": r["allocated_amount"],
                    "paymentMethod": r["payment_method"],
                    "confirmationStatus": r["confirmation_status"],
                    "source": r["source"],
                    "sourceDocumentId": (load(r["provenance_json"]) or {}).get("document_id"),
                    "provenance": load(r["provenance_json"]),
                }
                for r in rows
            ],
        },
    }
    auth = load(
        con.execute("SELECT data_json FROM agent_snapshots WHERE id=?", (row["snapshot_id"],)).fetchone()[0]
    )["authorized_sources"]
    for kind, check_name in KINDS.items():
        if check_name not in checks:
            continue
        fp = (fingerprints or {}).get(kind) or fingerprint(con, c["id"], kind, data)
        if fingerprints and fp != fingerprint(con, c["id"], kind, data):
            continue
        last = con.execute(
            "SELECT * FROM snapshots WHERE case_id=? AND kind=? ORDER BY version DESC LIMIT 1",
            (c["id"], kind),
        ).fetchone()
        if last and last["fingerprint"] == fp:
            continue
        version = last["version"] + 1 if last else 1
        body = {
            **bodies[kind],
            "agentResultId": rid,
            "result_id": rid,
            "_agentResultId": rid,
            "_agentGroupId": row["group_id"],
            "_authorizedSources": auth,
            "_period": {"periodStart": c["period_start"], "periodEnd": c["period_end"]},
            "_sources": (
                [s for i in wage_items for s in i["sources"]]
                if kind == "calculation"
                else [
                    s
                    for rule in {
                        "comparison": ["R-01", "R-02", "R-07"],
                        "payslipValidation": ["R-03", "R-04", "R-05"],
                        "depositComparison": ["R-06"],
                    }[kind]
                    for s in convert(comparisons.get(rule, {}).get("evidence_refs", []))
                ]
            ),
        }
        con.execute(
            "INSERT INTO snapshots(case_id,kind,version,input_version,fingerprint,data_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (c["id"], kind, version, result["input_version"], fp, dump(body), now()),
        )


def enqueue_current(con, service, case_id, checks=None, metadata=None):
    """Freeze one authorized input now; never read mutable case data after enqueue."""
    from app.agent_mapping import analysis_payload
    from app.integration_store import identifier

    checks = checks or list(KINDS.values())
    current = con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    default = con.execute("SELECT * FROM comparison_groups WHERE id=?", (f"default-{case_id}",)).fetchone()
    if default and default["confirmed_json"] != "{}" and not default["needs_review"]:
        uid = con.execute("SELECT user_id FROM workplaces WHERE id=?", (current["workplace_id"],)).fetchone()[
            0
        ]
        snapshot = service.store.freeze_snapshot(con, default["id"], uid)
        data, gid, version = collect(con, case_id), default["id"], default["version"]
    else:
        snapshot, data, gid, uid, version = prepare_snapshot(con, case_id, identifier())
    display = {
        "workplace_name": con.execute(
            "SELECT name FROM workplaces WHERE id=?", (current["workplace_id"],)
        ).fetchone()[0],
        "source_names": {
            r.document_id: con.execute(
                "SELECT file_name FROM documents WHERE id=?", (r.document_id,)
            ).fetchone()[0]
            for r in snapshot.authorized_sources
            if r.kind == "document"
        },
    }
    con.execute(
        "INSERT OR IGNORE INTO agent_snapshots(id,group_id,case_id,owner_id,input_version,group_version,snapshot_hash,data_json,display_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot.snapshot_id,
            gid,
            case_id,
            uid,
            current["input_version"],
            version,
            snapshot.snapshot_hash,
            snapshot.model_dump_json(),
            dump(display),
            now(),
        ),
    )
    payload = analysis_payload(snapshot).model_copy(update={"checks": checks})
    meta = {
        **(metadata or {}),
        "checks": checks,
        "fingerprints": {k: fingerprint(con, case_id, k, data) for k in KINDS},
    }
    return service.store.enqueue(
        con,
        uid,
        "analyze_case",
        payload,
        case_id=case_id,
        group_id=gid,
        snapshot_id=snapshot.snapshot_id,
        metadata=meta,
    )


def complete_job(con, agent_job, outcome=None, error_code=None):
    """Finish the Case job in the same transaction as actual agent publication."""
    metadata = load(agent_job["metadata_json"]) or {}
    jid = metadata.get("legacyJobId")
    if not jid:
        return
    job = con.execute("SELECT * FROM jobs WHERE id=? AND status='RUNNING'", (jid,)).fetchone()
    if job is None:
        return
    current = con.execute("SELECT * FROM cases WHERE id=?", (job["case_id"],)).fetchone()
    if current["input_version"] != job["input_version"] or current["deleted_at"]:
        status = "SUPERSEDED"
    elif outcome is None:
        status = "FAILED"
    elif any(
        saved != fingerprint(con, job["case_id"], kind)
        for kind, saved in metadata.get("fingerprints", {}).items()
        if KINDS[kind] in load(job["checks_json"])
    ):
        # Group confirmations can change independently of Case inputVersion.
        # A job whose projection was rejected must not claim current success.
        status = "SUPERSEDED"
    else:
        checks = load(job["checks_json"])
        rules = {
            "CONTRACT_ATTENDANCE_COMPARISON": ["R-01", "R-02"],
            "PAYSLIP_VALIDATION": ["R-03", "R-04", "R-05"],
            "DEPOSIT_COMPARISON": ["R-06"],
        }
        selected = {r for c in checks for r in rules.get(c, [])}
        complete = all(
            i["status"] in ("match", "difference") for i in outcome["comparisons"] if i["rule_id"] in selected
        )
        if "WAGE_CALCULATION" in checks:
            complete = complete and outcome["wage_section"]["scope_complete"]
        status = "SUCCEEDED" if complete else "PARTIAL"
    con.execute(
        "UPDATE jobs SET status=?,error_code=?,completed_at=? WHERE id=? AND status='RUNNING'",
        (status, error_code, now(), jid),
    )


async def execute_next(state):
    async with state.service.semaphore:
        with state.db.connect(write=True) as con:
            job = con.execute(
                """SELECT j.* FROM jobs j JOIN cases c ON c.id=j.case_id JOIN workplaces w ON w.id=c.workplace_id
                WHERE j.status='PENDING' AND j.ready_at<=? AND c.deleted_at IS NULL AND w.deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM agent_jobs a WHERE a.case_id=j.case_id AND a.status='RUNNING') ORDER BY j.created_at LIMIT 1""",
                (time.time(),),
            ).fetchone()
            if job is None:
                return False
            case = con.execute("SELECT * FROM cases WHERE id=?", (job["case_id"],)).fetchone()
            if case["input_version"] != job["input_version"]:
                con.execute(
                    "UPDATE jobs SET status='SUPERSEDED',completed_at=? WHERE id=?", (now(), job["id"])
                )
                return True
            con.execute("UPDATE jobs SET status='RUNNING' WHERE id=?", (job["id"],))
        try:
            with state.db.connect(write=True) as con:
                current_case = con.execute("SELECT * FROM cases WHERE id=?", (job["case_id"],)).fetchone()
                if current_case["input_version"] != job["input_version"] or current_case["deleted_at"]:
                    con.execute(
                        "UPDATE jobs SET status='SUPERSEDED',completed_at=? WHERE id=?", (now(), job["id"])
                    )
                    return True
                checks = load(job["checks_json"])
                ajid = enqueue_current(
                    con, state.service, job["case_id"], checks, metadata={"legacyJobId": job["id"]}
                )
            claimed = state.service.store.claim(ajid)
            if not claimed:
                return True
            await state.service._execute(claimed)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("case_compatibility_job_failed job_id=%s", job["id"])
            with state.db.connect(write=True) as con:
                con.execute(
                    "UPDATE jobs SET status='FAILED',completed_at=?,error_code='RECOMPUTE_FAILED' WHERE id=? AND status='RUNNING'",
                    (now(), job["id"]),
                )
        return True
