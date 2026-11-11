"""Bounded document graph. It produces candidates, never confirmations."""

import asyncio
import copy
import re
import time
import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent.graphs.extraction_graph import verify_response
from agent.providers.base import ModelField
from agent.review.flow import reading_route, visit
from agent.review.model import Classification
from agent.review.reading import compact, locate, png_bytes, read_document
from agent.review.registry import ROLE_FIELDS, Tool, ToolExecutor, definition, profiles
from agent.review.telemetry import add_usage, begin_span, timestamp, usage_numbers
from agent.services.extraction_profiles import DOCUMENT_TYPES
from agent.services.extraction_validation import candidate_range_valid
from agent.services.identity import ambiguous_money
from agent.services.hashing import content_hash, digest
from agent.services.normalization import normalize_evidenced_scalar, normalize_scalar


def identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex[:16]


def field_seed(key, spec, *, extra=False):
    return {
        "id": identifier("field"),
        "field_id": key,
        "label": spec["label"],
        "unit": spec["unit"],
        "value": "",
        "raw_value": None,
        "candidates": [],
        "issues": ["MISSING_OUTPUT"],
        "validation_status": "missing",
        "review_state": "unreviewed",
        "touched": False,
        "extra": extra,
        "evidence": None,
        "history": [],
        "revision": 1,
    }


def manual_value(value, unit, kind="contract", key=""):
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError("입력값은 4,000자 이하의 문자열이어야 합니다.")
    if not value.strip():
        return ""
    if unit == "rate":
        raise ValueError("임금 단위를 선택해 주세요.")
    normalized = normalize_scalar(value, unit)
    if normalized is None or not candidate_range_valid(
        key, unit, normalized, kind if unit != "text" else "extra"
    ):
        raise ValueError("값과 단위의 형식을 확인해 주세요.")
    return normalized


def build_fields(response, part, reading, *, visual=False, targets=None):
    kind = part["document_type"]
    allowed = range(part["start_page"], part["end_page"] + 1)
    raw = copy.deepcopy(response)
    extras = raw.pop("additional_fields", [])
    role_fields = [f for f in raw.get("fields", []) if f.get("field_id") in ROLE_FIELDS]
    raw["fields"] = [f for f in raw.get("fields", []) if f.get("field_id") not in ROLE_FIELDS]
    pages = [p["text"] if p["page"] in allowed else "" for p in reading["pages"]]
    checked = verify_response(raw, pages, kind, [visual] * len(pages))
    result = {}
    for item in checked.fields:
        spec = definition(kind, item.field_id)
        if not spec:
            continue
        reasons = list(dict.fromkeys(item.validation_reasons + item.issues))
        result[item.field_id] = _candidate(item.model_dump(), spec, reading, allowed, reasons, visual)
    for item in role_fields + extras[:30]:
        parsed = ModelField.model_validate({k: item[k] for k in ModelField.model_fields})
        spec = ROLE_FIELDS.get(parsed.field_id)
        is_extra = spec is None
        if is_extra:
            parsed.field_id = "extra_" + re.sub(r"[^a-z0-9_]", "_", parsed.field_id.lower())[:60]
            spec = {"label": str(item.get("label", "추가 정보"))[:120], "unit": "text"}
            parsed.unit = "text"
        entry = parsed.model_dump()
        entry["suggested_value"] = parsed.normalized_value
        reasons = list(parsed.issues)
        try:
            if parsed.raw_value:
                value = normalize_evidenced_scalar(
                    parsed.raw_value, parsed.unit or spec["unit"], parsed.quote, field_id=parsed.field_id
                )
                if value != parsed.normalized_value:
                    reasons.append("NORMALIZATION_INVALID")
            else:
                reasons.append("NOT_FOUND")
        except ValueError:
            reasons.append("NORMALIZATION_INVALID")
        result[parsed.field_id] = _candidate(entry, spec, reading, allowed, reasons, visual, is_extra)
    if targets is None:
        for key, spec in profiles()[kind]["fields"].items():
            result.setdefault(key, field_seed(key, spec))
    else:
        result = {key: value for key, value in result.items() if key in targets}
    return list(result.values())


def _candidate(item, spec, reading, allowed, reasons, visual, extra=False):
    key, raw, page = item["field_id"], item.get("raw_value"), item.get("page")
    field = field_seed(key, spec, extra=extra)
    quote = item.get("quote", "")
    value = item.get("normalized_value") or item.get("suggested_value")
    if page not in allowed:
        reasons.append("SOURCE_PAGE_INVALID")
    valid_page = page in allowed
    readable = next((p["text"] for p in reading["pages"] if p["page"] == page), "")
    if raw and not visual and (not quote or compact(quote) not in compact(readable)):
        reasons.append("SOURCE_QUOTE_MISMATCH")
    if raw and compact(raw) not in compact(quote):
        reasons.append("SOURCE_VALUE_MISMATCH")
    boxes = locate(reading, page, quote, raw) if valid_page else []
    evidence = {
        "id": identifier("evidence"),
        "page": page if valid_page else None,
        "quote": quote,
        "boxes": boxes,
        "location_status": "located" if boxes else "unavailable",
        "method": "vision_unverified"
        if visual
        else "text_match"
        if valid_page and reading["pages"][page - 1]["method"] == "pdf_text"
        else "ocr_unverified",
    }
    # The model's own row span (before header expansion) lets the UI pair row cells.
    row_quote = item.get("source_quote")
    if raw and isinstance(row_quote, str) and row_quote and row_quote != quote:
        evidence["row_quote"] = row_quote[:2000]
    if raw and not boxes:
        reasons.append("LOCATION_UNAVAILABLE")
    if visual:
        reasons.append("VISION_CANDIDATE")
    if not raw:
        reasons.append("NOT_FOUND")
    reasons = list(dict.fromkeys(reasons))
    if (item.get("unit") or spec["unit"]).startswith("KRW") and ambiguous_money(raw):
        reasons.append("AMBIGUOUS_NUMBER")
    substantive = [
        x
        for x in reasons
        if x not in ("LOCATION_UNAVAILABLE", "VISION_CANDIDATE")
        and not x.startswith("SOURCE_TEXT_")
        and x not in ("SOURCE_UNIT_FROM_HEADER", "SOURCE_SCALAR_SPELLING_RESTORED")
    ]
    status = "missing" if not raw else "needs_review" if substantive else "valid"
    candidate = {
        "id": identifier("candidate"),
        "value": value,
        "raw_value": raw,
        "unit": item.get("unit") or spec["unit"],
        "evidence": evidence,
        "issues": reasons,
        "validation_status": status,
    }
    field.update(
        raw_value=raw,
        unit=candidate["unit"],
        value=value if status == "valid" and value is not None else "",
        candidates=[candidate],
        evidence=evidence,
        issues=reasons,
        validation_status=status,
    )
    return field


class PipelineState(TypedDict, total=False):
    reading: dict
    parts: list


async def run_pipeline(
    data,
    media_type,
    settings,
    model,
    job,
    progress,
    *,
    preset_parts=None,
    prior_reading=None,
    verification_parts=None,
    verify_field_ids=None,
    persist=None,
    auto_verify=True,
    read_only=False,
    control=None,
):
    persist = persist or (lambda: None)
    reader_ends = []
    visit(job, "start")
    executor = ToolExecutor(
        [
            Tool(
                "document.read",
                ("reader",),
                read_document,
                timeout=max(180, settings.max_pages * settings.ocr_page_timeout),
            ),
            Tool("page.render", ("verifier",), png_bytes),
            Tool("fields.validate", ("validator",), build_fields),
        ],
        job.setdefault("tool_calls", []),
        timeline=job.setdefault("execution_trace", []),
        persist=persist,
    )

    async def model_call(operation, payload, sources=()):
        if control:
            await control()
        cache_payload = {key: value for key, value in payload.items() if key != "images"}
        cache_payload["images"] = [
            {"page": image["page"], "hash": content_hash(image["data"])}
            for image in payload.get("images", [])
        ]
        fingerprint = digest([operation, cache_payload])
        cache = job.setdefault("model_cache", {})
        if fingerprint in cache:
            return copy.deepcopy(cache[fingerprint])
        attempts = job.setdefault("model_attempts", {})
        if attempts.get(fingerprint, 0) >= settings.max_stage_calls:
            raise ValueError("이 단계의 모델 호출 예산을 소진했습니다. 직접 입력해 주세요.")
        attempts[fingerprint] = attempts.get(fingerprint, 0) + 1
        persist()
        context = {}
        if payload.get("document_type"):
            context["document_type"] = payload["document_type"]
        if payload.get("pages"):
            context["pages"] = [page["page"] for page in payload["pages"]]
        if payload.get("images"):
            context["pages"] = [page["page"] for page in payload["images"]]
        if payload.get("target_field_ids"):
            context["target_field_count"] = len(payload["target_field_ids"])
        event = begin_span("agent", operation, "model." + operation, context=context)
        visit(job, operation, *sources, status="running")
        event.update(agent=operation, model=settings.extraction_model)
        job.setdefault("agent_calls", []).append(event)
        job["execution_trace"].append(event)
        started = time.monotonic()
        try:
            await progress(
                {"classify": "classifying", "extract": "extracting", "verify": "verifying"}[operation]
            )
            response, usage = await asyncio.wait_for(
                model.invoke(operation, payload), settings.timeout_seconds + 10
            )
            event["usage"] = usage_numbers(usage)
            event["usage_status"] = "reported" if event["usage"] else "unavailable"
            add_usage(job.setdefault("usage", {}), event["usage"])
            event["status"] = "completed"
            if control:
                await control()
            cache[fingerprint] = copy.deepcopy(response)
            return response
        except BaseException as error:
            event["status"] = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            event["usage"] = usage_numbers(getattr(error, "usage", {}))
            event["usage_status"] = "reported" if event["usage"] else "unavailable"
            if getattr(error, "api_call_started", None) is False:
                event["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                event["usage_status"] = "not_sent"
            add_usage(job.setdefault("usage", {}), event["usage"])
            raise
        finally:
            event.update(finished_at=timestamp(), duration_ms=round((time.monotonic() - started) * 1000))
            visit(job, operation, status=event["status"])
            persist()

    async def read(state):
        await progress("reading")
        visit(job, "cache" if prior_reading else "format", "start")
        reading = prior_reading or await executor.call(
            "reader", "document.read", data=data, media_type=media_type, settings=settings
        )
        if not prior_reading:
            parent = next(
                (event for event in job["tool_calls"] if event.get("tool") == "document.read"), None
            )
            for child in reading.get("reader_calls", []):
                child["parent_id"] = parent["id"] if parent else None
                job["execution_trace"].append(child)
                job["tool_calls"].append(child)
            reader_ends.extend(reading_route(job, reading))
        else:
            reader_ends.append("cache")
        job["reading"] = reading
        return {"reading": reading}

    async def classify(state):
        reading = state["reading"]
        if verification_parts:
            return {"parts": copy.deepcopy(verification_parts)}
        if preset_parts:
            visit(job, "manual_type", *reader_ends)
            parts = copy.deepcopy(preset_parts)
        elif read_only or reading["status"] == "unreadable":
            visit(job, "type_wait", *reader_ends)
            parts = [
                {
                    "document_type": "unknown",
                    "start_page": 1,
                    "end_page": len(reading["pages"]),
                    "label": "유형 확인 필요",
                    "needs_review": True,
                    "reason": "유형을 지정하거나 문서 처리를 실행해 주세요.",
                }
            ]
        else:
            response = await model_call(
                "classify",
                {"pages": [{"page": p["page"], "text": p["text"][:16000]} for p in reading["pages"]]},
                sources=reader_ends,
            )
            parts = Classification.model_validate(response).model_dump()["parts"]
        covered = []
        for part in parts:
            covered.extend(range(part["start_page"], part["end_page"] + 1))
        if sorted(covered) != list(range(1, len(reading["pages"]) + 1)) or not parts:
            parts = [
                {
                    "document_type": "unknown",
                    "start_page": 1,
                    "end_page": len(reading["pages"]),
                    "label": "문서 구간 확인 필요",
                    "needs_review": True,
                    "reason": "문서 경계를 확인하지 못했습니다. 페이지 범위와 유형을 지정해 주세요.",
                }
            ]
        for part in parts:
            part.setdefault("id", identifier("part"))
            part["fields"] = []
        if preset_parts or reading["status"] != "unreadable":
            visit(job, "type_gate", "manual_type" if preset_parts else "classify")
        job["parts"] = parts
        return {"parts": parts}

    async def extract(state):
        reading, parts = state["reading"], state["parts"]
        if verification_parts:
            return {"parts": parts}
        for part in parts:
            kind = part["document_type"]
            if kind not in DOCUMENT_TYPES or part.get("needs_review"):
                if preset_parts or reading["status"] != "unreadable":
                    visit(job, "type_wait", "type_gate")
                visit(job, "review", "type_wait", status="waiting")
                part["status"] = "needs_classification"
                continue
            try:
                pages = [p for p in reading["pages"] if part["start_page"] <= p["page"] <= part["end_page"]]
                response = await model_call(
                    "extract",
                    {
                        "document_type": kind,
                        "pages": [{"page": p["page"], "text": p["text"]} for p in pages],
                        "extra_definitions": ROLE_FIELDS if kind == "contract" else {},
                    },
                    sources=("type_gate",),
                )
                visit(job, "validate", "extract", status="running")
                part["fields"] = await executor.call(
                    "validator",
                    "fields.validate",
                    flow_node="validate",
                    response=response,
                    part=part,
                    reading=reading,
                )
                part["status"] = "review_ready"
            except asyncio.CancelledError:
                raise
            except Exception:
                part["status"] = "failed"
                part["reason"] = "항목 추출에 실패했습니다. 다시 시도하거나 직접 입력할 수 있습니다."
                part["fields"] = [field_seed(k, v) for k, v in profiles()[kind]["fields"].items()]
            await progress("extracting")
        return {"parts": parts}

    async def verify(state):
        if not auto_verify and not verify_field_ids:
            return {"parts": state["parts"]}
        for part in state["parts"]:
            if part["status"] != "review_ready":
                continue
            required = {
                "contract": {"employee_name", "employer_name", "contract_rate", "pay_type"},
                "payslip": {"gross", "net", "period_start", "period_end"},
                "attendance": {"period_start", "period_end"},
                "deposit": {"account_holder"},
            }.get(part["document_type"], set())
            targets = (
                [f for f in part["fields"] if f["id"] in verify_field_ids]
                if verify_field_ids
                else [
                    f
                    for f in part["fields"]
                    if f["validation_status"] == "needs_review"
                    or "MISSING_OUTPUT" in f["issues"]
                    or f["field_id"] in required
                    and f["validation_status"] == "missing"
                ]
            )
            visit(job, "recheck", *(reader_ends if verification_parts else ["validate"]))
            if not targets:
                visit(job, "review", "recheck", status="waiting")
                continue
            targets = targets[:50]
            page_numbers = list(
                dict.fromkeys((f.get("evidence") or {}).get("page") or part["start_page"] for f in targets)
            )[:6]
            targets = [
                f
                for f in targets
                if ((f.get("evidence") or {}).get("page") or part["start_page"]) in page_numbers
            ]
            try:
                visit(job, "render_verify", "recheck", status="running")
                images = [
                    {
                        "page": page,
                        "data": await executor.call(
                            "verifier",
                            "page.render",
                            flow_node="render_verify",
                            data=data,
                            media_type=media_type,
                            page=page,
                            max_side=2200,
                        ),
                    }
                    for page in page_numbers
                ]
                response = await model_call(
                    "verify",
                    {
                        "document_type": part["document_type"],
                        "target_field_ids": [f["field_id"] for f in targets],
                        "targets": [
                            {
                                "field_id": f["field_id"],
                                "label": f["label"],
                                "raw_value": f["raw_value"],
                                "quote": (f.get("evidence") or {}).get("quote", ""),
                            }
                            for f in targets
                        ],
                        "extra_definitions": ROLE_FIELDS,
                        "images": images,
                    },
                    sources=("render_verify",),
                )
                visit(job, "validate_again", "verify", status="running")
                verified = await executor.call(
                    "validator",
                    "fields.validate",
                    flow_node="validate_again",
                    response=response,
                    part=part,
                    reading=state["reading"],
                    visual=True,
                    targets={f["field_id"] for f in targets},
                )
                visit(job, "review", "validate_again", status="waiting")
                replacements = {f["field_id"]: f for f in verified}
                for field in targets:
                    replacement = replacements.get(field["field_id"])
                    if replacement and replacement["raw_value"] is not None:
                        field["candidates"].extend(replacement["candidates"])
                        if field["raw_value"] and field["raw_value"] != replacement["raw_value"]:
                            field["issues"] = list(dict.fromkeys(field["issues"] + ["CONFLICT"]))
                        field["rechecked"] = True
                    else:
                        field["issues"].append("RECHECK_FAILED")
            except asyncio.CancelledError:
                raise
            except Exception:
                for field in targets:
                    field["issues"].append("RECHECK_FAILED")
            await progress("verifying")
        return {"parts": state["parts"]}

    graph = StateGraph(PipelineState)
    for name, node in (("read", read), ("classify", classify), ("extract", extract), ("verify", verify)):
        graph.add_node(name, node)
    graph.add_edge(START, "read")
    graph.add_edge("read", "classify")
    graph.add_edge("classify", "extract")
    graph.add_edge("extract", "verify")
    graph.add_edge("verify", END)
    return await graph.compile().ainvoke({})
