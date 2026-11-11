"""Read checked extraction candidates without promoting review-only suggestions."""


def candidate_value(field):
    """Legacy successful results remain readable; all values still require user review."""
    if field.get("validation_status", "valid") != "valid":
        return None
    return field.get("normalized_value")


def validation_metadata(field, *, document_id=None, title=None):
    return {
        "field_id": field["field_id"],
        "raw_value": field.get("raw_value"),
        "suggested_value": field.get("suggested_value", field.get("normalized_value")),
        "normalized_value": candidate_value(field),
        "unit": field.get("unit"),
        "validation_status": field.get("validation_status", "valid"),
        "validation_reasons": list(field.get("validation_reasons", [])),
        "evidence_refs": list(field.get("evidence_refs", [])),
        "source_quote": field.get("source_quote"),
        "source_page": field.get("source_page"),
        "source_document_id": document_id,
        "source_title": title,
    }


def row_requires_replacement(row, editable_suffix):
    """Noneditable invalid cells cannot be repaired by confirming an amount."""
    return any(
        (
            item.get("validation_status") == "needs_review"
            or item.get("validation_status") == "missing"
            and item["field_id"].endswith(("_label", "_date"))
        )
        and not item["field_id"].endswith("_" + editable_suffix)
        for item in row.get("validation_warnings", [])
    )
