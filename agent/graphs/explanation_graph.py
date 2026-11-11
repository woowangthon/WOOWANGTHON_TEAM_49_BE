import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.providers.base import ExplanationResponse
from agent.schemas.public.errors import BudgetExceeded, IntegrityError, ModelFormatError, ModelUnavailable
from agent.schemas.public.results import ExplanationCandidate
from agent.services.facts import allowed_facts, allowed_templates, template_explanation


def guard_response(response, facts, question_ids):
    try:
        parsed = ExplanationResponse.model_validate(response)
    except Exception as exc:
        raise ModelFormatError("Explanation schema invalid") from exc
    if set(parsed.question_ids) != set(question_ids):
        raise ModelFormatError("Question IDs changed")
    if not parsed.templates:
        raise ModelFormatError("Empty explanation")
    if any(text not in allowed_templates(facts) for text in parsed.templates):
        raise ModelFormatError("Explanation must use an approved sentence structure")
    for text in parsed.templates:
        refs = re.findall(r"\{\{([a-zA-Z0-9_]+)\}\}", text)
        if not refs or any(ref not in facts for ref in refs):
            raise ModelFormatError("Unknown fact reference")
        stripped = re.sub(r"\{\{[a-zA-Z0-9_]+\}\}", "", text)
        if re.search(r"\d|[{}]", stripped) or any(
            word in stripped
            for word in ("불법", "위법", "체불", "책임", "반드시 지급", "미지급 확정", "때문", "원인은")
        ):
            raise ModelFormatError("Unapproved numeric or legal statement")


class ExplanationState(TypedDict, total=False):
    composed: dict[str, Any]
    checked: bool
    result: dict[str, Any]


def build_explanation_graph(runtime):
    payload = runtime.task.payload
    facts = allowed_facts(payload.result)
    # The caller cannot smuggle arbitrary facts into an explanation.
    if payload.allowed_facts != facts:
        raise IntegrityError("Allowed facts differ from confirmed result")
    qids = [q.question_id for q in payload.result.questions]

    async def compose(state):
        async def generate():
            if payload.generation_mode == "template":
                return {
                    "response": {"templates": allowed_templates(facts)[1:], "question_ids": qids},
                    "status": "template_fallback",
                }
            try:
                response = await runtime.call_model(
                    "compose",
                    "explain",
                    {"facts": facts, "question_ids": qids, "allowed_templates": allowed_templates(facts)},
                    validator=lambda r: guard_response(r, facts, qids),
                )
                return {"response": response, "status": "generated"}
            except ModelUnavailable, ModelFormatError, BudgetExceeded, TimeoutError:
                return {
                    "response": {"templates": ["{{summary}}"], "question_ids": qids},
                    "status": "template_fallback",
                }

        return {"composed": await runtime.step("compose", generate)}

    async def guard(state):
        def validate():
            guard_response(state["composed"]["response"], facts, qids)
            return True

        return {"checked": await runtime.step("guard", validate)}

    async def publish(state):
        def finish():
            if not state["checked"]:
                raise IntegrityError("Unvalidated explanation")
            if payload.generation_mode == "template":
                return template_explanation(payload.result).model_dump(mode="json")
            response = state["composed"]["response"]
            paragraphs = [
                re.sub(r"\{\{([a-zA-Z0-9_]+)\}\}", lambda m: facts[m.group(1)], text)
                for text in response["templates"]
            ]
            return ExplanationCandidate(
                result_id=payload.result.result_id,
                result_version=payload.result.result_version,
                result_hash=payload.result.result_hash,
                status=state["composed"]["status"],
                paragraphs=paragraphs,
                question_ids=qids,
                model_config_version=payload.model_config_version,
                prompt_version=payload.prompt_version,
            ).model_dump(mode="json")

        return {"result": await runtime.step("publish", finish)}

    graph = StateGraph(ExplanationState)
    for name, fn in [("compose", compose), ("guard", guard), ("publish", publish)]:
        graph.add_node(name, fn)
    graph.add_edge(START, "compose")
    graph.add_edge("compose", "guard")
    graph.add_edge("guard", "publish")
    graph.add_edge("publish", END)
    return graph.compile()
