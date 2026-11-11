"""Keep the base extraction, optional PDF candidates and reviewed choices separate."""

import copy
import re
from datetime import UTC, datetime

from agent.schemas.public.inputs import EvidenceRef
from agent.schemas.public.results import ExtractedField, ExtractionCandidate
from agent.services.extraction_profiles import RATE_UNITS, canonical_document_type, field_definition
from agent.services.extraction_review import candidate_value
from agent.services.extraction_validation import candidate_range_valid
from agent.services.hashing import digest
from agent.services.normalization import normalize_scalar


def initialize_candidates(case):
    """Read legacy successful extraction as A without changing stored artifacts."""
    case.setdefault("extraction_a", copy.deepcopy(case.get("extraction")))
    case.setdefault("extraction_a_revision", case.get("extraction_revision", 0))
    case.setdefault(
        "extraction_a_context",
        {
            "ocr_result_id": (case.get("ocr") or {}).get("id"),
            "document_hash": (case.get("ocr") or {}).get("content_hash"),
            "job_id": None,
            "run_id": None,
        },
    )
    case.setdefault("supplements", [])
    case.setdefault("candidate_selections", {})
    case.setdefault("candidate_selection_history", [])


def clear_candidates(case, reason):
    initialize_candidates(case)
    if case["extraction_a"] or case["supplements"] or case["candidate_selections"]:
        case["candidate_selection_history"].append(
            {
                "event": "invalidated",
                "reason": reason,
                "created_at": datetime.now(UTC).isoformat(),
                "extraction_a_revision": case["extraction_a_revision"],
                "extraction_a_context": copy.deepcopy(case["extraction_a_context"]),
                "extraction_a": copy.deepcopy(case["extraction_a"]),
                "supplements": copy.deepcopy(case["supplements"]),
                "candidate_selections": copy.deepcopy(case["candidate_selections"]),
            }
        )
    case.update(
        extraction_a=None,
        extraction_a_revision=0,
        extraction_a_context={},
        supplements=[],
        candidate_selections={},
    )


def require_revision(case, extraction_revision):
    if type(extraction_revision) is not int or extraction_revision != case.get("extraction_revision"):
        raise ValueError("추출 후보가 변경되었습니다. 최신 항목을 불러온 뒤 다시 선택해 주세요.")


def source_location(payload, field):
    evidence = {item["evidence_id"]: item for item in payload.get("evidence_catalog", [])}
    original = next((evidence[ref] for ref in field.get("evidence_refs", []) if ref in evidence), {})
    page = field.get("source_page") or original.get("page")
    if type(page) is not int or not 1 <= page <= payload.get("page_count", 0):
        page = None
    return page, field.get("source_quote") or original.get("quote") or None


def supplement_targets(case, field_ids, extraction_revision):
    initialize_candidates(case)
    require_revision(case, extraction_revision)
    base = case.get("extraction_a")
    if not base or case.get("extraction_needs_review"):
        raise ValueError("현재 문서의 기본 항목 정리를 먼저 완료해 주세요.")
    if (
        not isinstance(field_ids, list)
        or not 1 <= len(field_ids) <= 50
        or any(not isinstance(key, str) for key in field_ids)
        or len(set(field_ids)) != len(field_ids)
    ):
        raise ValueError("보완할 항목을 중복 없이 1개부터 50개까지 선택해 주세요.")
    fields = {item["field_id"]: item for item in base["fields"]}
    effective = {item["field_id"]: item for item in case["extraction"]["fields"]}
    targets = []
    for key in field_ids:
        field = fields.get(key)
        if not field or field.get("validation_status", "valid") not in ("needs_review", "missing"):
            raise ValueError("기본 결과에서 확인이 필요한 항목만 보완할 수 있습니다.")
        if candidate_value(effective.get(key, {})) is not None:
            raise ValueError("이미 확인한 항목입니다. 미확인 항목만 보완해 주세요.")
        page, quote = source_location(base, field)
        row = re.match(r"^([a-z]+_[1-9]\d*_)\w+$", key)
        context = {}
        if row:
            for sibling_id, sibling in fields.items():
                if sibling_id.startswith(row[1]):
                    if candidate_value(sibling) is not None:
                        context[sibling_id] = sibling.get("raw_value") or candidate_value(sibling)
                    for name in ("raw_value", "normalized_value"):
                        context[sibling_id + "." + name] = sibling.get(name)
        targets.append({"field_id": key, "page": page, "source_quote": quote, "row_context": context})
    return targets


def validate_supplement_lineage(case, job):
    initialize_candidates(case)
    context = case["extraction_a_context"]
    if (
        not case["extraction_a"]
        or job.get("base_extraction_revision") != case["extraction_a_revision"]
        or job.get("ocr_result_id") != (case.get("ocr") or {}).get("id")
        or context.get("ocr_result_id") != job.get("ocr_result_id")
        or job.get("document_hash") != context.get("document_hash")
    ):
        raise ValueError("기본 추출 또는 원본이 변경되어 이전 보완 결과를 사용할 수 없습니다.")


def supplement_entry(case, job, payload):
    validate_supplement_lineage(case, job)
    data = payload.model_dump(mode="json")
    base = case["extraction_a"]
    allowed = {item["field_id"] for item in job["task"]["payload"]["target_fields"]}
    returned = [field["field_id"] for field in data["fields"]]
    if (
        any(data[key] != base[key] for key in ("document_id", "document_version"))
        or canonical_document_type(data["document_type"]) != canonical_document_type(base["document_type"])
        or len(returned) != len(set(returned))
        or set(returned) - allowed
    ):
        raise ValueError("보완 결과의 문서 또는 대상 항목이 현재 요청과 다릅니다.")
    source_id = job["task"]["payload"]["source"]["source_id"]
    if any(item["source_id"] != source_id for item in data["evidence_catalog"]):
        raise ValueError("보완 결과의 원문 연결을 확인할 수 없습니다.")
    return {
        "id": job["id"],
        "base_extraction_revision": case["extraction_a_revision"],
        "ocr_result_id": job["ocr_result_id"],
        "document_hash": job["document_hash"],
        "run_id": job["task"]["context"]["run_id"],
        "created_at": datetime.now(UTC).isoformat(),
        "payload": data,
    }


def _manual_field(base, field, value, unit):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError("원문에서 확인한 값을 입력해 주세요.")
    key = field["field_id"]
    expected = (field_definition(base["document_type"], key) or {}).get("unit")
    unit = unit or field.get("unit") or expected
    if not expected or (unit not in RATE_UNITS if expected == "rate" else unit != expected):
        raise ValueError("항목에 맞는 단위를 선택해 주세요.")
    raw = value.strip()
    # Manual numeric inputs use explicit canonical values, not OCR punctuation repair.
    if unit.startswith("KRW") or unit in ("minute", "count"):
        if not candidate_range_valid(key, unit, raw, base["document_type"]):
            raise ValueError("금액·시간의 숫자 형식과 범위를 확인해 주세요.")
    normalized = "last_day" if unit == "day_of_month" and raw == "last_day" else normalize_scalar(raw, unit)
    if normalized is None or not candidate_range_valid(key, unit, normalized, base["document_type"]):
        raise ValueError("확인값의 형식과 범위를 확인해 주세요.")
    page, quote = source_location(base, field)
    return ExtractedField(
        field_id=key,
        raw_value=raw,
        normalized_value=normalized,
        suggested_value=None,
        validation_status="valid",
        unit=unit,
        evidence_refs=[],
        source_page=page,
        source_quote=quote,
        issues=["USER_CONFIRMED_SELECTION"],
    ).model_dump(mode="json")


def select_candidate(case, request, *, document_hash, source_id):
    """Return a complete effective candidate and audit record without mutating state."""
    initialize_candidates(case)
    if not isinstance(request, dict) or set(request) - {
        "field_id",
        "candidate_id",
        "value",
        "unit",
        "source_confirmed",
        "extraction_revision",
    }:
        raise ValueError("항목 선택 요청 형식을 확인해 주세요.")
    require_revision(case, request.get("extraction_revision"))
    if request.get("source_confirmed") is not True:
        raise ValueError("원본을 직접 확인한 뒤 항목을 선택해 주세요.")
    base = case["extraction_a"]
    key, candidate_id = request.get("field_id"), request.get("candidate_id")
    original = next((item for item in (base or {}).get("fields", []) if item["field_id"] == key), None)
    if original is None:
        raise ValueError("현재 기본 추출에 없는 항목입니다.")
    context = case["extraction_a_context"]
    if context.get("document_hash") not in (None, document_hash):
        raise ValueError("원본이 변경되었습니다. 기본 추출을 다시 실행해 주세요.")
    payload, supplement = base, None
    if candidate_id == "manual":
        chosen = _manual_field(base, original, request.get("value"), request.get("unit"))
    elif candidate_id == "A":
        chosen = copy.deepcopy(original)
    else:
        supplement = next(
            (item for item in case["supplements"] if candidate_id == item["id"] + ":" + key), None
        )
        if supplement is None:
            raise ValueError("현재 문서의 보완 후보를 찾을 수 없습니다.")
        validate_supplement_lineage(case, {**supplement, "ocr_result_id": supplement["ocr_result_id"]})
        payload = supplement["payload"]
        chosen = next((copy.deepcopy(item) for item in payload["fields"] if item["field_id"] == key), None)
        if chosen is None:
            raise ValueError("선택한 보완 결과에 해당 항목이 없습니다.")
    if candidate_value(chosen) is None:
        raise ValueError("형식 검사를 통과한 후보만 선택할 수 있습니다. 원문을 보고 직접 입력해 주세요.")
    expected = (field_definition(base["document_type"], key) or {}).get("unit")
    if (
        not expected
        or (chosen.get("unit") not in RATE_UNITS if expected == "rate" else chosen.get("unit") != expected)
        or not candidate_range_valid(
            key, chosen.get("unit"), chosen["normalized_value"], base["document_type"]
        )
    ):
        raise ValueError("선택한 후보의 단위와 값 범위를 확인해 주세요.")
    page, quote = source_location(payload, chosen)
    if supplement and (page is None or not quote):
        raise ValueError("보완 후보의 원문 페이지와 인용을 확인할 수 없습니다.")
    record = {
        "event": "selected",
        "field_id": key,
        "candidate_id": candidate_id,
        "source_confirmed": True,
        "value": chosen["normalized_value"],
        "unit": chosen["unit"],
        "base_extraction_revision": case["extraction_a_revision"],
        "previous_extraction_revision": case["extraction_revision"],
        "extraction_revision": case["extraction_revision"] + 1,
        "document_hash": document_hash,
        "ocr_result_id": context.get("ocr_result_id"),
        "job_id": supplement["id"] if supplement else context.get("job_id"),
        "run_id": supplement["run_id"] if supplement else context.get("run_id"),
        "source_quote": quote,
        "source_page": page,
        "created_at": datetime.now(UTC).isoformat(),
    }
    evidence_id = "candidate-choice-" + digest(record)
    confirmation = EvidenceRef(
        evidence_id=evidence_id,
        source_id=source_id,
        page=page,
        quote=f"사용자가 원문 확인 후 선택한 값: {chosen['normalized_value']} {chosen['unit']}",
        locator="candidate-selection:" + key,
        verification_method="manual_confirmed",
    ).model_dump(mode="json")
    effective = copy.deepcopy(case["extraction"])
    catalog = {item["evidence_id"]: item for item in effective["evidence_catalog"]}
    for item in payload["evidence_catalog"]:
        if item["evidence_id"] in chosen.get("evidence_refs", []):
            if item["source_id"] != source_id or (
                item["evidence_id"] in catalog and catalog[item["evidence_id"]] != item
            ):
                raise ValueError("선택한 후보의 원문 연결이 일치하지 않습니다.")
            catalog[item["evidence_id"]] = copy.deepcopy(item)
    if set(chosen.get("evidence_refs", [])) - set(catalog):
        raise ValueError("선택한 후보의 원문 근거가 없습니다.")
    catalog[evidence_id] = confirmation
    chosen["evidence_refs"] = [*chosen.get("evidence_refs", []), evidence_id]
    chosen["issues"] = list(dict.fromkeys([*chosen.get("issues", []), "USER_CONFIRMED_SELECTION"]))
    effective["fields"] = [chosen if item["field_id"] == key else item for item in effective["fields"]]
    effective["evidence_catalog"] = list(catalog.values())
    return ExtractionCandidate.model_validate(effective), record
