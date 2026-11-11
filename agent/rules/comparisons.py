from decimal import Decimal

from agent.schemas.public.results import ComparisonResult
from agent.services.calculation_context import source_period_mismatch


def comparison(rule_id, lhs=None, rhs=None, *, unit="KRW", refs=(), reasons=(), conflict=False):
    reasons = list(dict.fromkeys(reasons))
    status = (
        "needs_review" if conflict else "insufficient" if lhs is None or rhs is None or reasons else "match"
    )
    delta = None
    if status == "match":
        delta = lhs - rhs
        status = "match" if delta == 0 else "difference"
    return ComparisonResult(
        rule_id=rule_id,
        status=status,
        lhs=lhs,
        rhs=rhs,
        unit=unit,
        delta=delta,
        formula_id=f"{rule_id}:lhs-rhs",
        scope_complete=delta is not None,
        evidence_refs=sorted(set(refs)),
        reason_codes=reasons,
    )


def field(snapshot, key, unit=None):
    value = next((f for f in snapshot.confirmed_fields if f.field_id == key), None)
    if value is None:
        return None, [], ["FIELD_MISSING"], False
    condition = next((c for c in snapshot.condition_states if c.field_id == key), None)
    state = condition.state if condition and condition.state != "known" else value.state
    conflict = state == "conflict"
    reasons = [] if state == "known" else [f"FIELD_{state.upper()}"]
    if unit and value.unit != unit:
        reasons.append("UNIT_MISMATCH")
        conflict = True
    period_valid = value.period is not None and (
        value.period.start <= snapshot.period.start and value.period.end >= snapshot.period.end
        if key == "contract_rate"
        else value.period == snapshot.period
    )
    if state == "known" and not period_valid:
        reasons.append("PERIOD_MISMATCH")
        conflict = True
    required_period = snapshot.period if key == "contract_rate" else value.period or snapshot.period
    if state == "known" and source_period_mismatch(snapshot, value.evidence_refs, required_period):
        reasons.append("SOURCE_PERIOD_MISMATCH")
        conflict = True
    return value.value if state == "known" else None, list(value.evidence_refs), reasons, conflict


def terms(context):
    s = context.snapshot
    unit = "KRW/hour" if s.profile == "H-STD" else "KRW/month"
    a, ar, ae, ac = field(s, "slip_rate", unit)
    b, br, be, bc = field(s, "contract_rate", unit)
    rate = comparison("R-01", a, b, unit=unit, refs=ar + br, reasons=ae + be, conflict=ac or bc)
    a, ar, ae, ac = field(s, "slip_minutes", "minute")
    b, br, be, bc = field(s, "worked_minutes", "minute")
    left = next((f for f in s.confirmed_fields if f.field_id == "slip_minutes"), None)
    right = next((f for f in s.confirmed_fields if f.field_id == "worked_minutes"), None)
    meaning = not left or not right or left.time_kind is None or left.time_kind != right.time_kind
    if meaning:
        ae.append("TIME_MEANING_UNCONFIRMED")
    payroll_intervals = [item for item in context.normalized_intervals if item.get("payroll")]
    br += [ref for item in payroll_intervals for ref in item["evidence_refs"]]
    work_conflict = False
    if "overlapping_payroll_work_sources" in context.issues:
        ae.append("WORK_SOURCE_CONFLICT")
        work_conflict = True
    if any(issue.startswith("payroll_source_period_mismatch:") for issue in context.issues):
        ae.append("SOURCE_PERIOD_MISMATCH")
        work_conflict = True
    if right and right.time_kind == "actual":
        if any(
            issue.startswith(("payroll_break_overlap:", "payroll_break_location_unknown:"))
            for issue in context.issues
        ):
            ae.append("WORK_BREAK_UNCONFIRMED")
            work_conflict = True
        if payroll_intervals and b is not None and not bc and not work_conflict:
            detailed_minutes = sum(item["minutes"] for item in payroll_intervals if not item["on_break"])
            if detailed_minutes != b:
                ae.append("WORK_SUMMARY_MISMATCH")
                work_conflict = True
    minutes = comparison(
        "R-02",
        a,
        b,
        unit="minute",
        refs=ar + br,
        reasons=ae + be,
        conflict=ac
        or bc
        or work_conflict
        or (meaning and bool(left and right and left.state == right.state == "known")),
    )
    return [rate, minutes]


def statements(context):
    s = context.snapshot
    results = []
    for rid, items, complete, key in [
        ("R-03", s.earnings, s.earnings_complete, "gross"),
        ("R-04", s.deductions, s.deductions_complete, "deduction"),
    ]:
        rhs, refs, errors, conflict = field(s, key, "KRW")
        detail = [i for i in items if not i.is_total]
        if len({i.item_id for i in items}) != len(items):
            errors.append("DUPLICATE_STATEMENT_ITEM")
            conflict = True
        valid = complete and all(i.confirmed and i.amount is not None for i in detail)
        if any(i.confirmed and source_period_mismatch(s, i.evidence_refs) for i in detail):
            errors.append("SOURCE_PERIOD_MISMATCH")
            conflict = True
        if not valid:
            errors.append("ITEM_SCOPE_INCOMPLETE")
        lhs = sum((i.amount for i in detail), Decimal(0)) if valid else None
        refs += [e for i in detail for e in i.evidence_refs]
        results.append(comparison(rid, lhs, rhs, refs=refs, reasons=errors, conflict=conflict))
    gross, gr, ge, gc = field(s, "gross", "KRW")
    ded, dr, de, dc = field(s, "deduction", "KRW")
    net, nr, ne, nc = field(s, "net", "KRW")
    results.append(
        comparison(
            "R-05",
            gross - ded if gross is not None and ded is not None else None,
            net,
            refs=gr + dr + nr,
            reasons=ge + de + ne,
            conflict=gc or dc or nc,
        )
    )
    return results


def payments(context):
    s = context.snapshot
    net, refs, reasons, conflict = field(s, "net", "KRW")
    selected = [
        t
        for t in s.transaction_allocations
        if t.confirmed and t.linked and not t.duplicate and t.kind == "bank"
    ]
    if len({t.transaction_id for t in selected}) != len(selected):
        reasons.append("DUPLICATE_TRANSACTION")
        conflict = True
    if any(t.amount < 0 or t.transaction_amount < 0 or t.amount > t.transaction_amount for t in selected):
        reasons.append("INVALID_ALLOCATION")
        conflict = True
    if any(source_period_mismatch(s, item.evidence_refs) for item in selected):
        reasons.append("SOURCE_PERIOD_MISMATCH")
        conflict = True
    if not s.payment_scope_confirmed or not selected:
        reasons.append("PAYMENT_SCOPE_UNCONFIRMED")
    amount = sum((t.amount for t in selected), Decimal(0)) if selected and s.payment_scope_confirmed else None
    refs += [e for t in selected for e in t.evidence_refs]
    return [comparison("R-06", net, amount, refs=refs, reasons=reasons, conflict=conflict)]
