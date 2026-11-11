"""Source-backed standard wages. Rates are Decimal; only each final item is rounded.

The first supported contract is an unchanged adult standard schedule, at most
8 contractual hours/day and 40/week. Special schedules, uncertain day categories,
partial monthly salary and unconfirmed fixed-allowance offsets are not inferred.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from agent.rules.comparisons import comparison, field
from agent.rules.registry import WAGE_RULE_IDS, RuleRegistry, VerifiedRule
from agent.schemas.public.base import Period
from agent.schemas.public.results import WageComparisonItem, WageItemResult, WageSection
from agent.services.calculation_context import source_period_mismatch

RULE_VERSION = "standard-2026-v1"
BUNDLE_VERSION = "standard-wages-v1"
MANIFEST_VERSION = "standard-wages-manifest-v1"
OFFICIAL_SOURCES = {
    "W-01": ("https://www.law.go.kr/lsLinkCommonInfo.do?lsJoLnkSeq=1034322425",),
    "W-02": ("https://moel.go.kr/mainpop2.do", "https://1350.moel.go.kr/rtmview.do?id=1000058972"),
    "W-03": ("https://www.law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1034323091",),
    "W-04": ("https://www.law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1034323091",),
    "W-05": ("https://www.law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1034323091",),
}
ORDINARY_SOURCE = "https://law.go.kr/LSW/lsInfoP.do?lsId=003058"


class WageBlocked(Exception):
    def __init__(self, reason, fields=(), periods=(), *, conflict=False):
        self.reason, self.fields, self.periods, self.conflict = reason, fields, periods, conflict


def _value(conditions, key):
    item = getattr(conditions, key)
    if item.state != "known":
        raise WageBlocked(
            "CONDITION_" + item.state.upper(), ["wage_conditions." + key], conflict=item.state == "conflict"
        )
    return item.value


def _true(conditions, key, reason):
    if _value(conditions, key) is not True:
        raise WageBlocked(reason, ["wage_conditions." + key], conflict=True)


def _round(value):
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _days(start, end):
    while start <= end:
        yield start
        start += timedelta(days=1)


def _week(day, schedule):
    return day - timedelta(days=(day.weekday() - schedule.week_start) % 7)


def _full_time_schedule(schedule):
    # Short-time workers have a separate contractual-overtime rule. Do not apply
    # an 8h/40h threshold to them as if they were standard full-time workers.
    if len(schedule.days) != 5 or any(d.minutes != 480 for d in schedule.days):
        raise WageBlocked("STANDARD_PROFILE_UNSUPPORTED", ["wage_conditions.schedule"], conflict=True)


def _covered(snapshot, start, end):
    remaining = start
    for period in sorted(snapshot.coverage, key=lambda p: p.start):
        if period.start > remaining:
            return False
        remaining = max(remaining, period.end + timedelta(days=1))
        if remaining > end:
            return True
    return False


def _day(conditions, day):
    item = next((d for d in conditions.days if d.date == day), None)
    if item is None or not item.confirmed:
        raise WageBlocked(
            "HOLIDAY_CLASSIFICATION_MISSING", ["wage_conditions.days"], [Period(start=day, end=day)]
        )
    if item.attendance in {"unknown", "pending", "conflict"}:
        raise WageBlocked(
            "WEEK_ATTENDANCE_UNCONFIRMED",
            ["wage_conditions.days"],
            [Period(start=day, end=day)],
            conflict=item.attendance == "conflict",
        )
    return item


def _ordinary(snapshot):
    c = snapshot.wage_conditions
    if c.ordinary_hourly_rate.state == "known":
        return c.ordinary_hourly_rate.value, {"ordinary_hourly_rate": str(c.ordinary_hourly_rate.value)}
    if c.ordinary_hourly_rate.state in {"conflict", "pending", "unknown"}:
        _value(c, "ordinary_hourly_rate")
    if snapshot.profile == "M-STD":
        amount = _value(c, "ordinary_monthly_amount")
        minutes = _value(c, "monthly_standard_minutes")
        return amount * Decimal(60) / Decimal(minutes), {
            "ordinary_monthly_amount": str(amount),
            "monthly_standard_minutes": str(minutes),
        }
    raise WageBlocked("ORDINARY_RATE_UNCONFIRMED", ["wage_conditions.ordinary_hourly_rate"])


def _work(context, *, schedule=None, weeks=False, calendar=True):
    """Minute classification keeps shift-day overtime and clock-based night distinct.

    Boundary context contributes to weekly thresholds but never to payable minutes.
    Overnight shifts belong to their start day for daily overtime. A holiday change
    across midnight is deliberately reviewed rather than silently reclassified.
    """
    s, c = context.snapshot, context.snapshot.wage_conditions
    _true(c, "records_complete", "WORK_RECORDS_INCOMPLETE")
    if not _covered(s, s.period.start, s.period.end):
        raise WageBlocked("WORK_RECORDS_INCOMPLETE", ["coverage"], [s.period])
    relevant_issues = ("payroll_break_", "payroll_source_period_mismatch:")
    if "overlapping_payroll_work_sources" in context.issues or any(
        i.startswith(relevant_issues) for i in context.issues
    ):
        raise WageBlocked("WORK_SOURCE_CONFLICT", ["work_records"], conflict=True)
    if weeks:
        start, end = _week(s.period.start, schedule), _week(s.period.end, schedule) + timedelta(days=6)
        if not _covered(s, start, end):
            missing = (
                [Period(start=start, end=s.period.start - timedelta(days=1))]
                if start < s.period.start
                else []
            )
            if end > s.period.end:
                missing.append(Period(start=s.period.end + timedelta(days=1), end=end))
            raise WageBlocked(
                "WEEK_CONTEXT_MISSING",
                ["coverage", "work_records"],
                missing or [Period(start=start, end=end)],
            )
        if (
            any(i.startswith(("break_overlap:", "break_location_unknown:")) for i in context.issues)
            or "overlapping_work_sources" in context.issues
        ):
            raise WageBlocked("WORK_SOURCE_CONFLICT", ["work_records"], conflict=True)
    else:
        start, end = s.period.start, s.period.end
    tz = ZoneInfo(s.timezone)
    origins = {r.record_id: r.start_at.astimezone(tz).date() for r in s.work_records}
    payroll_records = {i["origin_record_id"] for i in context.normalized_intervals if i["payroll"]}
    present_dates = {
        origins[i["origin_record_id"]] for i in context.normalized_intervals if not i["on_break"]
    }
    for entry in c.days:
        if (
            start <= entry.date <= end
            and entry.confirmed
            and entry.attendance == "present"
            and entry.date not in present_dates
        ):
            raise WageBlocked(
                "WORK_RECORDS_INCOMPLETE", ["work_records"], [Period(start=entry.date, end=entry.date)]
            )
    daily, weekly, holiday_daily = defaultdict(int), defaultdict(int), defaultdict(int)
    result = dict(regular=0, overtime=0, night=0, holiday_first=0, holiday_extra=0, actual=0)
    for interval in sorted(context.normalized_intervals, key=lambda i: (i["start"], i["origin_record_id"])):
        if interval["on_break"]:
            continue
        origin = origins[interval["origin_record_id"]]
        if not weeks and interval["origin_record_id"] not in payroll_records:
            continue
        if weeks and not start <= origin <= end and interval["origin_record_id"] not in payroll_records:
            continue
        day = _day(c, origin) if calendar else None
        if day is not None and day.attendance == "absent":
            raise WageBlocked("CONDITION_CONFLICT", ["work_records", "wage_conditions.days"], conflict=True)
        is_holiday = day is not None and day.kind in {"weekly_rest", "paid_holiday"}
        tick, finish = (
            datetime.fromisoformat(interval["start"]).astimezone(tz),
            datetime.fromisoformat(interval["end"]).astimezone(tz),
        )
        while tick < finish:
            payable = interval["payroll"]
            if calendar and tick.date() != origin:
                next_day = _day(c, tick.date())
                if (next_day.kind in {"weekly_rest", "paid_holiday"}) != is_holiday:
                    raise WageBlocked(
                        "HOLIDAY_BOUNDARY_REVIEW", ["work_records", "wage_conditions.days"], conflict=True
                    )
            if payable:
                result["actual"] += 1
                if tick.time() >= time(22) or tick.time() < time(6):
                    result["night"] += 1
            daily[origin] += 1
            if schedule and daily[origin] <= 480:
                weekly[_week(origin, schedule)] += 1
            if is_holiday:
                holiday_daily[origin] += 1
                if payable:
                    result["holiday_first" if holiday_daily[origin] <= 480 else "holiday_extra"] += 1
            elif payable:
                overtime = daily[origin] > 480 or (weeks and weekly[_week(origin, schedule)] > 2400)
                result["overtime" if overtime else "regular"] += 1
            tick += timedelta(minutes=1)
    return result


def _weekly_rest(context, schedule):
    s, c = context.snapshot, context.snapshot.wage_conditions
    scheduled = {d.weekday: d.minutes for d in schedule.days}
    if sum(scheduled.values()) < 900:
        return 0, []
    minutes, refs = 0, []
    for day in _days(s.period.start, s.period.end):
        if day.weekday() != schedule.weekly_rest_weekday:
            continue
        rest = _day(c, day)
        if rest.kind != "weekly_rest":
            raise WageBlocked("WEEK_ATTENDANCE_UNCONFIRMED", ["wage_conditions.days"], conflict=True)
        start = _week(day, schedule)
        end = start + timedelta(days=6)
        if not _covered(s, start, end):
            raise WageBlocked(
                "WEEK_CONTEXT_MISSING", ["coverage", "wage_conditions.days"], [Period(start=start, end=end)]
            )
        qualified = True
        for check in _days(start, end):
            entry = _day(c, check)
            refs.extend(entry.evidence_refs)
            if check.weekday() in scheduled and entry.kind == "workday":
                if entry.attendance == "absent":
                    qualified = False
                elif entry.attendance not in {"present", "paid_leave"}:
                    raise WageBlocked(
                        "WEEK_ATTENDANCE_UNCONFIRMED",
                        ["wage_conditions.days"],
                        [Period(start=check, end=check)],
                    )
        if qualified:
            minutes += schedule.weekly_rest_minutes
    return minutes, refs


def _contract(snapshot):
    unit = "KRW/month" if snapshot.profile == "M-STD" else "KRW/hour"
    amount, refs, reasons, conflict = field(snapshot, "contract_rate", unit)
    if reasons or amount is None or amount <= 0:
        raise WageBlocked(reasons[0] if reasons else "FIELD_MISSING", ["contract_rate"], conflict=conflict)
    return amount, refs


def _included(c, rule_id):
    entry = next((a for a in c.included_allowances if a.rule_id == rule_id), None)
    if entry is None:
        return Decimal(0)
    if entry.state != "known":
        raise WageBlocked(
            "INCLUDED_ALLOWANCE_UNCONFIRMED",
            ["wage_conditions.included_allowances"],
            conflict=entry.state == "conflict",
        )
    return entry.amount


def _basis_refs(context, rule_id, operands, initial):
    """Retain only facts actually used, and validate each scalar's source period."""
    s, c = context.snapshot, context.snapshot.wage_conditions
    keys = {"standard_employment", "contract_unchanged"}
    if rule_id == "W-01":
        keys.add("schedule")
        if s.profile == "M-STD":
            keys.update(("full_period_employment", "composition_complete", "records_complete"))
        else:
            keys.add("records_complete")
    elif rule_id == "W-02":
        keys.update(("schedule", "full_period_employment"))
        if s.profile == "M-STD":
            keys.add("monthly_weekly_rest_included")
    else:
        keys.update(("five_or_more_workers", "records_complete", "composition_complete"))
        if rule_id == "W-03":
            keys.add("schedule")
    keys.update(
        key
        for key in ("ordinary_hourly_rate", "ordinary_monthly_amount", "monthly_standard_minutes")
        if key in operands
    )
    refs = list(initial)
    for key in sorted(keys):
        evidence = getattr(c, key).evidence_refs
        if source_period_mismatch(s, evidence):
            raise WageBlocked("SOURCE_PERIOD_MISMATCH", ["wage_conditions." + key], conflict=True)
        refs.extend(evidence)
    for allowance in c.included_allowances:
        if rule_id == "W-01" or rule_id == allowance.rule_id:
            if source_period_mismatch(s, allowance.evidence_refs):
                raise WageBlocked(
                    "SOURCE_PERIOD_MISMATCH", ["wage_conditions.included_allowances"], conflict=True
                )
            refs.extend(allowance.evidence_refs)
    if rule_id != "W-04":
        for day in c.days:
            needed = (
                s.period.start <= day.date <= s.period.end
                or rule_id in {"W-02", "W-03"}
                or (rule_id == "W-01" and s.profile == "H-STD")
            )
            if needed:
                if source_period_mismatch(s, day.evidence_refs, Period(start=day.date, end=day.date)):
                    raise WageBlocked(
                        "SOURCE_PERIOD_MISMATCH",
                        ["wage_conditions.days"],
                        [Period(start=day.date, end=day.date)],
                        conflict=True,
                    )
                refs.extend(day.evidence_refs)
    if rule_id != "W-02":
        refs.extend(
            ref
            for interval in context.normalized_intervals
            if interval["payroll"] or rule_id == "W-03" or (rule_id == "W-01" and s.profile == "H-STD")
            for ref in interval["evidence_refs"]
        )
    return sorted(set(refs))


def evaluate_wage(rule_id, context):
    s, c = context.snapshot, context.snapshot.wage_conditions
    common = dict(
        rule_id=rule_id,
        rule_version=RULE_VERSION,
        official_sources=[*OFFICIAL_SOURCES[rule_id], ORDINARY_SOURCE],
    )
    if not date(2025, 1, 1) <= s.period.start <= s.period.end <= date(2026, 12, 31):
        return WageItemResult(**common, status="needs_review", reason_codes=["RULE_PERIOD_UNSUPPORTED"])
    try:
        if c is None:
            raise WageBlocked("WAGE_CONDITIONS_MISSING", ["wage_conditions"])
        if s.timezone != "Asia/Seoul":
            raise WageBlocked("STANDARD_PROFILE_UNSUPPORTED", ["timezone"], conflict=True)
        _true(c, "standard_employment", "STANDARD_PROFILE_UNSUPPORTED")
        _true(c, "contract_unchanged", "CONTRACT_CHANGED")
        if c.calculation_period != s.period:
            raise WageBlocked(
                "PERIOD_MISMATCH",
                ["wage_conditions.calculation_period"],
                conflict=c.calculation_period is not None,
            )
        refs = []
        operands, included, additional, payable = {}, Decimal(0), None, None
        if rule_id == "W-01":
            contract, contract_refs = _contract(s)
            refs += contract_refs
            schedule = _value(c, "schedule")
            _full_time_schedule(schedule)
            if s.profile == "M-STD":
                _true(c, "full_period_employment", "MONTHLY_PRORATION_UNSUPPORTED")
                _true(c, "composition_complete", "WAGE_COMPOSITION_UNCONFIRMED")
                _true(c, "records_complete", "WORK_RECORDS_INCOMPLETE")
                if not 28 <= (s.period.end - s.period.start).days + 1 <= 31:
                    raise WageBlocked(
                        "MONTHLY_PRORATION_UNSUPPORTED", ["wage_conditions.calculation_period"], conflict=True
                    )
                for day in _days(s.period.start, s.period.end):
                    entry = _day(c, day)
                    if entry.kind == "workday" and entry.attendance not in {"present", "paid_leave"}:
                        raise WageBlocked(
                            "MONTHLY_PRORATION_UNSUPPORTED", ["wage_conditions.days"], conflict=True
                        )
                    if entry.kind == "workday" and entry.attendance == "present":
                        tz = ZoneInfo(s.timezone)
                        record_ids = {
                            r.record_id for r in s.work_records if r.start_at.astimezone(tz).date() == day
                        }
                        actual = sum(
                            i["minutes"]
                            for i in context.normalized_intervals
                            if i["origin_record_id"] in record_ids and not i["on_break"]
                        )
                        expected = next((d.minutes for d in schedule.days if d.weekday == day.weekday()), 0)
                        if actual < expected:
                            raise WageBlocked(
                                "MONTHLY_PRORATION_UNSUPPORTED",
                                ["work_records", "wage_conditions.days"],
                                [Period(start=day, end=day)],
                                conflict=True,
                            )
                if any(a.rule_id in {"W-01", "W-02"} for a in c.included_allowances):
                    raise WageBlocked(
                        "INCLUDED_ALLOWANCE_UNCONFIRMED",
                        ["wage_conditions.included_allowances"],
                        conflict=True,
                    )
                fixed = sum((_included(c, rid) for rid in ("W-03", "W-04", "W-05")), Decimal(0))
                amount = contract - fixed
                if amount < 0:
                    raise WageBlocked(
                        "WAGE_COMPOSITION_UNCONFIRMED", ["wage_conditions.included_allowances"], conflict=True
                    )
                operands = {
                    "monthly_contract_amount": str(contract),
                    "separated_fixed_allowances": str(fixed),
                }
            else:
                work = _work(context, schedule=schedule, weeks=True)
                scheduled = {d.weekday: d.minutes for d in schedule.days}
                if any(
                    d.attendance == "paid_leave" and s.period.start <= d.date <= s.period.end for d in c.days
                ):
                    raise WageBlocked("STANDARD_PROFILE_UNSUPPORTED", ["wage_conditions.days"], conflict=True)
                paid_holiday_minutes = sum(
                    scheduled.get(day.weekday(), 0)
                    for day in _days(s.period.start, s.period.end)
                    if _day(c, day).kind == "paid_holiday"
                )
                amount = contract * Decimal(work["regular"] + paid_holiday_minutes) / Decimal(60)
                operands = {
                    "base_hourly_rate": str(contract),
                    "regular_minutes": str(work["regular"]),
                    "paid_holiday_minutes": str(paid_holiday_minutes),
                }
        elif rule_id == "W-02":
            schedule = _value(c, "schedule")
            _full_time_schedule(schedule)
            if schedule.weekly_rest_minutes != 480:
                raise WageBlocked("CONDITION_CONFLICT", ["wage_conditions.schedule"], conflict=True)
            _true(c, "full_period_employment", "WEEK_ATTENDANCE_UNCONFIRMED")
            minutes, extra_refs = _weekly_rest(context, schedule)
            refs += extra_refs
            rate, operands = _ordinary(s)
            amount = rate * Decimal(minutes) / Decimal(60)
            operands.update(
                weekly_rest_minutes=str(minutes),
                weekly_scheduled_minutes=str(sum(d.minutes for d in schedule.days)),
            )
            if s.profile == "M-STD" and _value(c, "monthly_weekly_rest_included"):
                included = _round(amount)
                payable = Decimal(0)
                operands["included_in_monthly_base"] = "true"
                operands["amount_meaning"] = "weekly_rest_reference_for_eligible_dates"
        else:
            _true(c, "five_or_more_workers", "STANDARD_PROFILE_UNSUPPORTED")
            schedule = _value(c, "schedule") if rule_id == "W-03" else None
            if schedule is not None:
                _full_time_schedule(schedule)
            work = _work(context, schedule=schedule, weeks=rule_id == "W-03", calendar=rule_id != "W-04")
            rate, operands = _ordinary(s)
            if rule_id == "W-03":
                amount = rate * Decimal(work["overtime"]) / Decimal(60) * Decimal("1.5")
                operands.update(overtime_minutes=str(work["overtime"]), multiplier="1.5")
            elif rule_id == "W-04":
                amount = rate * Decimal(work["night"]) / Decimal(60) * Decimal("0.5")
                operands.update(
                    night_minutes=str(work["night"]), multiplier="0.5", overlaps_other_work="true"
                )
            else:
                amount = (
                    rate
                    * (
                        Decimal(work["holiday_first"]) * Decimal("1.5")
                        + Decimal(work["holiday_extra"]) * Decimal(2)
                    )
                    / Decimal(60)
                )
                operands.update(
                    holiday_first_minutes=str(work["holiday_first"]),
                    holiday_extra_minutes=str(work["holiday_extra"]),
                    multipliers="1.5/2",
                )
            _true(c, "composition_complete", "WAGE_COMPOSITION_UNCONFIRMED")
            included = _included(c, rule_id)
            if s.profile == "H-STD" and included:
                raise WageBlocked(
                    "STANDARD_PROFILE_UNSUPPORTED", ["wage_conditions.included_allowances"], conflict=True
                )
            payable = max(_round(amount), included)
        amount = _round(amount)
        additional = max(amount - included, Decimal(0))
        payable = amount if payable is None else payable
        refs = _basis_refs(context, rule_id, operands, refs)
        dependencies = []
        sources = {src.source_id: src for src in s.authorized_sources}
        evidence = {ev.evidence_id: ev for ev in s.evidence_catalog}
        for ref in refs:
            source = sources[evidence[ref].source_id]
            dependencies.append("source:" + source.source_id)
        return WageItemResult(
            **common,
            status="not_applicable" if rule_id != "W-01" and amount == 0 and included == 0 else "calculated",
            amount=amount,
            included_amount=included,
            additional_amount=additional,
            payable_amount=payable,
            formula_id=f"{s.profile}/{rule_id}:{RULE_VERSION}",
            operands=operands,
            evidence_refs=sorted(set(refs)),
            dependencies=sorted(set(dependencies)),
        )
    except WageBlocked as blocked:
        return WageItemResult(
            **common,
            status="needs_review" if blocked.conflict else "insufficient",
            reason_codes=[blocked.reason],
            required_field_ids=list(blocked.fields),
            required_periods=list(blocked.periods),
        )


def wage_section(items, snapshot):
    subtotal = sum(
        (i.payable_amount if i.payable_amount is not None else i.amount or Decimal(0) for i in items),
        Decimal(0),
    )
    c = snapshot.wage_conditions
    complete = bool(
        c
        and c.composition_complete.state == "known"
        and c.composition_complete.value
        and len(items) == 5
        and all(i.status in {"calculated", "not_applicable"} and i.payable_amount is not None for i in items)
    )
    return WageSection(
        items=items, subtotal=subtotal, gross_total=subtotal if complete else None, scope_complete=complete
    )


def compare_wages(context, section):
    s, c = context.snapshot, context.snapshot.wage_conditions
    if c is None or not c.statement_mappings:
        return comparison("R-07", reasons=["WAGE_MAPPING_UNCONFIRMED"])
    wages = {i.rule_id: i for i in section.items}
    statement = {i.item_id: i for i in s.earnings if not i.is_total}
    used_rules, used_items, details = set(), set(), []
    for mapping in c.statement_mappings:
        reasons = []
        if mapping.state != "known":
            reasons.append("WAGE_MAPPING_UNCONFIRMED")
        if mapping.period != s.period or not mapping.rule_ids or not mapping.statement_item_ids:
            reasons.append("WAGE_MAPPING_SCOPE_MISMATCH")
        if (
            len(set(mapping.rule_ids)) != len(mapping.rule_ids)
            or len(set(mapping.statement_item_ids)) != len(mapping.statement_item_ids)
            or used_rules.intersection(mapping.rule_ids)
            or used_items.intersection(mapping.statement_item_ids)
        ):
            reasons.append("WAGE_MAPPING_SCOPE_MISMATCH")
        selected_wages = [wages.get(rid) for rid in mapping.rule_ids]
        selected_items = [statement.get(iid) for iid in mapping.statement_item_ids]
        refs = list(mapping.evidence_refs)
        if any(
            w is None or w.status not in {"calculated", "not_applicable"} or w.payable_amount is None
            for w in selected_wages
        ):
            reasons.append("REQUIRED_INPUT_MISSING")
        if any(i is None or not i.confirmed or i.amount is None for i in selected_items):
            reasons.append("ITEM_SCOPE_INCOMPLETE")
        for item in [*selected_wages, *selected_items]:
            if item is not None:
                refs.extend(item.evidence_refs)
        comparison_refs = mapping.evidence_refs + [
            ref for item in selected_items if item is not None for ref in item.evidence_refs
        ]
        if source_period_mismatch(s, comparison_refs):
            reasons.append("SOURCE_PERIOD_MISMATCH")
        lhs = sum((w.payable_amount for w in selected_wages), Decimal(0)) if not reasons else None
        rhs = sum((i.amount for i in selected_items), Decimal(0)) if not reasons else None
        result = comparison(
            "R-07",
            lhs,
            rhs,
            refs=refs,
            reasons=reasons,
            conflict=any(r in {"WAGE_MAPPING_SCOPE_MISMATCH", "SOURCE_PERIOD_MISMATCH"} for r in reasons)
            or mapping.state == "conflict",
        )
        # A wage basis sourced from a mapped statement is a dependent comparison.
        wage_sources = {
            e.source_id
            for e in s.evidence_catalog
            if any(w and e.evidence_id in w.evidence_refs for w in selected_wages)
        }
        statement_sources = {
            e.source_id
            for e in s.evidence_catalog
            if any(i and e.evidence_id in i.evidence_refs for i in selected_items)
        }
        details.append(
            WageComparisonItem(
                rule_ids=mapping.rule_ids,
                statement_item_ids=mapping.statement_item_ids,
                status=result.status,
                lhs=result.lhs,
                rhs=result.rhs,
                delta=result.delta,
                reason_codes=result.reason_codes,
                evidence_refs=result.evidence_refs,
                independent=not bool(wage_sources & statement_sources),
            )
        )
        used_rules.update(mapping.rule_ids)
        used_items.update(mapping.statement_item_ids)
    complete = (
        section.scope_complete
        and used_rules == set(WAGE_RULE_IDS)
        and s.earnings_complete
        and used_items == set(statement)
        and all(i.status in {"match", "difference"} for i in details)
    )
    total = comparison(
        "R-07",
        sum((i.lhs for i in details), Decimal(0)) if complete else None,
        sum((i.rhs for i in details), Decimal(0)) if complete else None,
        refs=[ref for i in details for ref in i.evidence_refs],
        reasons=[] if complete else ["WAGE_MAPPING_SCOPE_MISMATCH"],
        conflict=any(i.status == "needs_review" for i in details),
    )
    total.wage_items = details
    if complete and any(item.status == "difference" for item in details):
        total.status = "difference"
        if total.delta == 0:
            total.reason_codes = ["WAGE_COMPONENT_DIFFERENCE"]
    return total


class StandardWageRegistry(RuleRegistry):
    supported_r07 = True

    def evaluate(self, rule_id, context):
        return evaluate_wage(rule_id, context)


def default_rule_registry():
    rules = [
        VerifiedRule(
            rule_id=rid,
            version=RULE_VERSION,
            profile=profile,
            effective_from=date(2025, 1, 1),
            effective_to=date(2026, 12, 31),
            required_fields=(),
            official_sources=OFFICIAL_SOURCES[rid] + (ORDINARY_SOURCE,),
            verification_cases=("H-STD-independent", "MONTHLY-202604-M026", "standard-boundaries"),
            verification_status="passed",
            formula_id=f"{profile}/{rid}:{RULE_VERSION}",
            calculate=lambda context: Decimal(0),
        )
        for profile in ("H-STD", "M-STD")
        for rid in WAGE_RULE_IDS
    ]
    return StandardWageRegistry(rules, bundle_version=BUNDLE_VERSION, manifest_version=MANIFEST_VERSION)
