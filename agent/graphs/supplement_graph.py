"""Explicit PDF visual rereading of selected fields, never automatic confirmation."""

import io
import re

from langgraph.graph import END, START, StateGraph
from pypdf import PdfReader

from agent.graphs.extraction_graph import ExtractionState, normalized_text, verify_response
from agent.providers.base import ExtractionResponse
from agent.schemas.public.errors import DocumentUnreadable, IntegrityError, ModelFormatError, OCRRequired
from agent.schemas.public.inputs import EvidenceRef
from agent.schemas.public.results import ExtractedField, ExtractionCandidate
from agent.services.extraction_profiles import canonical_document_type, expected_unit, field_definition
from agent.services.hashing import content_hash, digest


def verify_supplement(response, targets, page_count, document_type):
    try:
        parsed = ExtractionResponse.model_validate(response)
    except Exception as exc:
        raise ModelFormatError("Invalid PDF supplement schema") from exc
    requested = {target.field_id: target for target in targets}
    if any(field.field_id not in requested for field in parsed.fields):
        raise ModelFormatError("PDF supplement returned an unrequested field")
    # Visual evidence is explicitly unverified. It must not be rejected because
    # the primary OCR/text stream contains different characters.
    checked = verify_response(parsed.model_dump(), [""] * page_count, document_type, [True] * page_count)
    present = {field.field_id for field in checked.fields}
    for field in checked.fields:
        target = requested[field.field_id]
        row = re.match(r"(?:transaction|earning|deduction|work)_\d+_", field.field_id)
        if row and field.raw_value is not None:
            # Bare keys are checked sibling facts; suffixed keys are unverified
            # hints. A target's own corrupt spelling cannot identify its row.
            siblings = {
                key: value
                for key, value in target.row_context.items()
                if key.startswith(row[0]) and "." not in key and key != field.field_id and value
            }
            anchors = [value for key, value in siblings.items() if key.endswith(("_reference", "_label"))]
            if not anchors:
                context_values = [
                    value
                    for key, value in siblings.items()
                    if key.endswith(("_date", "_time", "_deposit_amount", "_start", "_end", "_sender"))
                ]
                # Dates/amounts alone can recur; require at least two distinct
                # columns when no stable reference or item label is available.
                anchors = context_values if len(set(context_values)) >= 2 else []
            quote = re.sub(r"\s+", "", normalized_text(field.quote))
            if not anchors or not all(
                re.sub(r"\s+", "", normalized_text(value)) in quote for value in anchors
            ):
                field.quarantine("SUPPLEMENT_ROW_UNCERTAIN")
                field.evidence_valid = False
    from agent.services.extraction_validation import ReviewedField

    for field_id in requested.keys() - present:
        checked.fields.append(
            ReviewedField(
                field_id=field_id,
                raw_value=None,
                normalized_value=None,
                unit=expected_unit(document_type, field_id),
                page=requested[field_id].page or 1,
                quote="",
                issues=["PDF_SUPPLEMENT_VALUE_UNAVAILABLE"],
                validation_status="missing",
            )
        )
    return checked


def build_supplement_graph(runtime):
    payload = runtime.task.payload
    document_type = canonical_document_type(payload.document_type)
    targets = payload.target_fields

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
        async def inspect():
            if not payload.ocr_result_id:
                raise OCRRequired("Primary document reading is required before PDF supplement")
            if any(field_definition(document_type, target.field_id) is None for target in targets):
                raise IntegrityError("Unknown supplement target")
            data = await read()
            try:
                reader = PdfReader(io.BytesIO(data), strict=True)
                if reader.is_encrypted or not 1 <= len(reader.pages) <= runtime.settings.max_pages:
                    raise DocumentUnreadable("Unsupported PDF")
                count = len(reader.pages)
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
                    ("page_count", count),
                )
            ):
                raise IntegrityError("Primary reading belongs to different source content")
            if any(target.page is not None and target.page > count for target in targets):
                raise IntegrityError("Supplement target page outside original document")
            return {"page_count": count}

        return {"inspected": await runtime.step("inspect_document", inspect)}

    async def extract_candidates(state):
        async def extract():
            # PDF bytes live only in this request; no base64 document in graph
            # state, progress events or checkpoints.
            data = {
                "pdf_bytes": await read(),
                "source_method": "pdf_supplement",
                "document_type": document_type,
                "target_fields": [target.model_dump(mode="json") for target in targets],
            }
            return await runtime.call_model(
                "extract_candidates",
                "extract_pdf",
                data,
                validator=lambda response: verify_supplement(
                    response, targets, state["inspected"]["page_count"], document_type
                ),
            )

        return {"candidates": await runtime.step("extract_candidates", extract)}

    async def package_candidates(state):
        def package():
            count = state["inspected"]["page_count"]
            checked = verify_supplement(state["candidates"], targets, count, document_type)
            fields, evidence = [], []
            for item in checked.fields:
                refs = []
                if item.raw_value is not None and item.evidence_valid:
                    eid = digest(
                        [
                            payload.source.source_id,
                            runtime.context.job_id,
                            item.field_id,
                            item.page,
                            item.quote,
                        ]
                    )
                    evidence.append(
                        EvidenceRef(
                            evidence_id=eid,
                            source_id=payload.source.source_id,
                            page=item.page,
                            quote=item.quote,
                            verification_method="vision_unverified",
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
                        unit=item.unit or expected_unit(document_type, item.field_id),
                        evidence_refs=refs,
                        issues=[*item.issues, "PDF_REQUIRES_USER_CONFIRMATION"],
                    )
                )
            return ExtractionCandidate(
                document_id=payload.source.document_id,
                document_version=payload.source.document_version,
                document_type=document_type,
                extraction_version=payload.extraction_version,
                model_config_version=payload.model_config_version,
                fields=fields,
                evidence_catalog=evidence,
                issues=[*checked.issues, "PDF_SUPPLEMENT_REQUIRES_USER_CONFIRMATION"],
                page_count=count,
                verification_method="vision_unverified",
                source_method="pdf_supplement",
                page_methods=["pdf_supplement"] * count,
            ).model_dump(mode="json")

        return {"result": await runtime.step("package_candidates", package)}

    graph = StateGraph(ExtractionState)
    graph.add_node("inspect_document", inspect_document)
    graph.add_node("extract_candidates", extract_candidates)
    graph.add_node("package_candidates", package_candidates)
    graph.add_edge(START, "inspect_document")
    graph.add_edge("inspect_document", "extract_candidates")
    graph.add_edge("extract_candidates", "package_candidates")
    graph.add_edge("package_candidates", END)
    return graph.compile()
