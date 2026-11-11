from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent import run_task
from agent.rules.comparisons import payments, statements, terms
from agent.schemas.public.inputs import ConditionState, Period
from agent.services.hashing import digest
from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import confirmed_result, explanation_task
from agent.tests.test_analysis import detailed_context, rehash, work_record


async def test_contract_can_cover_multiple_payroll_periods(setup_agent):
    task, ports, settings = setup_agent
    next(f for f in task.payload.snapshot.confirmed_fields if f.field_id == "contract_rate").period = Period(
        start="2026-01-01", end="2026-12-31"
    )
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[0].status == "match"


async def test_condition_conflict_cannot_be_ignored(setup_agent):
    task, ports, settings = setup_agent
    task.payload.snapshot.condition_states = [
        ConditionState(field_id="contract_rate", state="conflict", conflicting_source_refs=["source-demo"])
    ]
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[0].status == "needs_review"
    assert out.payload.comparisons[4].status == "match"


async def test_unknown_without_a_numeric_value_is_partial_input(setup_agent):
    task, ports, settings = setup_agent
    field = next(f for f in task.payload.snapshot.confirmed_fields if f.field_id == "contract_rate")
    replacement = field.model_dump() | {"state": "unknown", "value": None, "evidence_refs": []}
    task.payload.snapshot.confirmed_fields = [
        type(field).model_validate(replacement) if f.field_id == field.field_id else f
        for f in task.payload.snapshot.confirmed_fields
    ]
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "completed"
    assert out.payload.comparisons[0].status == "insufficient"
    assert out.payload.comparisons[5].status == "match"


async def test_confirmed_zero_is_compared(setup_agent):
    task, ports, settings = setup_agent
    for field in task.payload.snapshot.confirmed_fields:
        if field.field_id in ("worked_minutes", "slip_minutes"):
            field.value = Decimal(0)
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[1].delta == Decimal(0)


async def test_duplicate_and_overallocated_transactions_are_review(setup_agent):
    task, ports, settings = setup_agent
    allocation = task.payload.snapshot.transaction_allocations[0]
    task.payload.snapshot.transaction_allocations.append(allocation.model_copy(deep=True))
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[5].status == "needs_review"
    assert out.payload.comparisons[5].delta is None


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "not-a-number"])
def test_invalid_decimal_strings_rejected(setup_agent, invalid):
    task, _, _ = setup_agent
    field = task.payload.snapshot.confirmed_fields[0]
    with pytest.raises(ValidationError):
        type(field).model_validate(field.model_dump() | {"value": invalid})


def test_ordered_prose_hash_preserves_order():
    assert digest({"paragraphs": ["a", "b"]}) != digest({"paragraphs": ["b", "a"]})


async def test_usage_survives_resume(setup_agent):
    task, ports, settings = setup_agent
    analysis = await run_task(task, ports=ports, settings=settings)
    task = explanation_task(confirmed_result(analysis.payload))
    ports.bind(task.context)

    class UsageProvider(FixtureProvider):
        async def invoke(self, operation, data):
            out, _ = await super().invoke(operation, data)
            return out, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    first = await run_task(task, ports=ports, settings=settings, provider=UsageProvider())
    second = await run_task(task, ports=ports, settings=settings, provider=UsageProvider())
    assert first.usage == second.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


async def test_unsupported_schema_and_prompt_do_not_call_provider(setup_agent):
    task, ports, settings = setup_agent
    task.context.schema_version = "unsupported"
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"
    assert not ports.checkpoints


async def test_missing_font_fails_report_instead_of_broken_pdf(setup_agent):
    from pathlib import Path

    from agent.schemas.public.errors import IntegrityError
    from agent.services.reports import render_pdf

    task, ports, settings = setup_agent
    analysis = await run_task(task, ports=ports, settings=settings)
    result = confirmed_result(analysis.payload)
    explain = explanation_task(result)
    ports.bind(explain.context)
    out = await run_task(explain, ports=ports, settings=settings)
    settings.font_path = Path("missing-font.ttf")
    with pytest.raises(IntegrityError):
        render_pdf(result, out.payload, settings, task.context.requested_at)


async def test_checkpoint_version_mismatch_never_reuses_state(setup_agent):
    task, ports, settings = setup_agent
    await run_task(task, ports=ports, settings=settings)
    ports.checkpoints[task.context.job_id].checkpoint_schema_version = "different"
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"


def attach_period_source(snapshot, start, end, suffix="period"):
    source = snapshot.authorized_sources[0].model_copy(
        update={"source_id": suffix, "applicable_period": Period(start=start, end=end)}
    )
    evidence = snapshot.evidence_catalog[0].model_copy(
        update={"evidence_id": suffix + "-evidence", "source_id": source.source_id}
    )
    snapshot.authorized_sources.append(source)
    snapshot.evidence_catalog.append(evidence)
    return evidence.evidence_id


async def test_source_period_cannot_be_relabelled_as_current_payroll_period(setup_agent):
    task, ports, settings = setup_agent
    snapshot = task.payload.snapshot
    ref = attach_period_source(snapshot, "2026-09-01", "2026-09-30")
    next(field for field in snapshot.confirmed_fields if field.field_id == "contract_rate").evidence_refs = [
        ref
    ]
    ports.add_source(snapshot.authorized_sources[-1], ports.sources["source-demo"])
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "completed", out.error
    result = out.payload.comparisons[0]
    assert result.status == "needs_review"
    assert result.reason_codes == ["SOURCE_PERIOD_MISMATCH"]
    assert result.delta is None
    assert all(item.status == "match" for item in out.payload.comparisons[1:6])


@pytest.mark.parametrize("target", ["earnings", "deductions", "transaction_allocations"])
def test_source_link_period_is_checked_for_statement_and_payment_details(setup_agent, target):
    task, _, _ = setup_agent
    snapshot = task.payload.snapshot
    ref = attach_period_source(snapshot, "2026-09-01", "2026-09-30")
    getattr(snapshot, target)[0].evidence_refs.append(ref)
    rehash(task)
    from agent.services.calculation_context import build_calculation_context

    ctx = build_calculation_context(snapshot, snapshot.owner_id, snapshot.case_id)
    results = statements(ctx) + payments(ctx)
    index = {"earnings": 0, "deductions": 1, "transaction_allocations": 3}[target]
    assert results[index].status == "needs_review"
    assert results[index].reason_codes == ["SOURCE_PERIOD_MISMATCH"]
    assert results[index].delta is None
    assert all(item.status == "match" for i, item in enumerate(results) if i != index)


@pytest.mark.parametrize(
    "periods,expected",
    [
        ([("2026-01-01", "2026-12-31")], "match"),
        ([("2026-10-01", "2026-10-15"), ("2026-10-16", "2026-10-31")], "match"),
        ([("2026-10-01", "2026-10-15"), ("2026-10-17", "2026-10-31")], "needs_review"),
        ([("2026-10-01", "2026-10-15")], "needs_review"),
        ([("2026-10-01", "2026-10-31"), ("2026-09-01", "2026-09-30")], "needs_review"),
    ],
)
def test_multiple_declared_source_periods_need_complete_coverage_even_with_manual_evidence(
    setup_agent, periods, expected
):
    task, _, _ = setup_agent
    snapshot = task.payload.snapshot
    field = next(field for field in snapshot.confirmed_fields if field.field_id == "worked_minutes")
    for index, (start, end) in enumerate(periods):
        field.evidence_refs.append(attach_period_source(snapshot, start, end, suffix=f"period-{index}"))
    rehash(task)
    from agent.services.calculation_context import build_calculation_context

    ctx = build_calculation_context(snapshot, snapshot.owner_id, snapshot.case_id)
    result = terms(ctx)[1]
    assert result.status == expected
    assert ("SOURCE_PERIOD_MISMATCH" in result.reason_codes) == (expected == "needs_review")


def test_contract_source_can_apply_to_current_payroll_with_longer_contract_field_period(setup_agent):
    task, _, _ = setup_agent
    snapshot = task.payload.snapshot
    ref = attach_period_source(snapshot, "2026-10-01", "2026-10-31")
    field = next(field for field in snapshot.confirmed_fields if field.field_id == "contract_rate")
    field.period = Period(start="2026-01-01", end="2026-12-31")
    field.evidence_refs.append(ref)
    ctx = detailed_context(task, [])
    assert terms(ctx)[0].status == "match"


def test_payroll_detail_source_period_mismatch_holds_time_comparison(setup_agent):
    task, _, _ = setup_agent
    snapshot = task.payload.snapshot
    ref = attach_period_source(snapshot, "2026-09-01", "2026-09-30")
    ctx = detailed_context(
        task,
        [
            work_record(
                "wrong-period",
                "2026-10-01T09:00:00+09:00",
                "2026-10-01T10:00:00+09:00",
                evidence_refs=[ref],
            )
        ],
    )
    result = terms(ctx)[1]
    assert result.status == "needs_review"
    assert result.reason_codes == ["SOURCE_PERIOD_MISMATCH"]
    assert result.delta is None
    assert terms(ctx)[0].status == "match"
