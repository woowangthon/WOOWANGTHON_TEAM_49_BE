from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.config import ENGINE_VERSION
from agent.rules import comparisons
from agent.rules.registry import WAGE_RULE_IDS
from agent.rules.wages import compare_wages, wage_section
from agent.schemas.internal.calculation_context import CalculationContext
from agent.schemas.public.errors import IntegrityError
from agent.schemas.public.results import AnalysisResultCandidate, WageSection
from agent.services.calculation_context import build_calculation_context
from agent.services.hashing import digest, result_hash
from agent.services.questions import build_questions


class AnalysisState(TypedDict, total=False):
    context: dict[str, Any]
    wage: dict[str, Any]
    terms: list[dict[str, Any]]
    statement: list[dict[str, Any]]
    payment: list[dict[str, Any]]
    candidate: dict[str, Any]
    validated: bool
    result: dict[str, Any]


def build_analysis_graph(runtime, registry):
    payload = runtime.task.payload

    async def supervisor(state):
        def prepare():
            if (
                payload.snapshot_id != payload.snapshot.snapshot_id
                or payload.snapshot_hash != payload.snapshot.snapshot_hash
                or payload.engine_version != ENGINE_VERSION
                or payload.rule_bundle_version != registry.bundle_version
                or payload.support_manifest_version != registry.manifest_version
                or payload.snapshot.rule_bundle_version != registry.bundle_version
                or payload.snapshot.support_manifest_version != registry.manifest_version
            ):
                raise IntegrityError("Analysis version mismatch")
            return build_calculation_context(
                payload.snapshot, runtime.context.owner_id, runtime.context.case_id
            ).model_dump(mode="json")

        return {"context": await runtime.step("supervisor", prepare)}

    async def worker_wage(state):
        def compute():
            ctx = CalculationContext.model_validate(state["context"])
            items = [registry.evaluate(rid, ctx) for rid in WAGE_RULE_IDS]
            return wage_section(items, ctx.snapshot).model_dump(mode="json")

        return {"wage": await runtime.step("worker_wage", compute)}

    def comparison_worker(name, key, fn):
        async def execute(state):
            return {
                key: await runtime.step(
                    name,
                    lambda: [
                        x.model_dump(mode="json")
                        for x in fn(CalculationContext.model_validate(state["context"]))
                    ],
                )
            }

        return execute

    async def aggregate(state):
        def merge():
            ctx = CalculationContext.model_validate(state["context"])
            comps = state["terms"] + state["statement"] + state["payment"]
            comps.append(
                compare_wages(ctx, WageSection.model_validate(state["wage"])).model_dump(mode="json")
            )
            return AnalysisResultCandidate(
                owner_id=ctx.snapshot.owner_id,
                case_id=ctx.snapshot.case_id,
                workplace_id=ctx.snapshot.workplace_id,
                input_version=ctx.snapshot.input_version,
                snapshot_id=ctx.snapshot.snapshot_id,
                snapshot_hash=ctx.snapshot_hash,
                context_id=ctx.context_hash,
                context_hash=ctx.context_hash,
                rule_bundle_version=registry.bundle_version,
                support_manifest_version=registry.manifest_version,
                engine_version=ENGINE_VERSION,
                wage_section=state["wage"],
                comparisons=comps,
                questions=[],
                evidence_catalog=ctx.snapshot.evidence_catalog,
                dependencies=ctx.dependencies,
            ).model_dump(mode="json")

        return {"candidate": await runtime.step("aggregate", merge)}

    async def validator(state):
        def validate():
            ctx = CalculationContext.model_validate(state["context"])
            candidate = AnalysisResultCandidate.model_validate(state["candidate"])
            expected = comparisons.terms(ctx) + comparisons.statements(ctx) + comparisons.payments(ctx)
            if digest(candidate.comparisons[:6]) != digest(expected):
                raise IntegrityError("Comparison revalidation failed")
            expected_items = [registry.evaluate(rid, ctx) for rid in WAGE_RULE_IDS]
            if digest(candidate.wage_section.items) != digest(expected_items):
                raise IntegrityError("Wage revalidation failed")
            expected_wage = wage_section(expected_items, ctx.snapshot)
            if digest(candidate.wage_section) != digest(expected_wage):
                raise IntegrityError("Wage total revalidation failed")
            if len(candidate.comparisons) != 7 or digest(candidate.comparisons[6]) != digest(
                compare_wages(ctx, expected_wage)
            ):
                raise IntegrityError("Wage comparison revalidation failed")
            allowed = {e.evidence_id for e in ctx.snapshot.evidence_catalog}
            for item in [*candidate.comparisons, *candidate.wage_section.items]:
                if not set(item.evidence_refs) <= allowed:
                    raise IntegrityError("Result evidence outside snapshot")
            if candidate.context_hash != ctx.context_hash or candidate.snapshot_hash != ctx.snapshot_hash:
                raise IntegrityError("Context changed during analysis")
            return True

        return {"validated": await runtime.step("validator", validate)}

    async def finalize(state):
        def finish():
            if not state["validated"]:
                raise IntegrityError("Unvalidated result")
            result = AnalysisResultCandidate.model_validate(state["candidate"])
            result.questions = build_questions(
                result.wage_section.items, result.comparisons, snapshot=payload.snapshot
            )
            result.result_hash = result_hash(result)
            return result.model_dump(mode="json")

        return {"result": await runtime.step("finalize", finish)}

    graph = StateGraph(AnalysisState)
    graph.add_node("supervisor", supervisor)
    graph.add_node("worker_wage", worker_wage)
    graph.add_node("worker_terms", comparison_worker("worker_terms", "terms", comparisons.terms))
    graph.add_node(
        "worker_statement", comparison_worker("worker_statement", "statement", comparisons.statements)
    )
    graph.add_node("worker_payment", comparison_worker("worker_payment", "payment", comparisons.payments))
    graph.add_node("aggregate", aggregate)
    graph.add_node("validator", validator)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "supervisor")
    workers = ["worker_wage", "worker_terms", "worker_statement", "worker_payment"]
    for worker in workers:
        graph.add_edge("supervisor", worker)
    graph.add_edge(workers, "aggregate")
    graph.add_edge("aggregate", "validator")
    graph.add_edge("validator", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()
