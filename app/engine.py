"""Stable Case views over source-authorized agent result projections."""

from collections import Counter

from app.db import load
from app.source_location import public_source
from app.compatibility import KINDS, IDS, collect, fingerprint
from app.compatibility import job_dto as job_dto, schedule as schedule

ALL_CHECKS = list(KINDS.values())


def snapshot_accessible(con, row):
    from app.integration_store import check_historical_access
    from agent.schemas.public.inputs import SourceRef

    if "kind" not in row.keys():
        snapshot = con.execute(
            "SELECT data_json FROM agent_snapshots WHERE id=?", (row["snapshot_id"],)
        ).fetchone()
        body = load(snapshot[0])
        group_id, uid, refs = row["group_id"], row["owner_id"], body["authorized_sources"]
    else:
        body = load(row["data_json"])
        group_id, refs = body.get("_agentGroupId"), body.get("_authorizedSources", [])
        uid = con.execute(
            "SELECT w.user_id FROM cases c JOIN workplaces w ON c.workplace_id=w.id WHERE c.id=?",
            (row["case_id"],),
        ).fetchone()[0]
    try:
        check_historical_access(
            con, uid, row["case_id"], group_id, [SourceRef.model_validate(r) for r in refs]
        )
        return True
    except Exception:
        return False


def latest(con, case_id, kind=None, data=None):
    kind = kind or "comparison"
    row = con.execute(
        "SELECT * FROM snapshots WHERE case_id=? AND kind=? ORDER BY version DESC LIMIT 1", (case_id, kind)
    ).fetchone()
    if row is not None and not snapshot_accessible(con, row):
        row = None
    c = con.execute("SELECT input_version FROM cases WHERE id=?", (case_id,)).fetchone()
    fresh = "CURRENT" if row and row["fingerprint"] == fingerprint(con, case_id, kind, data) else "STALE"
    if fresh != "CURRENT":
        relevant = next(
            (
                j
                for j in con.execute(
                    "SELECT * FROM jobs WHERE case_id=? ORDER BY created_at DESC", (case_id,)
                )
                if KINDS[kind] in load(j["checks_json"]) and j["status"] != "SUPERSEDED"
            ),
            None,
        )
        if relevant:
            fresh = (
                "UPDATING"
                if relevant["status"] in ("PENDING", "RUNNING")
                else ("FAILED" if relevant["status"] == "FAILED" else "STALE")
            )
    return row, {
        "snapshotId": row["id"] if row else None,
        "version": row["version"] if row else None,
        "inputVersion": row["input_version"] if row else c[0],
        "createdAt": row["created_at"] if row else None,
        "freshness": fresh,
    }


def snapshot_dto(row, freshness="CURRENT"):
    body = {k: v for k, v in load(row["data_json"]).items() if not k.startswith("_")}
    return {
        **body,
        IDS[row["kind"]]: row["id"],
        "version": row["version"],
        "basedOnInputVersion": row["input_version"],
        "createdAt": row["created_at"],
        "freshness": freshness,
    }


def results(con, case_id):
    data = collect(con, case_id)
    refs, bodies = {}, {}
    for kind in KINDS:
        row, ref = latest(con, case_id, kind, data)
        refs[kind] = ref
        bodies[kind] = snapshot_dto(row, ref["freshness"]) if row else None
    titles = {
        "HOURLY_WAGE": "계약·명세서 시급 대조",
        "WORKED_MINUTES": "근태·명세서 근로시간 대조",
        "BASE_PAY": "계산 임금·명세서 항목 대조",
        "paymentTotal": "지급 항목 합계 대조",
        "deductionTotal": "공제 항목 합계 대조",
        "netPay": "실수령액 산술 검사",
        "NET_VS_PAYMENT": "실수령액·지급 내역 대조",
    }
    items = []
    for kind in ("comparison", "payslipValidation", "depositComparison"):
        row = (
            con.execute("SELECT * FROM snapshots WHERE id=?", (refs[kind]["snapshotId"],)).fetchone()
            if refs[kind]["snapshotId"]
            else None
        )
        body = bodies[kind]
        if body is None:
            continue
        frozen = load(row["data_json"])
        sources = frozen.get("_sources", [])
        period = frozen.get(
            "_period", {"periodStart": data["case"]["period_start"], "periodEnd": data["case"]["period_end"]}
        )
        checks = (
            body["items"]
            if kind == "comparison"
            else [{"type": k, **body[k]} for k in ("paymentTotal", "deductionTotal", "netPay")]
            if kind == "payslipValidation"
            else [
                {
                    "type": "NET_VS_PAYMENT",
                    "calculated": body["payslipNetPay"],
                    "stated": body["linkedPaymentTotal"],
                    "difference": body["difference"],
                    "status": body["status"],
                }
            ]
        )
        for check in checks:
            items.append(
                {
                    "id": check["type"],
                    "title": titles.get(check["type"], check["type"]),
                    "status": check["status"],
                    "calculatedValue": check.get("calculated"),
                    "documentValue": check.get("stated"),
                    "difference": check.get("difference"),
                    "unit": (check.get("comparisonBasis") or {}).get("unit")
                    or ("MINUTE" if check["type"] == "WORKED_MINUTES" else "KRW"),
                    **period,
                    "formula": None,
                    "rule": None,
                    "comparisonBasis": check.get("comparisonBasis"),
                    "sources": check.get("sources", sources),
                    "confirmedAt": max((s.get("confirmedAt") or "" for s in sources), default="") or None,
                    "nextAction": None if check["status"] == "MATCH" else "자료와 확인값을 검토하세요.",
                }
            )
    counts = Counter(i["status"] for i in items)
    statuses = [r["freshness"] for r in refs.values()]
    overall = next((s for s in ("FAILED", "UPDATING", "STALE") if s in statuses), "CURRENT")
    active = con.execute(
        "SELECT id FROM jobs WHERE case_id=? AND status IN ('PENDING','RUNNING') ORDER BY created_at DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    calculation = bodies["calculation"]
    raw = None
    raw_row = con.execute(
        "SELECT * FROM agent_results WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (case_id,)
    ).fetchone()
    if raw_row and snapshot_accessible(con, raw_row):
        raw = load(raw_row["data_json"])
        saved = con.execute("SELECT * FROM agent_snapshots WHERE id=?", (raw_row["snapshot_id"],)).fetchone()
        group = con.execute("SELECT * FROM comparison_groups WHERE id=?", (raw_row["group_id"],)).fetchone()
        raw["freshness"] = (
            "current"
            if saved["input_version"] == data["case"]["input_version"]
            and group["version"] == saved["group_version"]
            and not group["needs_review"]
            else "stale"
        )
    return {
        "caseId": case_id,
        "currentInputVersion": data["case"]["input_version"],
        "resultVersion": max((r["version"] or 0 for r in refs.values()), default=0),
        "summary": {
            "calculationStatus": calculation["status"] if calculation else "REVIEW_REQUIRED",
            "comparisonStatus": "INSUFFICIENT_INFORMATION"
            if not items
            else next(
                (s for s in ("DIFFERENCE", "REVIEW_REQUIRED", "INSUFFICIENT_INFORMATION") if counts[s]),
                "MATCH",
            ),
            "counts": {
                "match": counts["MATCH"],
                "difference": counts["DIFFERENCE"],
                "insufficientInformation": counts["INSUFFICIENT_INFORMATION"],
                "reviewRequired": counts["REVIEW_REQUIRED"],
            },
            "calculatedSubtotal": calculation["calculatedSubtotal"] if calculation else None,
            "grossPay": calculation["grossPay"] if calculation else None,
        },
        "freshness": {
            "overall": overall,
            "hasOutdatedResults": overall != "CURRENT",
            "activeJobId": active[0] if active else None,
        },
        "versions": refs,
        "items": items,
        "result": raw,
        **bodies,
    }


def effective_fields(con, case_id, document_id):
    result = []
    for field in con.execute("SELECT * FROM fields WHERE document_id=? ORDER BY id", (document_id,)):
        override = con.execute(
            "SELECT * FROM field_overrides WHERE case_id=? AND field_id=?", (case_id, field["id"])
        ).fetchone()
        active = override or field
        result.append(
            {
                "fieldId": field["id"],
                "key": field["key"],
                "label": field["label"],
                "value": load(active["value_json"]),
                "valueType": field["value_type"],
                "status": active["status"],
                "editedByUser": bool(override or field["edited"]),
                "confirmedAt": active["confirmed_at"],
                "effectiveValueOrigin": "CASE_OVERRIDE" if override else "SHARED_DEFAULT",
                "hasCaseOverride": override is not None,
                "source": public_source(
                    load(override["source_json"])
                    if override and override["source_json"]
                    else load(field["source_json"])
                ),
            }
        )
    from app.review_policy import metadata
    doc = con.execute("SELECT document_type FROM documents WHERE id=?", (document_id,)).fetchone()
    case = con.execute("SELECT pay_type FROM cases WHERE id=?", (case_id,)).fetchone()
    for field in result:
        row = con.execute("SELECT resolution FROM field_review_resolutions WHERE case_id=? AND field_id=?", (case_id, field["fieldId"])).fetchone()
        field.update(metadata(field, doc[0], case[0] if case else None, row[0] if row else "NONE"))
    return result
