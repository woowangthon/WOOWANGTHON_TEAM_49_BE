"""Authored expectations, independent of the wage implementation and any AI output."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from agent.rules.wages import compare_wages, default_rule_registry, wage_section
from agent.schemas.public.base import Period
from agent.schemas.public.inputs import InputSnapshot
from agent.services.calculation_context import build_calculation_context
from agent.services.hashing import snapshot_hash


def known(value, ref="contract"):
    return dict(state="known", value=value, confirmation_id="confirmed-test", evidence_refs=[ref])


def standard_snapshot(profile="M-STD"):
    period = dict(start="2026-04-01", end="2026-04-30")
    records, days = [], []
    day = date(2026, 3, 30)
    while day <= date(2026, 5, 3):
        kind = "workday" if day.weekday() < 5 else "unpaid_rest" if day.weekday() == 5 else "weekly_rest"
        if day == date(2026, 5, 1):
            kind = "paid_holiday"
        works = kind == "workday" or day == date(2026, 4, 19)
        days.append(
            dict(
                date=day.isoformat(),
                kind=kind,
                attendance="present" if works else "not_scheduled",
                confirmed=True,
                evidence_refs=["attendance"],
            )
        )
        if works:
            start, end = "09:00", "18:00"
            end_day = day
            breaks = [("12:00", "13:00")]
            if day == date(2026, 4, 8):
                end = "21:00"
                breaks.append(("18:00", "18:30"))
            if day == date(2026, 4, 22):
                end, end_day = "00:30", day + timedelta(days=1)
                breaks.extend([("18:00", "18:30"), ("23:00", "23:30")])
            if day == date(2026, 4, 19):
                end, breaks = "13:30", [("11:00", "11:30")]
            records.append(
                dict(
                    record_id=str(day),
                    start_at=f"{day}T{start}:00+09:00",
                    end_at=f"{end_day}T{end}:00+09:00",
                    breaks=[(f"{day}T{a}:00+09:00", f"{day}T{b}:00+09:00") for a, b in breaks],
                    evidence_refs=["attendance"],
                )
            )
        day += timedelta(days=1)
    conditions = {
        name: known(True)
        for name in (
            "standard_employment",
            "contract_unchanged",
            "five_or_more_workers",
            "full_period_employment",
            "records_complete",
            "composition_complete",
            "monthly_weekly_rest_included",
        )
    }
    conditions.update(
        schedule=known(
            dict(
                days=[dict(weekday=d, minutes=480) for d in range(5)],
                week_start=0,
                weekly_rest_weekday=6,
                weekly_rest_minutes=480,
            )
        ),
        days=days,
        calculation_period=period,
        ordinary_monthly_amount=known("3000000"),
        monthly_standard_minutes=known(12540),
    )
    if profile == "H-STD":
        conditions["ordinary_hourly_rate"] = known("10000")
    expected = (
        ["3000000", "172249", "14354", "86124"]
        if profile == "M-STD"
        else ["2080000", "120000", "10000", "60000"]
    )
    conditions["statement_mappings"] = [
        dict(
            rule_ids=rules,
            statement_item_ids=[f"item-{i}"],
            period=period,
            state="known",
            evidence_refs=["slip"],
        )
        for i, rules in enumerate([["W-01", "W-02"], ["W-03"], ["W-04"], ["W-05"]])
    ]
    s = InputSnapshot(
        snapshot_id="snapshot",
        owner_id="owner",
        workplace_id="workplace",
        case_id="case",
        input_version=1,
        period=period,
        profile=profile,
        confirmed_fields=[
            dict(
                field_id="contract_rate",
                value="3000000" if profile == "M-STD" else "10000",
                unit="KRW/month" if profile == "M-STD" else "KRW/hour",
                confirmation_id="confirm",
                evidence_refs=["contract"],
                period=period,
            )
        ],
        authorized_sources=[
            dict(
                source_id=name,
                owner_id="owner",
                workplace_id="workplace",
                link_id=name,
                link_version=1,
                document_id=name,
                document_version=1,
                content_hash="test-" + name,
                usage="both",
                kind="manual",
            )
            for name in ("contract", "attendance", "slip")
        ],
        evidence_catalog=[
            dict(
                evidence_id=name,
                source_id=name,
                quote="Independent authored fixture",
                verification_method="manual_confirmed",
            )
            for name in ("contract", "attendance", "slip")
        ],
        earnings=[
            dict(item_id=f"item-{i}", amount=value, evidence_refs=["slip"])
            for i, value in enumerate(expected)
        ],
        earnings_complete=True,
        work_records=records,
        coverage=[dict(start="2026-03-30", end="2026-05-03")],
        wage_conditions=conditions,
        rule_bundle_version="standard-wages-v1",
        support_manifest_version="standard-wages-manifest-v1",
    )
    return s


def run(snapshot):
    snapshot.snapshot_hash = snapshot_hash(snapshot)
    context = build_calculation_context(snapshot, "owner", "case")
    registry = default_rule_registry()
    items = [registry.evaluate(f"W-0{i}", context) for i in range(1, 6)]
    return context, wage_section(items, snapshot)


def test_monthly_april_authored_expected_values():
    context, result = run(standard_snapshot())
    assert [i.status for i in result.items] == ["calculated"] * 5
    assert [i.amount for i in result.items] == list(
        map(Decimal, ["3000000", "459330", "172249", "14354", "86124"])
    )
    assert result.items[1].included_amount == Decimal("459330")
    assert result.items[1].payable_amount == 0
    assert result.items[2].operands["overtime_minutes"] == "480"
    assert result.items[3].operands["night_minutes"] == "120"
    assert result.gross_total == Decimal("3272727")
    comparison = compare_wages(context, result)
    assert comparison.status == "match"
    assert comparison.lhs == Decimal("3272727")
    assert all(item.independent for item in comparison.wage_items)


def test_hourly_april_all_five_rules_have_independent_nonzero_answers():
    context, result = run(standard_snapshot("H-STD"))
    assert [i.amount for i in result.items] == list(
        map(Decimal, ["1760000", "320000", "120000", "10000", "60000"])
    )
    assert result.gross_total == Decimal("2270000")
    assert compare_wages(context, result).status == "match"


@pytest.mark.parametrize(
    "included,contract,total,additional",
    [("100000", "3100000", "3272727", "72249"), ("200000", "3200000", "3300478", "0")],
)
def test_monthly_fixed_allowance_not_double_counted(included, contract, total, additional):
    snapshot = standard_snapshot()
    snapshot.confirmed_fields[0].value = Decimal(contract)
    snapshot.wage_conditions.included_allowances = [
        dict(rule_id="W-03", amount=included, state="known", evidence_refs=["contract"])
    ]
    _, result = run(snapshot)
    assert result.items[0].amount == Decimal("3000000")
    assert result.items[2].included_amount == Decimal(included)
    assert result.items[2].additional_amount == Decimal(additional)
    assert result.gross_total == Decimal(total)


def test_divisor_is_confirmed_not_unconditional_209():
    snapshot = standard_snapshot()
    snapshot.wage_conditions.monthly_standard_minutes.value = 12000
    _, result = run(snapshot)
    assert result.gross_total == Decimal("3285000")
    snapshot.wage_conditions.monthly_standard_minutes = dict(state="unknown", value=None)
    _, result = run(snapshot)
    assert result.items[0].amount == Decimal("3000000")
    assert all(i.status == "insufficient" for i in result.items[1:])
    assert result.gross_total is None


def test_missing_boundary_week_only_blocks_dependent_rules():
    snapshot = standard_snapshot()
    snapshot.coverage = [Period(start="2026-04-01", end="2026-04-30")]
    _, result = run(snapshot)
    assert [i.status for i in result.items] == [
        "calculated",
        "insufficient",
        "insufficient",
        "calculated",
        "calculated",
    ]
    assert result.items[1].required_periods
    assert result.items[3].amount == Decimal("14354")
    assert result.gross_total is None


def test_unknown_five_workers_does_not_block_base_or_weekly_rest():
    snapshot = standard_snapshot()
    snapshot.wage_conditions.five_or_more_workers = dict(state="unknown", value=None)
    _, result = run(snapshot)
    assert [i.status for i in result.items] == [
        "calculated",
        "calculated",
        "insufficient",
        "insufficient",
        "insufficient",
    ]
    assert result.gross_total is None


def test_daily_and_weekly_overtime_are_union_not_double_count():
    snapshot = standard_snapshot("H-STD")
    record = next(r for r in snapshot.work_records if r.record_id == "2026-04-08")
    record.end_at = datetime.fromisoformat("2026-04-08T20:00:00+09:00")
    record.breaks = record.breaks[:1]
    snapshot.work_records = [r for r in snapshot.work_records if r.record_id != "2026-04-22"]
    # Replace the second long day with an ordinary 8-hour day.
    original = next(r for r in standard_snapshot("H-STD").work_records if r.record_id == "2026-04-22")
    original.end_at = datetime.fromisoformat("2026-04-22T18:00:00+09:00")
    original.breaks = original.breaks[:1]
    snapshot.work_records.append(original)
    _, result = run(snapshot)
    assert result.items[2].operands["overtime_minutes"] == "120"
    assert result.items[2].amount == Decimal("30000")


def test_mapping_difference_and_unknown_component_preserve_other_details():
    snapshot = standard_snapshot()
    snapshot.earnings[2].amount = Decimal("14000")
    context, result = run(snapshot)
    mapped = compare_wages(context, result)
    assert mapped.status == "difference" and mapped.delta == Decimal("354")
    snapshot.wage_conditions.statement_mappings[1].state = "unknown"
    context, result = run(snapshot)
    mapped = compare_wages(context, result)
    assert mapped.lhs is None
    assert mapped.wage_items[0].status == "match"
    assert mapped.wage_items[2].status == "difference"


def test_wrong_mapping_period_and_duplicate_scope_are_not_compared():
    snapshot = standard_snapshot()
    snapshot.wage_conditions.statement_mappings[0].period = Period(start="2026-03-01", end="2026-03-31")
    snapshot.wage_conditions.statement_mappings[1].statement_item_ids = ["item-0"]
    context, result = run(snapshot)
    comparison = compare_wages(context, result)
    assert comparison.status == "needs_review"
    assert comparison.wage_items[0].delta is None
    assert comparison.wage_items[1].delta is None


def test_missing_condition_is_not_confirmed_zero_and_false_is_not_missing():
    snapshot = standard_snapshot()
    snapshot.wage_conditions.standard_employment = dict(
        state="known", value=False, evidence_refs=["contract"]
    )
    _, result = run(snapshot)
    assert all(i.status == "needs_review" and i.amount is None for i in result.items)
    assert result.gross_total is None


def test_context_only_minutes_do_not_inflate_monthly_pay():
    snapshot = standard_snapshot("H-STD")
    _, result = run(snapshot)
    assert result.items[0].operands["regular_minutes"] == "10560"
    assert len(snapshot.work_records) == 25  # 22 payroll weekdays + holiday + 2 context weekdays.


def test_unconfirmed_source_or_inclusion_is_rejected_or_blocks():
    snapshot = standard_snapshot()
    snapshot.wage_conditions.included_allowances = [dict(rule_id="W-03", amount="100000", state="pending")]
    _, result = run(snapshot)
    assert result.items[0].amount is None and result.items[2].amount is None
    assert result.items[3].amount == Decimal("14354")


def test_holiday_over_eight_hours_uses_two_tiers_without_overtime_duplicate():
    snapshot = standard_snapshot("H-STD")
    holiday = next(r for r in snapshot.work_records if r.record_id == "2026-04-19")
    holiday.end_at = datetime.fromisoformat("2026-04-19T20:00:00+09:00")
    holiday.breaks = [
        (
            datetime.fromisoformat("2026-04-19T12:00:00+09:00"),
            datetime.fromisoformat("2026-04-19T13:00:00+09:00"),
        )
    ]
    _, result = run(snapshot)
    assert result.items[4].amount == Decimal("160000")  # 8h*15000 + 2h*20000
    assert result.items[2].amount == Decimal("120000")  # Weekday overtime unchanged.


def test_payroll_end_clips_overnight_minutes_without_clipping_daily_context():
    snapshot = standard_snapshot("H-STD")
    snapshot.period = Period(start="2026-04-01", end="2026-04-22")
    snapshot.wage_conditions.calculation_period = snapshot.period
    _, result = run(snapshot)
    assert result.items[2].operands["overtime_minutes"] == "450"
    assert result.items[3].operands["night_minutes"] == "90"
    assert result.items[3].amount == Decimal("7500")


def test_previous_period_shift_tail_enters_payroll_once():
    from agent.schemas.public.inputs import WorkRecord

    snapshot = standard_snapshot("H-STD")
    snapshot.work_records.append(
        WorkRecord(
            record_id="boundary",
            start_at="2026-03-31T23:00:00+09:00",
            end_at="2026-04-01T02:00:00+09:00",
            evidence_refs=["attendance"],
        )
    )
    _, result = run(snapshot)
    assert result.items[2].operands["overtime_minutes"] == "600"
    assert result.items[3].operands["night_minutes"] == "240"
    assert result.items[0].operands["regular_minutes"] == "10560"


def test_paid_public_holiday_is_paid_once_without_recorded_work():
    snapshot = standard_snapshot("H-STD")
    holiday_date = date(2026, 4, 10)
    day = next(d for d in snapshot.wage_conditions.days if d.date == holiday_date)
    day.kind, day.attendance = "paid_holiday", "not_scheduled"
    snapshot.work_records = [r for r in snapshot.work_records if r.record_id != str(holiday_date)]
    _, result = run(snapshot)
    assert result.items[0].amount == Decimal("1760000")
    assert result.items[0].operands["paid_holiday_minutes"] == "480"
    assert result.items[4].amount == Decimal("60000")


def test_absence_changes_hourly_weekly_rest_but_monthly_proration_is_reviewed():
    for profile in ("H-STD", "M-STD"):
        snapshot = standard_snapshot(profile)
        next(d for d in snapshot.wage_conditions.days if d.date == date(2026, 4, 10)).attendance = "absent"
        snapshot.work_records = [r for r in snapshot.work_records if r.record_id != "2026-04-10"]
        _, result = run(snapshot)
        assert result.items[1].operands["weekly_rest_minutes"] == "1440"
        if profile == "H-STD":
            assert result.items[0].amount == Decimal("1680000")
            assert result.items[1].amount == Decimal("240000")
        else:
            assert result.items[0].status == "needs_review" and result.gross_total is None


def test_known_zero_minutes_and_unconfirmed_minutes_differ():
    snapshot = standard_snapshot()
    for record in snapshot.work_records:
        if record.record_id in {"2026-04-08", "2026-04-22"}:
            record.end_at = datetime.fromisoformat(record.record_id + "T18:00:00+09:00")
            record.breaks = record.breaks[:1]
    snapshot.work_records = [r for r in snapshot.work_records if r.record_id != "2026-04-19"]
    next(d for d in snapshot.wage_conditions.days if d.date == date(2026, 4, 19)).attendance = "not_scheduled"
    _, result = run(snapshot)
    assert all(i.status == "not_applicable" and i.amount == 0 for i in result.items[2:])
    snapshot.wage_conditions.records_complete = dict(state="pending", value=None)
    _, result = run(snapshot)
    assert all(i.amount is None for i in result.items[2:])


def test_standard_rule_matrix_has_all_required_cells_with_sources():
    from agent.rules.support_manifest import support_manifest

    manifest = support_manifest(default_rule_registry())
    assert len(manifest["cells"]) == 12
    assert manifest["release_ready"]
    assert all(c["verification_status"] == "passed" for c in manifest["cells"])
    assert all(c["official_sources"] and c["verification_cases"] for c in manifest["cells"])


def test_short_time_schedule_not_silently_subject_to_full_time_thresholds():
    snapshot = standard_snapshot("H-STD")
    snapshot.wage_conditions.schedule.value.days[0].minutes = 360
    _, result = run(snapshot)
    assert result.items[0].status == "needs_review"
    assert result.items[2].status == "needs_review"
    assert result.items[3].amount == Decimal("10000")
    assert result.gross_total is None


def test_confirmed_calendar_cannot_hide_a_missing_work_record():
    snapshot = standard_snapshot()
    snapshot.work_records = [r for r in snapshot.work_records if r.record_id != "2026-04-08"]
    _, result = run(snapshot)
    assert result.items[0].status == "needs_review"
    assert result.items[2].amount is None
    assert result.items[3].amount is None
    assert result.gross_total is None


def test_wrong_rate_source_period_only_blocks_rules_using_that_rate():
    snapshot = standard_snapshot()
    source = snapshot.authorized_sources[0].model_copy(
        update={"source_id": "old-rate", "applicable_period": Period(start="2026-03-01", end="2026-03-31")}
    )
    evidence = snapshot.evidence_catalog[0].model_copy(
        update={"evidence_id": "old-rate", "source_id": "old-rate"}
    )
    snapshot.authorized_sources.append(source)
    snapshot.evidence_catalog.append(evidence)
    snapshot.wage_conditions.ordinary_monthly_amount.evidence_refs = ["old-rate"]
    _, result = run(snapshot)
    assert result.items[0].status == "calculated"
    assert all(i.status == "needs_review" and i.amount is None for i in result.items[1:])
    assert result.gross_total is None


def test_unmapped_extra_statement_payment_prevents_full_wage_match():
    from agent.schemas.public.inputs import StatementItem

    snapshot = standard_snapshot()
    snapshot.earnings.append(StatementItem(item_id="unmapped-bonus", amount="100000", evidence_refs=["slip"]))
    context, result = run(snapshot)
    mapped = compare_wages(context, result)
    assert mapped.status == "insufficient"
    assert mapped.lhs is None and mapped.rhs is None
    assert all(item.status == "match" for item in mapped.wage_items)


def test_only_actual_rate_dependency_marks_statement_based_comparison():
    snapshot = standard_snapshot()
    snapshot.wage_conditions.ordinary_monthly_amount.evidence_refs = ["slip"]
    context, result = run(snapshot)
    mapped = compare_wages(context, result)
    # Base+weekly-rest group contains the weekly-rest rate dependency.
    assert all(not item.independent for item in mapped.wage_items)
    assert "slip" not in result.items[0].evidence_refs
    assert "slip" in result.items[1].evidence_refs


def test_offsetting_component_differences_do_not_become_an_overall_match():
    snapshot = standard_snapshot()
    snapshot.earnings[1].amount += Decimal("100")
    snapshot.earnings[2].amount -= Decimal("100")
    context, result = run(snapshot)
    comparison = compare_wages(context, result)
    assert comparison.delta == 0 and comparison.status == "difference"
    assert comparison.reason_codes == ["WAGE_COMPONENT_DIFFERENCE"]


@pytest.mark.parametrize("profile,total", [("H-STD", "2270000"), ("M-STD", "3272727")])
async def test_actual_analysis_graph_uses_standard_rules_without_ai(profile, total):
    from agent import run_task
    from agent.config import AgentSettings
    from agent.schemas.public.tasks import AgentTask, AnalysisJobInput
    from agent.tests.fake_ports import FakePorts
    from agent.tests.fixtures_support import context

    class NoModel:
        async def complete(self, *args, **kwargs):
            raise AssertionError("Wage calculation must not invoke AI")

    snapshot = standard_snapshot(profile)
    snapshot.snapshot_hash = snapshot_hash(snapshot)
    ctx = context("analyze_case").model_copy(update={"owner_id": "owner", "case_id": "case"})
    task = AgentTask(
        context=ctx,
        payload=AnalysisJobInput(
            snapshot=snapshot,
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_hash,
            rule_bundle_version="standard-wages-v1",
            support_manifest_version="standard-wages-manifest-v1",
        ),
    )
    ports = FakePorts()
    ports.bind(ctx)
    ports.source_refs = {
        source.source_id: source.model_copy(deep=True) for source in snapshot.authorized_sources
    }
    output = await run_task(task, ports=ports, settings=AgentSettings(), provider=NoModel())
    assert output.status == "completed", output.error
    assert output.payload.wage_section.gross_total == Decimal(total)
    assert output.payload.comparisons[6].status == "match"
