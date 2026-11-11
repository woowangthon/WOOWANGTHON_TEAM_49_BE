"""Versioned, code-owned field and tool registries for the review workflow."""

import copy
import inspect
import time
from dataclasses import dataclass

from agent.review.telemetry import begin_span, timestamp
from agent.services.extraction_profiles import DOCUMENT_TYPES, PROFILES, field_definition

PIPELINE_VERSION = "review-pipeline-1"
ROLE_FIELDS = {
    "employer_representative": {"label": "고용주 대표자 성명", "unit": "text"},
    "employer_address": {"label": "업체 소재지", "unit": "text"},
    "workplace_address": {"label": "실제 근무 장소", "unit": "text"},
    "employer_phone": {"label": "업체 연락처", "unit": "text"},
    "contract_period_type": {"label": "계약기간 유형", "unit": "text"},
}
ISSUES = {
    "AMBIGUOUS_NUMBER": "금액의 구분 기호가 애매합니다. 근거를 확인한 추천값 또는 직접 입력한 값을 사용해 주세요.",
    "MISSING_OUTPUT": "추출 응답에서 누락된 항목입니다.",
    "NOT_FOUND": "읽은 범위에서 찾지 못했습니다.",
    "UNREADABLE": "원문을 판독하지 못했습니다.",
    "AMBIGUOUS_ROLE": "고용주·근로자 등 정보의 주체를 확인해 주세요.",
    "CONFLICT": "다른 판독 결과 또는 자료와 값이 다릅니다.",
    "SOURCE_QUOTE_MISMATCH": "인용문을 판독 원문에서 확인하지 못했습니다.",
    "SOURCE_VALUE_MISMATCH": "값과 인용문의 표기가 다릅니다.",
    "NORMALIZATION_INVALID": "값의 형식이나 단위를 확인해 주세요.",
    "LOCATION_UNAVAILABLE": "정확한 위치 확인 필요",
    "RECHECK_FAILED": "추가 판독에서도 확인하지 못했습니다.",
    "VISION_CANDIDATE": "원본을 다시 읽은 후보입니다. 사용자 확인이 필요합니다.",
    "MANUAL": "직접 입력한 값입니다.",
    "TABLE_CELL_SOURCE_MISMATCH": "표의 이 칸 값이 행 원문 표기와 다릅니다.",
    "TABLE_SOURCE_MISMATCH": "표의 행을 원문에서 찾지 못했습니다.",
    "TABLE_SHAPE_INVALID": "표의 열 개수가 맞지 않습니다.",
    "SOURCE_PAGE_INVALID": "근거 페이지가 이 자료 범위를 벗어났습니다.",
    "SOURCE_VALUE_MISSING": "제안값의 원문 표기를 찾지 못했습니다.",
    "CANDIDATE_RANGE_INVALID": "값이 가능한 범위를 벗어났습니다.",
    "DUPLICATE_FIELD_ID": "같은 항목이 중복 추출됐습니다.",
    "UNKNOWN_FIELD": "알 수 없는 항목입니다.",
}


def profiles():
    result = {key: copy.deepcopy(PROFILES[key]) for key in DOCUMENT_TYPES}
    result["contract"]["fields"].update(ROLE_FIELDS)
    return result


def definition(kind, key):
    return profiles().get(kind, {}).get("fields", {}).get(key) or field_definition(kind, key)


@dataclass(frozen=True)
class Tool:
    name: str
    roles: tuple[str, ...]
    handler: object
    version: str = "1"
    timeout: float = 180


class ToolExecutor:
    def __init__(self, tools, trace, *, timeline=None, persist=None):
        self.tools = {tool.name: tool for tool in tools}
        self.trace = trace
        self.timeline = timeline
        self.persist = persist or (lambda: None)

    async def call(self, role, name, *, flow_node=None, **arguments):
        import asyncio

        tool = self.tools[name]
        if role not in tool.roles:
            raise PermissionError("이 단계에서는 해당 도구를 호출할 수 없습니다.")
        started = time.monotonic()
        context = {
            key: arguments[key] for key in ("media_type", "page", "max_side", "region") if key in arguments
        }
        event = begin_span("tool", "verify" if role == "verifier" else role, name, context=context)
        event.update(tool=name, version=tool.version, role=role)
        if flow_node:
            event["flow_node"] = flow_node
        self.trace.append(event)
        if self.timeline is not None:
            self.timeline.append(event)
        self.persist()
        try:
            if inspect.iscoroutinefunction(tool.handler):
                result = await asyncio.wait_for(tool.handler(**arguments), tool.timeout)
            else:
                result = await asyncio.wait_for(asyncio.to_thread(tool.handler, **arguments), tool.timeout)
            event["status"] = "completed"
            return result
        except BaseException:
            event["status"] = "failed"
            raise
        finally:
            event["duration_ms"] = round((time.monotonic() - started) * 1000)
            event["finished_at"] = timestamp()
            self.persist()
