"""Case-specific review and analysis eligibility over existing field records."""

from app.compatibility import public_field
from app.db import load

REQUIRED = {
    "CONTRACT": {"periodStart", "periodEnd", "payType", "regularMinutes"},
    "ATTENDANCE": {"workDate", "clockIn", "clockOut", "breakMinutes"},
    "PAYSLIP": {"periodStart", "periodEnd", "grossPay", "totalDeductions", "netPay"},
}
ALIASES = {
    "contract_start": "periodStart",
    "contract_end": "periodEnd",
    "period_start": "periodStart",
    "period_end": "periodEnd",
    "pay_type": "payType",
    "regular_minutes": "regularMinutes",
    "work_date": "workDate",
    "clock_in": "clockIn",
    "clock_out": "clockOut",
    "break_minutes": "breakMinutes",
}


def metadata(field, document_type, pay_type, resolution="NONE"):
    f = public_field(field)
    status = field.get("agentStatus", field["status"])
    if f["status"] == "UNREADABLE" and status not in {"CONFLICTED", "DEFERRED", "NEEDS_RECONFIRMATION"}:
        status = "UNREADABLE"
    value = f["value"]
    valid = value is not None and not (isinstance(value, str) and not value.strip())
    keys = set(REQUIRED.get(document_type, set()))
    if document_type == "CONTRACT":
        keys.add("hourlyWage" if pay_type == "HOURLY" else "monthlyBasePay")
    key = ALIASES.get(f["key"], f["key"])
    reason = (
        "CONFLICTED"
        if status == "CONFLICTED"
        else "RECONFIRMATION"
        if status in {"DEFERRED", "NEEDS_RECONFIRMATION"}
        else "UNREADABLE"
        if status == "UNREADABLE"
        else "MISSING"
        if status == "MISSING" or not valid
        else "REQUIRED"
        if key in keys
        else None
    )
    resolved = (valid and status == "CONFIRMED") or (
        not valid and status in {"MISSING", "UNREADABLE"} and resolution in {"NOT_IN_DOCUMENT", "UNKNOWN"}
    )
    usable = (
        valid
        and resolution == "NONE"
        and (status == "CONFIRMED" or (reason is None and status == "UNCONFIRMED"))
    )
    return dict(
        requiresUserReview=reason is not None,
        reviewReason=reason,
        reviewResolution=resolution,
        reviewResolved=bool(resolved),
        usableForAnalysis=bool(usable),
        analysisValueOrigin=("USER_CONFIRMED" if status == "CONFIRMED" else "SYSTEM_EXTRACTED_UNVERIFIED")
        if usable
        else "NONE",
    )


def summary(fields, processing_status):
    pending = [f["fieldId"] for f in fields if f["requiresUserReview"] and not f["reviewResolved"]]
    required = [f for f in fields if f["requiresUserReview"]]
    return dict(
        totalFieldCount=len(fields),
        confirmedFieldCount=sum(f["status"] == "CONFIRMED" for f in fields),
        requiredReviewCount=len(required),
        resolvedRequiredCount=sum(f["reviewResolved"] for f in required),
        pendingRequiredFieldIds=pending,
        unconfirmedFieldIds=[f["fieldId"] for f in fields if f["status"] != "CONFIRMED"],
        optionalUnconfirmedFieldIds=[
            f["fieldId"] for f in fields if not f["requiresUserReview"] and f["status"] != "CONFIRMED"
        ],
        canCompleteReview=bool(fields) and not pending and processing_status in {"READY", "REVIEW_REQUIRED"},
    )


def processing_status(row):
    """The READ_READY handoff depends on whether extraction is automatic."""
    if row["processing_status"] != "READ_READY":
        return row["processing_status"]
    auto = (load(row["ai_metadata"]) or {}).get("legacyAutoExtract")
    return "PROCESSING" if auto and row["error_code"] is None else "REVIEW_REQUIRED"


def sync_reviews(con, case_id, document_id=None, started=False):
    from app.engine import effective_fields

    for link in con.execute(
        "SELECT l.*,d.processing_status,d.ai_metadata,d.error_code FROM document_links l JOIN documents d ON d.id=l.document_id WHERE l.case_id=?",
        (case_id,),
    ).fetchall():
        if document_id is not None and link["document_id"] != document_id:
            continue
        # Processing transitions alone do not invalidate reviewed inputs.
        fields = effective_fields(con, case_id, link["document_id"])
        complete = bool(fields) and not any(
            f["requiresUserReview"] and not f["reviewResolved"] for f in fields
        )
        if (link["review_status"] == "CONFIRMED" and not complete) or (
            started and link["review_status"] != "CONFIRMED"
        ):
            con.execute("UPDATE document_links SET review_status='IN_PROGRESS' WHERE id=?", (link["id"],))
