import io
import re
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pypdf import PdfReader

from agent.providers.base import ExtractionResponse
from agent.schemas.public.errors import (
    DocumentUnreadable,
    IntegrityError,
    ModelFormatError,
    OCRRequired,
)
from agent.schemas.public.inputs import EvidenceRef
from agent.schemas.public.results import ExtractedField, ExtractionCandidate
from agent.services.extraction_profiles import (
    DOCUMENT_TYPES,
    RATE_UNITS,
    canonical_document_type,
    expected_unit,
    field_definition,
)
from agent.services.extraction_tables import compact_text as normalized_text
from agent.services.extraction_tables import expand_tables_for_review
from agent.services.extraction_validation import ReviewedExtraction, ReviewedField, candidate_range_valid
from agent.services.hashing import content_hash, digest
from agent.services.normalization import (
    CANONICAL_UNITS,
    DURATION_UNIT_PATTERN,
    normalize_evidenced_scalar,
    validate_payment_day_candidate,
    validate_payment_day_context,
    validate_rate_unit_context,
)


def _standalone_number_present(raw, quote):
    if not re.fullmatch(r"-?\d+(?:,\d{3})*(?:\.\d+)?(?:(?:st|nd|rd|th)|원|KRW)?", raw.strip(), re.I):
        return True
    for match in re.finditer(rf"(?<![\d.,+\-−]){re.escape(raw.strip())}(?![\d.,])", quote):
        if raw.strip().startswith("-") or not re.search(r"[+\-−]\s*\(?\s*$", quote[: match.start()]):
            return True
    return False


def _source_scalar_spelling(item, source_unit):
    """Recover spacing/brackets around one explicit number and its own unit.

    This is deliberately narrower than whitespace-insensitive quote matching.
    The number and unit spelling must both be present together in the quote;
    no other amount, translated unit, or inferred duration can supply them.
    """
    if source_unit == "minute":
        number = r"\d+(?:\.\d+)?"
        units = DURATION_UNIT_PATTERN
    elif source_unit == "day_of_month":
        number = r"\d{1,2}"
        units = r"일"
    else:
        return None
    scalar = rf"(?:\(\s*(?P<bracketed>{number})\s*\)|(?P<plain>{number}))\s*(?P<unit>{units})"
    raw = re.fullmatch(scalar, item.raw_value.strip(), re.I)
    if raw is None:
        return None
    quantity = raw["bracketed"] or raw["plain"]
    unit_pattern = r"시\s*간" if re.fullmatch(r"시\s*간", raw["unit"]) else re.escape(raw["unit"])
    source = re.compile(
        rf"(?<![\d.,+\-−])(?:\(\s*{re.escape(quantity)}\s*\)|{re.escape(quantity)})"
        rf"\s*{unit_pattern}(?![a-zA-Z])",
        re.I,
    )
    candidates = []
    for match in source.finditer(item.quote):
        before, after = item.quote[: match.start()], item.quote[match.end() :]
        # A sign separated from its number is still a sign. Never turn a
        # negative source quantity into a positive candidate.
        if re.search(r"[+\-−]\s*\(?\s*$", before):
            continue
        if source_unit == "minute":
            # A single component cannot stand for '1 hour 30 minutes'.
            component = rf"{number}\s*(?:{units})(?![a-z가-힣])"
            if re.search(rf"{component}\s*$", before, re.I) or re.match(rf"\s*{component}", after, re.I):
                continue
            expected_period = {
                "break_minutes": "day",
                "scheduled_daily_minutes": "day",
                "scheduled_weekly_minutes": "week",
            }.get(item.field_id)
            prefix = re.search(r"(?:1\s*)?(일|하루|주)\s*[:：]?\s*$", before)
            suffix = re.match(r"\s*(?:per\s+|/\s*)(day|week|일|주)\b", after, re.I)
            periods = {"일": "day", "하루": "day", "day": "day", "주": "week", "week": "week"}
            if expected_period and any(
                periods[period[1].lower()] != expected_period for period in (prefix, suffix) if period
            ):
                continue
        candidates.append(match.group())
    if not candidates:
        raise ValueError("No complete source scalar with matching number and unit")
    # Keep the exact OCR spelling for review, but normalize the equivalent
    # scalar without its purely typographical parentheses.
    return candidates[0], quantity + " " + raw["unit"]


def verify_response(response, pages, expected_type, image_pages=None):
    if (
        expected_type == "auto"
        and isinstance(response, dict)
        and response.get("document_type") in {"auto", "unknown", "unsupported", "mixed"}
    ):
        raise ModelFormatError(
            "문서 유형을 분류하지 못했습니다. 유형을 직접 선택하고 다시 추출해 주세요.",
            reason="DOCUMENT_TYPE_UNRESOLVED",
        )
    response, table_reasons, model_values = expand_tables_for_review(response, pages, image_pages)
    try:
        response = ExtractionResponse.model_validate(response)
    except Exception as exc:
        raise ModelFormatError("Invalid extraction schema") from exc
    actual_type = canonical_document_type(response.document_type)
    canonical_expected = canonical_document_type(expected_type)
    if expected_type == "auto" and response.document_type not in DOCUMENT_TYPES:
        raise ModelFormatError(
            "문서 유형을 분류하지 못했습니다. 유형을 직접 선택하고 다시 추출해 주세요.",
            reason="DOCUMENT_TYPE_UNRESOLVED",
        )
    if expected_type != "auto" and actual_type != canonical_expected:
        raise ModelFormatError(
            "Document type conflict; user confirmation required", reason="DOCUMENT_TYPE_MISMATCH"
        )
    reviewed, seen = [], {}
    for original in response.fields:
        item = ReviewedField.from_model(original)
        if item.field_id in model_values:
            for key, value in model_values[item.field_id].items():
                setattr(item, key, value)
        if item.field_id in seen:
            # Keep one explicit placeholder rather than letting a later value
            # silently replace the first in dictionary-based consumers.
            seen[item.field_id].quarantine("DUPLICATE_FIELD_ID")
            continue
        seen[item.field_id] = item
        reviewed.append(item)
        for reason in table_reasons.get(item.field_id, []):
            item.quarantine(reason)
        if not 1 <= item.page <= len(pages):
            item.quarantine("SOURCE_PAGE_INVALID")
            continue
        if item.raw_value is None:
            if item.suggested_value is not None:
                item.quarantine("SOURCE_VALUE_MISSING")
            elif item.validation_status != "needs_review":
                item.validation_status = "missing"
            continue
        item.evidence_valid = bool(item.quote) and (
            bool((image_pages or [False] * len(pages))[item.page - 1])
            or normalized_text(item.quote) in normalized_text(pages[item.page - 1])
        )
        if not item.evidence_valid:
            item.quarantine("SOURCE_QUOTE_MISMATCH")
            continue
        if field_definition(actual_type, item.field_id) is None:
            item.quarantine("UNKNOWN_FIELD")
            continue
        if item.validation_status == "needs_review":
            continue
        source_unit = (
            item.unit or expected_unit(actual_type, item.field_id) or CANONICAL_UNITS.get(item.field_id)
        )
        try:
            spelling = _source_scalar_spelling(item, source_unit)
            if not _standalone_number_present(item.raw_value, item.quote):
                raise ValueError("Numeric source boundary or sign does not match")
        except ValueError:
            item.quarantine("SOURCE_VALUE_MISMATCH")
            continue
        normalization_raw = item.raw_value
        if not item.quote or normalized_text(item.raw_value) not in normalized_text(item.quote):
            if spelling is None:
                item.quarantine("SOURCE_VALUE_MISMATCH")
                continue
            item.raw_value, normalization_raw = spelling
            if "SOURCE_SCALAR_SPELLING_RESTORED" not in item.issues:
                item.issues.append("SOURCE_SCALAR_SPELLING_RESTORED")
        elif spelling and normalized_text(spelling[0]) == normalized_text(item.raw_value):
            normalization_raw = spelling[1]
        if item.normalized_value is not None:
            if not candidate_range_valid(item.field_id, source_unit, item.normalized_value, actual_type):
                item.quarantine("CANDIDATE_RANGE_INVALID")
                continue
            try:
                definition = field_definition(actual_type, item.field_id)
                if definition and (
                    definition["unit"] == "rate"
                    and source_unit not in RATE_UNITS
                    or definition["unit"] != "rate"
                    and source_unit != definition["unit"]
                ):
                    raise ValueError("Field unit does not match document vocabulary")
                if source_unit == "day_of_month":
                    validate_payment_day_context(item.raw_value, item.quote)
                if source_unit in RATE_UNITS:
                    validate_rate_unit_context(item.raw_value, source_unit, item.quote)
                try:
                    expected = normalize_evidenced_scalar(
                        normalization_raw,
                        source_unit,
                        item.quote,
                        field_id=item.field_id,
                    )
                except ValueError:
                    if source_unit != "day_of_month":
                        raise
                    expected = validate_payment_day_candidate(normalization_raw, item.normalized_value)
                if expected is not None and expected != item.normalized_value:
                    raise ValueError("Normalized value differs from explicit source scalar")
            except ValueError, ArithmeticError:
                item.quarantine("SOURCE_NORMALIZATION_INVALID")
        else:
            item.quarantine("NORMALIZED_VALUE_UNAVAILABLE")
    for start_id, end_id in (("contract_start", "contract_end"), ("period_start", "period_end")):
        start, end = seen.get(start_id), seen.get(end_id)
        if (
            start
            and end
            and start.validation_status == end.validation_status == "valid"
            and (start.unit or expected_unit(actual_type, start_id)) == "date"
            and (end.unit or expected_unit(actual_type, end_id)) == "date"
            and date.fromisoformat(start.normalized_value) > date.fromisoformat(end.normalized_value)
        ):
            start.quarantine("PERIOD_CONFLICT")
            end.quarantine("PERIOD_CONFLICT")
    # Structural failures invalidate the whole row. An isolated amount error
    # preserves its valid label/date so the user can repair that cell directly.
    structural_reasons = {
        "SOURCE_PAGE_INVALID",
        "SOURCE_QUOTE_MISMATCH",
        "DUPLICATE_FIELD_ID",
        "TABLE_SOURCE_MISMATCH",
        "TABLE_ROW_SOURCE_REUSED",
        "TABLE_SHAPE_INVALID",
    }
    row_reasons, incomplete = {}, False
    for item in reviewed:
        match = re.fullmatch(r"((?:work|earning|deduction|transaction)_[1-9]\d*)_[a-z_]+", item.field_id)
        if match and item.validation_status == "needs_review":
            incomplete = True
            structural = set(item.validation_reasons) & structural_reasons
            if structural:
                row_reasons.setdefault(match[1], set()).update(structural)
    for item in reviewed:
        match = re.fullmatch(r"((?:work|earning|deduction|transaction)_[1-9]\d*)_[a-z_]+", item.field_id)
        if match and match[1] in row_reasons:
            for reason in sorted(row_reasons[match[1]]):
                item.quarantine(reason)
        if set(item.validation_reasons) & (
            structural_reasons
            | {"SOURCE_VALUE_MISSING", "SOURCE_VALUE_MISMATCH", "TABLE_CELL_SOURCE_MISMATCH"}
        ):
            item.evidence_valid = False
    issues = list(response.issues)
    if incomplete and "EXTRACTION_INCOMPLETE" not in issues:
        issues.append("EXTRACTION_INCOMPLETE")
    return ReviewedExtraction(document_type=response.document_type, fields=reviewed, issues=issues)


class ExtractionState(TypedDict, total=False):
    inspected: dict[str, Any]
    candidates: dict[str, Any]
    verified: bool
    result: dict[str, Any]


def build_extraction_graph(runtime):
    payload = runtime.task.payload
    # Kept as an input alias for previously saved local cases. Recognition and
    # classification now follow the same pipeline as every other payslip.
    document_type = canonical_document_type(payload.document_type)

    async def read():
        content = await runtime.ports.read_source(runtime.context, payload.source)
        if (
            content.document_version != payload.source.document_version
            or content.content_hash != payload.source.content_hash
            or content_hash(content.data) != payload.source.content_hash
        ):
            raise IntegrityError("Source version or content hash mismatch")
        if len(content.data) > runtime.settings.max_file_bytes or content.mime_type != "application/pdf":
            raise DocumentUnreadable("Document size or MIME is unsupported")
        return content.data

    async def inspect_document(state):
        async def load_recognized_document():
            if not payload.ocr_result_id:
                raise OCRRequired("A completed OCR result is required before AI processing")
            data = await read()
            try:
                reader = PdfReader(io.BytesIO(data), strict=True)
                if reader.is_encrypted:
                    raise DocumentUnreadable("Encrypted PDFs are not supported")
                if not 1 <= len(reader.pages) <= runtime.settings.max_pages:
                    raise DocumentUnreadable("Page count exceeds configured limit")
                page_count = len(reader.pages)
            except DocumentUnreadable:
                raise
            except Exception as exc:
                raise DocumentUnreadable("PDF could not be inspected") from exc
            recognized = await runtime.ports.read_ocr(runtime.context, payload.source, payload.ocr_result_id)
            if not isinstance(recognized, dict) or any(
                recognized.get(key) != expected
                for key, expected in (
                    ("id", payload.ocr_result_id),
                    ("document_id", payload.source.document_id),
                    ("document_version", payload.source.document_version),
                    ("content_hash", payload.source.content_hash),
                    ("page_count", page_count),
                )
            ):
                raise IntegrityError("Stored OCR result belongs to different source content")
            pages, ocr_pages = recognized.get("pages"), recognized.get("ocr_pages")
            if (
                not isinstance(pages, list)
                or len(pages) != page_count
                or any(not isinstance(page, str) for page in pages)
                or not any(page.strip() for page in pages)
                or not isinstance(ocr_pages, list)
                or len(ocr_pages) != page_count
                or any(
                    not isinstance(page, dict) or page.get("text") != text
                    for page, text in zip(ocr_pages, pages)
                )
            ):
                raise OCRRequired("Stored OCR pages are missing or invalid")
            page_methods = recognized.get("page_methods", ["python_ocr"] * page_count)
            if (
                not isinstance(page_methods, list)
                or len(page_methods) != page_count
                or any(method not in {"pdf_text", "python_ocr"} for method in page_methods)
            ):
                raise IntegrityError("Invalid stored page reading methods")
            source_method = page_methods[0] if len(set(page_methods)) == 1 else "mixed"
            return {
                "pages": pages,
                "page_count": page_count,
                "image_pages": [False] * page_count,
                "ocr_pages": ocr_pages,
                "ocr_result_id": payload.ocr_result_id,
                "page_methods": page_methods,
                "source_method": source_method,
            }

        return {"inspected": await runtime.step("inspect_document", load_recognized_document)}

    async def extract_candidates(state):
        async def extract():
            inspected = state["inspected"]
            data = {
                "document_type": document_type,
                "pages": inspected["pages"],
                "page_text": "\n\n".join(f"PAGE {i}\n{p}" for i, p in enumerate(inspected["pages"], 1)),
                "source_method": inspected["source_method"],
                "page_methods": inspected["page_methods"],
            }
            return await runtime.call_model(
                "extract_candidates",
                "extract",
                data,
                validator=lambda response: verify_response(
                    response, inspected["pages"], document_type, inspected.get("image_pages")
                ),
            )

        return {"candidates": await runtime.step("extract_candidates", extract)}

    async def verify_candidates(state):
        def verify():
            verify_response(
                state["candidates"],
                state["inspected"]["pages"],
                document_type,
                state["inspected"].get("image_pages"),
            )
            return True

        return {"verified": await runtime.step("verify_candidates", verify)}

    async def package_candidates(state):
        def package():
            if not state["verified"]:
                raise IntegrityError("Unverified extraction")
            response = verify_response(
                state["candidates"],
                state["inspected"]["pages"],
                document_type,
                state["inspected"].get("image_pages"),
            )
            output_type = response.document_type if payload.document_type == "auto" else payload.document_type
            vocabulary_type = canonical_document_type(response.document_type)
            fields, evidence = [], []
            for item in response.fields:
                refs = []
                if item.raw_value is not None and item.evidence_valid:
                    eid = digest([payload.source.source_id, item.field_id, item.page, item.quote])
                    evidence.append(
                        EvidenceRef(
                            evidence_id=eid,
                            source_id=payload.source.source_id,
                            page=item.page,
                            quote=item.quote,
                            verification_method=(
                                "text_match"
                                if state["inspected"]["page_methods"][item.page - 1] == "pdf_text"
                                else "ocr_unverified"
                            ),
                        )
                    )
                    refs = [eid]
                fields.append(
                    ExtractedField(
                        field_id=item.field_id,
                        raw_value=item.raw_value,
                        normalized_value=item.normalized_value,
                        suggested_value=item.suggested_value,
                        validation_status=item.validation_status,
                        validation_reasons=item.validation_reasons,
                        source_quote=item.source_quote,
                        source_page=item.source_page,
                        unit=item.unit
                        or (
                            (
                                expected_unit(vocabulary_type, item.field_id)
                                or CANONICAL_UNITS.get(item.field_id)
                            )
                            if item.raw_value
                            else None
                        ),
                        evidence_refs=refs,
                        issues=item.issues,
                    )
                )
            issues = list(response.issues)
            method = state["inspected"]["source_method"]
            marker = "PDF_TEXT_AI_CLASSIFICATION" if method == "pdf_text" else "PYTHON_OCR_AI_CLASSIFICATION"
            if marker not in issues:
                issues.append(marker)
            confirmation_marker = (
                "SOURCE_REQUIRES_USER_CONFIRMATION"
                if method == "pdf_text"
                else "OCR_REQUIRES_USER_CONFIRMATION"
            )
            if confirmation_marker not in issues:
                issues.append(confirmation_marker)
            if (
                any(
                    line.get("confidence", 100) < 60
                    for page in state["inspected"]["ocr_pages"]
                    for line in page.get("lines", [])
                )
                and "OCR_LOW_CONFIDENCE" not in issues
            ):
                issues.append("OCR_LOW_CONFIDENCE")
            return ExtractionCandidate(
                document_id=payload.source.document_id,
                document_version=payload.source.document_version,
                document_type=output_type,
                extraction_version=payload.extraction_version,
                model_config_version=payload.model_config_version,
                fields=fields,
                evidence_catalog=evidence,
                issues=issues,
                page_count=state["inspected"]["page_count"],
                verification_method="text_match" if method == "pdf_text" else "ocr_unverified",
                recognized_pages=state["inspected"]["pages"],
                source_method=method,
                page_methods=state["inspected"]["page_methods"],
            ).model_dump(mode="json")

        return {"result": await runtime.step("package_candidates", package)}

    graph = StateGraph(ExtractionState)
    names = ["inspect_document", "extract_candidates", "verify_candidates", "package_candidates"]
    for name, fn in zip(names, [inspect_document, extract_candidates, verify_candidates, package_candidates]):
        graph.add_node(name, fn)
    graph.add_edge(START, names[0])
    for a, b in zip(names, names[1:]):
        graph.add_edge(a, b)
    graph.add_edge(names[-1], END)
    return graph.compile()
