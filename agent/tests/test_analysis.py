from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent import run_task
from agent.graphs.analysis_graph import build_analysis_graph
from agent.rules.comparisons import payments, statements, terms
from agent.rules.registry import RuleRegistry, VerifiedRule
from agent.rules.support_manifest import support_manifest
from agent.runtime import Runtime
from agent.schemas.public.inputs import WorkRecord
from agent.services.calculation_context import build_calculation_context
from agent.services.hashing import result_hash, snapshot_hash
from agent.tests.fixture_provider import FixtureProvider


def rehash(task):
    task.payload.snapshot.snapshot_hash = snapshot_hash(task.payload.snapshot)
    task.payload.snapshot_hash = task.payload.snapshot.snapshot_hash


async def test_analysis_graph_eight_nodes_and_single_join(setup_agent):
    task, ports, settings = setup_agent
    runtime = Runtime(task, ports, settings, FixtureProvider())
    graph = build_analysis_graph(runtime, RuleRegistry())
    assert len(set(graph.get_graph().nodes) - {"__start__", "__end__"}) == 8
    outcome = await run_task(task, ports=ports, settings=settings)
    assert outcome.status == "completed", outcome.error
    assert outcome.provider_mode == "openai"
    assert [r.status for r in outcome.payload.comparisons[:6]] == ["match"] * 6
    assert all(r.status == "needs_review" for r in outcome.payload.wage_section.items)
    assert outcome.payload.wage_section.gross_total is None
    assert outcome.payload.comparisons[6].status == "insufficient"
    assert not outcome.payload.wage_section.scope_complete
    assert len(ports.checkpoints[task.context.job_id].completed_nodes) == 8
    assert len([event for event in ports.events if event["node_id"] == "aggregate"]) == 1
    assert result_hash(outcome.payload) == outcome.payload.result_hash


@pytest.mark.parametrize(
    "key,value,rid,delta",
    [
        ("slip_rate", "10500", "R-01", "500"),
        ("slip_minutes", "12100", "R-02", "100"),
        ("gross", "1999999", "R-03", "1"),
        ("deduction", "199999", "R-04", "1"),
        ("net", "1799999", "R-05", "1"),
        ("net", "1799999", "R-06", "-1"),
    ],
)
async def test_difference_signs(setup_agent, key, value, rid, delta):
    task, ports, settings = setup_agent
    next(f for f in task.payload.snapshot.confirmed_fields if f.field_id == key).value = Decimal(value)
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "completed", out.error
    result = next(r for r in out.payload.comparisons if r.rule_id == rid)
    assert result.delta == Decimal(delta)


async def test_missing_does_not_block_independent_checks(setup_agent):
    task, ports, settings = setup_agent
    task.payload.snapshot.confirmed_fields = [
        f for f in task.payload.snapshot.confirmed_fields if f.field_id != "contract_rate"
    ]
    task.payload.snapshot.earnings_complete = False
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "completed"
    assert out.payload.comparisons[0].status == "insufficient"
    assert out.payload.comparisons[2].status == "insufficient"
    assert out.payload.comparisons[4].status == "match"
    assert out.payload.comparisons[5].status == "match"


async def test_no_deposits_is_unknown_not_zero(setup_agent):
    task, ports, settings = setup_agent
    task.payload.snapshot.transaction_allocations = []
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[5].rhs is None
    assert out.payload.comparisons[5].delta is None


async def test_cash_and_duplicate_excluded(setup_agent):
    task, ports, settings = setup_agent
    allocation = task.payload.snapshot.transaction_allocations[0]
    cash = allocation.model_copy(update={"transaction_id": "cash", "kind": "cash_memo"})
    duplicate = allocation.model_copy(update={"transaction_id": "duplicate", "duplicate": True})
    task.payload.snapshot.transaction_allocations.extend([cash, duplicate])
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[5].rhs == Decimal("1800000")


@pytest.mark.parametrize("change", ["unit", "time_kind", "period"])
async def test_semantic_mismatch_holds_comparison(setup_agent, change):
    task, ports, settings = setup_agent
    field = next(f for f in task.payload.snapshot.confirmed_fields if f.field_id == "slip_minutes")
    if change == "unit":
        field.unit = "hour"
    elif change == "time_kind":
        field.time_kind = "paid"
    else:
        field.period = None
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[1].status == "needs_review"
    assert out.payload.comparisons[1].delta is None


async def test_hash_and_evidence_tampering_rejected(setup_agent):
    task, ports, settings = setup_agent
    task.payload.snapshot.input_version = 2
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"
    rehash(task)
    task.payload.snapshot.confirmed_fields[0].evidence_refs = ["invented"]
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"


def test_snapshot_hash_is_set_order_independent(setup_agent):
    task, _, _ = setup_agent
    snapshot = task.payload.snapshot
    previous = snapshot_hash(snapshot)
    snapshot.confirmed_fields.reverse()
    assert snapshot_hash(snapshot) == previous


def test_cross_midnight_breaks_and_missing_context(setup_agent):
    task, _, _ = setup_agent
    s = task.payload.snapshot
    s.work_records = [
        WorkRecord(
            record_id="night",
            start_at="2026-10-01T22:00:00+09:00",
            end_at="2026-10-02T02:00:00+09:00",
            evidence_refs=["evidence-demo"],
            breaks=[("2026-10-01T23:30:00+09:00", "2026-10-02T00:30:00+09:00")],
        )
    ]
    rehash(task)
    ctx = build_calculation_context(s, s.owner_id, s.case_id)
    assert len(ctx.normalized_intervals) == 4
    assert sum(i["minutes"] for i in ctx.normalized_intervals if not i["on_break"]) == 180


def test_numeric_json_rejected_and_unknown_not_zero(setup_agent):
    task, _, _ = setup_agent
    f = task.payload.snapshot.confirmed_fields[0]
    with pytest.raises(ValidationError):
        type(f).model_validate(f.model_dump() | {"value": 10000})
    with pytest.raises(ValidationError):
        type(f).model_validate(f.model_dump() | {"state": "unknown", "value": "0"})


def test_unverified_rule_cannot_activate():
    from datetime import date

    with pytest.raises(ValueError):
        RuleRegistry(
            [
                VerifiedRule(
                    rule_id="W-01",
                    version="test",
                    profile="H-STD",
                    effective_from=date(2026, 1, 1),
                    effective_to=date(2026, 12, 31),
                    required_fields=(),
                    official_sources=(),
                    verification_cases=(),
                    verification_status="pending",
                    formula_id="unverified",
                    calculate=lambda _: Decimal(10),
                )
            ]
        )
    assert not support_manifest(RuleRegistry())["release_ready"]


async def test_verified_rule_injection_is_explicit_not_default(setup_agent):
    from datetime import date

    task, ports, settings = setup_agent
    registry = RuleRegistry(
        [
            VerifiedRule(
                rule_id="W-01",
                version="TEST-ONLY",
                profile="H-STD",
                effective_from=date(2026, 1, 1),
                effective_to=date(2026, 12, 31),
                required_fields=("contract_rate",),
                official_sources=("test-fixture-not-legal-authority",),
                verification_cases=("independent-fixture",),
                verification_status="passed",
                formula_id="TEST-ONLY",
                calculate=lambda _: Decimal("12.5"),
            )
        ]
    )
    out = await run_task(task, ports=ports, settings=settings, registry=registry)
    assert out.payload.wage_section.items[0].amount == Decimal("13")
    assert out.payload.wage_section.gross_total is None


def detailed_context(task, records, minutes="60", time_kind="actual"):
    snapshot = task.payload.snapshot
    snapshot.work_records = records
    for field in snapshot.confirmed_fields:
        if field.field_id in ("worked_minutes", "slip_minutes"):
            field.value = Decimal(minutes)
            field.time_kind = time_kind
    rehash(task)
    return build_calculation_context(snapshot, snapshot.owner_id, snapshot.case_id)


def work_record(record_id, start, end, **kwargs):
    return WorkRecord(
        record_id=record_id,
        start_at=start,
        end_at=end,
        evidence_refs=kwargs.pop("evidence_refs", ["evidence-demo"]),
        **kwargs,
    )


async def test_detailed_work_summary_conflict_holds_only_time_comparison(setup_agent):
    task, ports, settings = setup_agent
    detailed_context(
        task,
        [work_record("one-hour", "2026-10-01T09:00:00+09:00", "2026-10-01T10:00:00+09:00")],
        minutes="12000",
    )
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "completed", out.error
    result = out.payload.comparisons[1]
    assert result.status == "needs_review"
    assert result.reason_codes == ["WORK_SUMMARY_MISMATCH"]
    assert result.delta is None and not result.scope_complete
    assert all(out.payload.comparisons[index].status == "match" for index in (0, 2, 3, 4, 5))
    question = next(item for item in out.payload.questions if item.reason_code == "WORK_SUMMARY_MISMATCH")
    assert question.blocking_rule_ids == ["R-02"]
    assert "상세 실근로시간 합계" in question.reason


def test_detail_totals_clip_payroll_boundaries_and_cross_midnight_breaks(setup_agent):
    task, _, _ = setup_agent
    ctx = detailed_context(
        task,
        [
            work_record(
                "before",
                "2026-09-30T23:00:00+09:00",
                "2026-10-01T02:00:00+09:00",
                breaks=[("2026-09-30T23:30:00+09:00", "2026-10-01T00:30:00+09:00")],
            ),
            work_record(
                "after",
                "2026-10-31T23:00:00+09:00",
                "2026-11-01T02:00:00+09:00",
                breaks=[("2026-10-31T23:30:00+09:00", "2026-11-01T00:30:00+09:00")],
            ),
        ],
        minutes="120",
    )
    assert sum(i["minutes"] for i in ctx.normalized_intervals if i["payroll"] and not i["on_break"]) == 120
    assert terms(ctx)[1].status == "match"


@pytest.mark.parametrize("context_inside_payroll", [False, True])
def test_context_only_work_does_not_change_payroll_summary_or_overlap_status(
    setup_agent, context_inside_payroll
):
    task, _, _ = setup_agent
    snapshot = task.payload.snapshot
    source = snapshot.authorized_sources[0].model_copy(update={"source_id": "context", "usage": "context"})
    evidence = snapshot.evidence_catalog[0].model_copy(
        update={"evidence_id": "context-evidence", "source_id": source.source_id}
    )
    snapshot.authorized_sources.append(source)
    snapshot.evidence_catalog.append(evidence)
    day = "2026-10-01" if context_inside_payroll else "2026-09-30"
    records = [work_record("payroll", "2026-10-01T09:00:00+09:00", "2026-10-01T10:00:00+09:00")]
    records.extend(
        work_record(
            f"context-{index}",
            f"{day}T09:00:00+09:00",
            f"{day}T10:00:00+09:00",
            evidence_refs=[evidence.evidence_id],
            break_location_known=False,
        )
        for index in range(2)
    )
    ctx = detailed_context(task, records)
    assert "overlapping_work_sources" in ctx.issues
    assert "overlapping_payroll_work_sources" not in ctx.issues
    assert terms(ctx)[1].status == "match"


@pytest.mark.parametrize("problem", ["unknown_break", "nested_breaks", "overlap"])
def test_ambiguous_payroll_details_cannot_produce_a_time_match(setup_agent, problem):
    task, _, _ = setup_agent
    records = [
        work_record(
            "details",
            "2026-09-30T23:00:00+09:00",
            "2026-10-01T02:00:00+09:00",
            break_location_known=problem != "unknown_break",
            breaks=[
                ("2026-09-30T23:00:00+09:00", "2026-10-01T01:30:00+09:00"),
                ("2026-09-30T23:30:00+09:00", "2026-09-30T23:45:00+09:00"),
                ("2026-10-01T00:00:00+09:00", "2026-10-01T01:00:00+09:00"),
            ]
            if problem == "nested_breaks"
            else [],
        )
    ]
    if problem == "overlap":
        records.append(work_record("duplicate", "2026-10-01T00:00:00+09:00", "2026-10-01T01:00:00+09:00"))
    result = terms(detailed_context(task, records))[1]
    assert result.status == "needs_review"
    assert result.delta is None
    assert (
        "WORK_SOURCE_CONFLICT" if problem == "overlap" else "WORK_BREAK_UNCONFIRMED"
    ) in result.reason_codes
    assert "WORK_SUMMARY_MISMATCH" not in result.reason_codes


def test_actual_details_do_not_claim_to_validate_paid_time_summary(setup_agent):
    task, _, _ = setup_agent
    ctx = detailed_context(
        task,
        [work_record("actual", "2026-10-01T09:00:00+09:00", "2026-10-01T10:00:00+09:00")],
        minutes="12000",
        time_kind="paid",
    )
    assert terms(ctx)[1].status == "match"


def test_complete_break_can_support_a_confirmed_zero_actual_summary(setup_agent):
    task, _, _ = setup_agent
    ctx = detailed_context(
        task,
        [
            work_record(
                "all-break",
                "2026-10-01T09:00:00+09:00",
                "2026-10-01T10:00:00+09:00",
                breaks=[("2026-10-01T09:00:00+09:00", "2026-10-01T10:00:00+09:00")],
            )
        ],
        minutes="0",
    )
    assert terms(ctx)[1].status == "match"
    assert terms(ctx)[1].delta == Decimal(0)


def test_comparisons_do_not_mutate_confirmed_evidence_or_snapshot_hash(setup_agent):
    task, _, _ = setup_agent
    snapshot = task.payload.snapshot
    source = snapshot.authorized_sources[0].model_copy(update={"source_id": "detail-source"})
    evidence = snapshot.evidence_catalog[0].model_copy(
        update={"evidence_id": "detail-evidence", "source_id": source.source_id}
    )
    snapshot.authorized_sources.append(source)
    snapshot.evidence_catalog.append(evidence)
    ctx = detailed_context(
        task,
        [
            work_record(
                "detail",
                "2026-10-01T09:00:00+09:00",
                "2026-10-01T10:00:00+09:00",
                evidence_refs=[evidence.evidence_id],
            )
        ],
    )
    original = ctx.snapshot.model_dump(mode="json")
    first = terms(ctx) + statements(ctx) + payments(ctx)
    second = terms(ctx) + statements(ctx) + payments(ctx)
    assert first == second
    assert ctx.snapshot.model_dump(mode="json") == original
    assert snapshot_hash(ctx.snapshot) == ctx.snapshot_hash
    assert evidence.evidence_id in first[1].evidence_refs
