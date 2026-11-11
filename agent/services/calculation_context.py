from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agent.schemas.internal.calculation_context import CalculationContext
from agent.schemas.public.errors import IntegrityError
from agent.schemas.public.inputs import InputSnapshot
from agent.services.hashing import digest, snapshot_hash


def source_period_mismatch(snapshot, evidence_refs, period=None):
    """Check declared link periods without letting unscoped manual evidence override them."""
    period = period or snapshot.period
    evidence = {item.evidence_id: item for item in snapshot.evidence_catalog}
    sources = {item.source_id: item for item in snapshot.authorized_sources}
    periods = [
        sources[source_id].applicable_period
        for source_id in {evidence[ref].source_id for ref in evidence_refs}
        if sources[source_id].applicable_period is not None
    ]
    if not periods:
        return False
    # Several attendance documents may jointly cover one confirmed monthly summary.
    if any(item.end < period.start or item.start > period.end for item in periods):
        return True
    covered_until = period.start
    for item in sorted(periods, key=lambda item: item.start):
        if item.start > covered_until:
            return True
        if item.end >= period.end:
            return False
        covered_until = max(covered_until, item.end + timedelta(days=1))
    return True


def build_calculation_context(snapshot: InputSnapshot, owner_id: str, case_id: str):
    if snapshot.owner_id != owner_id or snapshot.case_id != case_id:
        raise IntegrityError("Snapshot ownership does not match job")
    if snapshot.snapshot_hash != snapshot_hash(snapshot):
        raise IntegrityError("Snapshot hash mismatch")
    fields = [f.field_id for f in snapshot.confirmed_fields]
    if len({r.record_id for r in snapshot.work_records}) != len(snapshot.work_records):
        raise IntegrityError("Duplicate work record IDs")
    sources = {s.source_id: s for s in snapshot.authorized_sources}
    evidence = {e.evidence_id: e for e in snapshot.evidence_catalog}
    if (
        len(fields) != len(set(fields))
        or len(sources) != len(snapshot.authorized_sources)
        or len(evidence) != len(snapshot.evidence_catalog)
    ):
        raise IntegrityError("Duplicate input IDs")
    field_map = {f.field_id: f for f in snapshot.confirmed_fields}
    if len({c.field_id for c in snapshot.condition_states}) != len(snapshot.condition_states):
        raise IntegrityError("Duplicate condition state")
    for condition in snapshot.condition_states:
        if condition.state == "known":
            confirmed = field_map.get(condition.confirmed_field_ref)
            if confirmed is None or confirmed.state != "known" or confirmed.field_id != condition.field_id:
                raise IntegrityError("Known condition must reference its confirmed field")
        elif condition.confirmed_field_ref is not None:
            raise IntegrityError("Unconfirmed condition cannot select a confirmed value")
        if any(ref not in sources for ref in condition.conflicting_source_refs):
            raise IntegrityError("Condition conflict refers to an unauthorized source")
    for source in sources.values():
        if (
            source.owner_id != owner_id
            or source.workplace_id != snapshot.workplace_id
            or source.status != "active"
        ):
            raise IntegrityError("Source authorization mismatch")
    for ev in evidence.values():
        if ev.source_id not in sources:
            raise IntegrityError("Evidence refers to unauthorized source")
        if sources[ev.source_id].kind == "document" and ev.page is None:
            raise IntegrityError("Document evidence requires a page")
    for record in [
        *snapshot.confirmed_fields,
        *snapshot.earnings,
        *snapshot.deductions,
        *snapshot.transaction_allocations,
        *snapshot.work_records,
    ]:
        if any(ref not in evidence for ref in record.evidence_refs):
            raise IntegrityError("Unknown evidence ID")
        if (
            not record.evidence_refs
            and getattr(record, "state", "known") == "known"
            and getattr(record, "confirmed", True)
        ):
            raise IntegrityError("Confirmed input requires evidence")
    for field in snapshot.confirmed_fields:
        if field.state == "known":
            for ref in field.evidence_refs:
                src = sources[evidence[ref].source_id]
                if src.usage == "context":
                    raise IntegrityError("Context-only source cannot supply payroll scalar")
    if snapshot.wage_conditions is not None:
        wage = snapshot.wage_conditions
        conditions = [value for value in wage.__dict__.values() if hasattr(value, "state")]
        for item in [*conditions, *wage.days, *wage.included_allowances, *wage.statement_mappings]:
            refs = item.evidence_refs
            if any(ref not in evidence for ref in refs):
                raise IntegrityError("Unknown wage evidence ID")
            confirmed = getattr(item, "state", None) == "known" or getattr(item, "confirmed", False)
            if confirmed and not refs:
                raise IntegrityError("Confirmed wage input requires evidence")
            if (
                item in conditions
                and confirmed
                and any(sources[evidence[ref].source_id].usage == "context" for ref in refs)
            ):
                raise IntegrityError("Context-only source cannot supply payroll wage condition")
    dependencies = [f"source:{s.source_id}:{s.document_version}:{s.link_version}" for s in sources.values()]
    dependencies += [f"required-period:{p.start}:{p.end}" for p in snapshot.context_requirements]
    intervals, issues = [], []
    tz = ZoneInfo(snapshot.timezone)
    payroll_start = datetime.combine(snapshot.period.start, time.min, tz).astimezone(UTC)
    payroll_end = datetime.combine(snapshot.period.end + timedelta(days=1), time.min, tz).astimezone(UTC)
    payroll_records = []
    for record in sorted(snapshot.work_records, key=lambda r: (r.start_at, r.record_id)):
        start, end = record.start_at.astimezone(UTC), record.end_at.astimezone(UTC)
        if end <= start:
            raise IntegrityError("Invalid work interval")
        payroll_source = any(
            sources[evidence[ref].source_id].usage in ("payroll", "both") for ref in record.evidence_refs
        )
        scoped_start, scoped_end = max(start, payroll_start), min(end, payroll_end)
        in_payroll = payroll_source and scoped_start < scoped_end
        if in_payroll:
            payroll_records.append((scoped_start, scoped_end))
            record_period = snapshot.period.model_copy(
                update={
                    "start": scoped_start.astimezone(tz).date(),
                    "end": (scoped_end - timedelta(microseconds=1)).astimezone(tz).date(),
                }
            )
            if source_period_mismatch(snapshot, record.evidence_refs, record_period):
                issues.append(f"payroll_source_period_mismatch:{record.record_id}")
        breaks = sorted((a.astimezone(UTC), b.astimezone(UTC)) for a, b in record.breaks)
        if any(a < start or b > end or b <= a for a, b in breaks):
            raise IntegrityError("Invalid break interval")
        overlapping_breaks, latest_break_end = [], None
        for a, b in breaks:
            if latest_break_end is not None and a < latest_break_end:
                overlapping_breaks.append((a, min(b, latest_break_end)))
            latest_break_end = max(b, latest_break_end) if latest_break_end is not None else b
        if overlapping_breaks:
            issues.append(f"break_overlap:{record.record_id}")
            if in_payroll and any(max(a, payroll_start) < min(b, payroll_end) for a, b in overlapping_breaks):
                issues.append(f"payroll_break_overlap:{record.record_id}")
        if not record.break_location_known:
            issues.append(f"break_location_unknown:{record.record_id}")
            if in_payroll:
                issues.append(f"payroll_break_location_unknown:{record.record_id}")
        boundaries = {start, end, *(x for pair in breaks for x in pair)}
        day = start.astimezone(tz).date() + timedelta(days=1)
        while datetime.combine(day, time.min, tz).astimezone(UTC) < end:
            boundaries.add(datetime.combine(day, time.min, tz).astimezone(UTC))
            day += timedelta(days=1)
        for a, b in zip(sorted(boundaries), sorted(boundaries)[1:]):
            if (b - a).total_seconds() % 60:
                raise IntegrityError("Work intervals must use whole minutes")
            on_break = any(c <= a and b <= d for c, d in breaks)
            intervals.append(
                {
                    "interval_id": digest([record.record_id, a.isoformat(), b.isoformat()]),
                    "origin_record_id": record.record_id,
                    "start": a.isoformat(),
                    "end": b.isoformat(),
                    "minutes": int((b - a).total_seconds() // 60),
                    "on_break": on_break,
                    "payroll": payroll_source and payroll_start <= a and b <= payroll_end,
                    "evidence_refs": sorted(record.evidence_refs),
                }
            )
    records = sorted(snapshot.work_records, key=lambda r: r.start_at)
    for left, right in zip(records, records[1:]):
        if left.end_at > right.start_at:
            issues.append("overlapping_work_sources")
    payroll_records.sort()
    if any(left[1] > right[0] for left, right in zip(payroll_records, payroll_records[1:])):
        issues.append("overlapping_payroll_work_sources")
    for required in snapshot.context_requirements:
        if not any(c.start <= required.start and c.end >= required.end for c in snapshot.coverage):
            issues.append(f"missing_context:{required.start}:{required.end}")
    issues.sort()
    dependencies.sort()
    context_hash = digest(
        {
            "snapshot": snapshot.snapshot_hash,
            "intervals": intervals,
            "issues": issues,
            "normalizer": "normalizer-v1",
        }
    )
    return CalculationContext(
        snapshot_hash=snapshot.snapshot_hash,
        context_hash=context_hash,
        snapshot=snapshot,
        normalized_intervals=intervals,
        issues=issues,
        dependencies=dependencies,
    )
