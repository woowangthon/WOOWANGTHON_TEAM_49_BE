"""Code-owned candidate validation, separate from the model response schema."""

import re
from typing import Literal

from pydantic import BaseModel, Field

from agent.providers.base import ModelField


class ReviewedField(ModelField):
    suggested_value: str | None = None
    validation_status: Literal["valid", "needs_review", "missing"] = "valid"
    validation_reasons: list[str] = Field(default_factory=list)
    evidence_valid: bool = False
    source_quote: str | None = None
    source_page: int | None = None

    @classmethod
    def from_model(cls, item):
        return cls(
            **item.model_dump(),
            suggested_value=item.normalized_value,
            source_quote=item.quote or None,
            source_page=item.page,
        )

    def quarantine(self, reason):
        self.validation_status = "needs_review"
        self.normalized_value = None
        if reason not in self.validation_reasons:
            self.validation_reasons.append(reason)


class ReviewedExtraction(BaseModel):
    document_type: str
    fields: list[ReviewedField]
    issues: list[str]


def candidate_range_valid(field_id, unit, value, document_type):
    """Match the typed values accepted by confirmation, without altering them."""
    if document_type == "contract" and len(value) > 100:
        return False
    if unit in ("minute", "count"):
        return re.fullmatch(r"\d{1,13}", value) is not None
    if unit and unit.startswith("KRW"):
        signed = unit == "KRW" and not (
            field_id == "deposit_total"
            or re.fullmatch(r"transaction_[1-9]\d*_(?:deposit|withdrawal)_amount", field_id)
        )
        return (
            re.fullmatch(r"-?\d{1,13}(?:\.\d{1,2})?" if signed else r"\d{1,13}(?:\.\d{1,2})?", value)
            is not None
        )
    return True
