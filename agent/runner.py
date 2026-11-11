import asyncio

from pydantic import ValidationError

from agent.config import SUPPORTED_SCHEMA_VERSIONS
from agent.providers.model_provider import OpenAIProvider
from agent.rules.registry import RuleRegistry
from agent.runtime import Runtime
from agent.schemas.public.errors import (
    AccessRevoked,
    AgentError,
    ExecutionRevoked,
    IntegrityError,
    JobCancelled,
    ModelFormatError,
    OCRFailed,
    OCRRequired,
    OCRUnavailable,
)
from agent.schemas.public.tasks import AgentFailure, AgentOutcome, AgentTask
from agent.services.hashing import result_hash


def failure_message(exc):
    """Explain validated failure categories without exposing model or source text."""
    if isinstance(exc, OCRRequired):
        return "OCR 추출 페이지에서 먼저 원문을 추출한 뒤 AI 처리를 실행해 주세요."
    if isinstance(exc, (OCRUnavailable, OCRFailed)):
        return str(exc)
    if not isinstance(exc, ModelFormatError):
        return "처리에 실패했습니다. 코드와 작업 ID로 확인해 주세요."
    messages = {
        "DOCUMENT_TYPE_MISMATCH": (
            "선택한 문서 유형과 원문이 다릅니다. 문서 유형을 변경한 뒤 다시 추출해 주세요."
        ),
        "DOCUMENT_TYPE_UNRESOLVED": (
            "문서 유형을 확정하지 못했습니다. 서로 다른 자료가 섞여 있으면 파일을 나누거나 유형을 직접 선택해 주세요."
        ),
        "SOURCE_NORMALIZATION_INVALID": (
            "AI가 변환한 수치·단위를 OCR 원문과 일치시키지 못했습니다. "
            "다시 추출하거나 원문을 보며 해당 값을 직접 입력해 주세요."
        ),
        "SOURCE_VALUE_MISMATCH": (
            "AI가 추출한 값의 표기를 OCR 원문에서 확인하지 못했습니다. "
            "다시 추출하거나 원문을 보며 해당 값을 직접 입력해 주세요."
        ),
        "SOURCE_QUOTE_MISMATCH": (
            "AI가 제시한 근거 문장이 OCR 원문과 맞지 않습니다. "
            "다시 추출하거나 원문을 보며 해당 값을 직접 입력해 주세요."
        ),
        "TABLE_SOURCE_MISMATCH": (
            "AI가 분류한 표의 제목·행을 OCR 원문에서 확인하지 못했습니다. "
            "표가 선명한 PDF로 다시 추출하거나 원문을 보며 행을 직접 입력해 주세요."
        ),
        "TABLE_CELL_SOURCE_MISMATCH": (
            "AI가 분류한 표의 값이 해당 OCR 행·열과 맞지 않습니다. "
            "다시 추출하거나 원문을 보며 해당 행의 값을 직접 입력해 주세요."
        ),
        "TABLE_ROW_SOURCE_REUSED": (
            "AI가 같은 OCR 행을 여러 항목으로 중복 분류했습니다. "
            "다시 추출하거나 원문을 보며 중복 행을 제외해 주세요."
        ),
        "MODEL_FORMAT_INVALID": (
            "AI 추출 결과의 형식 또는 원문 근거를 검증하지 못했습니다. "
            "다시 추출하거나 원문을 보며 값을 직접 입력해 주세요."
        ),
    }
    reason = exc.reason if exc.reason in messages else "MODEL_FORMAT_INVALID"
    detail = reason
    if exc.field_id:
        from agent.services.extraction_profiles import DOCUMENT_TYPES, field_definition

        definition = next(
            (item for kind in DOCUMENT_TYPES if (item := field_definition(kind, exc.field_id))), None
        )
        if definition:
            detail += f" · 항목: {definition['label']} [{exc.field_id}]"
    return f"{messages[reason]} (사유: {detail})"


async def run_task(task, *, ports, settings, provider=None, registry=None):
    if not isinstance(task, AgentTask):
        # Malformed envelopes have no trustworthy job identifier; reject before constructing an outcome.
        task = AgentTask.model_validate(task)
    context = task.context
    runtime = None
    try:
        if context.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise IntegrityError("Unsupported public schema version")
        if settings.provider_mode != "openai":
            raise IntegrityError("Unknown provider mode")
        if provider is None:
            provider = OpenAIProvider(settings)
        payload = task.payload
        if context.task_kind == "extract_document":
            sources = [payload.source]
        elif context.task_kind == "analyze_case":
            sources = payload.snapshot.authorized_sources
        else:
            if payload.result.owner_id != context.owner_id or payload.result.case_id != context.case_id:
                raise IntegrityError("Confirmed result ownership mismatch")
            if result_hash(payload.result) != payload.result.result_hash:
                raise IntegrityError("Confirmed result hash mismatch")
            sources = payload.selected_sources if context.task_kind == "build_report" else []
        if any(s.owner_id != context.owner_id for s in sources):
            raise IntegrityError("Source owner mismatch")
        if context.task_kind in ("extract_document", "explain_result"):
            if payload.model_config_version != settings.model_config_version:
                raise IntegrityError("Model configuration version mismatch")
            expected_prompt = "extract-v1" if context.task_kind == "extract_document" else "explain-v1"
            if payload.prompt_version != expected_prompt:
                raise IntegrityError("Unsupported prompt version")
            if context.task_kind == "extract_document" and payload.extraction_version not in {
                "1.0",
                "1.1",
                "1.2",
            }:
                raise IntegrityError("Unsupported extraction schema version")
        runtime = Runtime(task, ports, settings, provider)
        await runtime.start(sources)
        if context.task_kind == "analyze_case":
            from agent.graphs.analysis_graph import build_analysis_graph
            from agent.rules.wages import default_rule_registry

            selected_registry = registry
            if selected_registry is None:
                selected_registry = (
                    RuleRegistry() if payload.rule_bundle_version == "pending-v1" else default_rule_registry()
                )
            result = await build_analysis_graph(runtime, selected_registry).ainvoke({})
            output = result["result"]
        elif context.task_kind == "extract_document":
            from agent.graphs.extraction_graph import build_extraction_graph
            from agent.graphs.supplement_graph import build_supplement_graph

            builder = (
                build_supplement_graph
                if payload.extraction_method == "pdf_supplement"
                else build_extraction_graph
            )
            output = (await builder(runtime).ainvoke({}))["result"]
        elif context.task_kind == "explain_result":
            from agent.graphs.explanation_graph import build_explanation_graph

            output = (await build_explanation_graph(runtime).ainvoke({}))["result"]
        else:
            from agent.services.reports import build_report

            output = await runtime.step("build_report", lambda: build_report(runtime))
        await runtime.control()
        return AgentOutcome(
            schema_version=context.schema_version,
            provider_mode=settings.provider_mode,
            job_id=context.job_id,
            run_id=context.run_id,
            status="completed",
            payload=output,
            usage=runtime.usage,
        )
    except (JobCancelled, AccessRevoked, ExecutionRevoked) as exc:
        return AgentOutcome(
            schema_version=context.schema_version,
            provider_mode=settings.provider_mode,
            job_id=context.job_id,
            run_id=context.run_id,
            status="cancelled",
            error=AgentFailure(code=exc.code, message="작업이 취소되었거나 접근·실행 권한이 철회되었습니다."),
            usage=runtime.usage if runtime else {},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, AgentError):
            code, retryable = exc.code, exc.retryable
        elif isinstance(exc, ValidationError):
            code, retryable = "RESULT_SCHEMA_INVALID", False
        elif isinstance(exc, TimeoutError):
            code, retryable = "MODEL_TIMEOUT", True
        else:
            code, retryable = "INTERNAL_ERROR", False
        return AgentOutcome(
            schema_version=context.schema_version,
            provider_mode=settings.provider_mode,
            job_id=context.job_id,
            run_id=context.run_id,
            status="failed",
            error=AgentFailure(
                code=code,
                message=failure_message(exc),
                retryable=retryable,
            ),
            usage=runtime.usage if runtime else {},
        )
