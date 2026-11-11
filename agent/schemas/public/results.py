from typing import Literal

from pydantic import Field

from agent.schemas.public.base import AwareDateTime, ContractModel, Money, Period
from agent.schemas.public.inputs import EvidenceRef


class WageComparisonItem(ContractModel):
    rule_ids: list[str]
    statement_item_ids: list[str]
    status: Literal["match", "difference", "insufficient", "needs_review"]
    lhs: Money | None = None
    rhs: Money | None = None
    delta: Money | None = None
    reason_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    independent: bool = True


class ComparisonResult(ContractModel):
    rule_id: str
    status: Literal["match", "difference", "insufficient", "needs_review"]
    lhs: Money | None = None
    rhs: Money | None = None
    unit: str = "KRW"
    delta: Money | None = None
    formula_id: str
    scope_complete: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    wage_items: list[WageComparisonItem] = Field(default_factory=list)


class WageItemResult(ContractModel):
    rule_id: str
    rule_version: str | None = None
    status: Literal["calculated", "not_applicable", "insufficient", "needs_review"]
    amount: Money | None = None
    formula_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    operands: dict[str, str] = Field(default_factory=dict)
    included_amount: Money | None = None
    additional_amount: Money | None = None
    payable_amount: Money | None = None
    required_field_ids: list[str] = Field(default_factory=list)
    required_periods: list[Period] = Field(default_factory=list)
    official_sources: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class WageSection(ContractModel):
    items: list[WageItemResult]
    subtotal: Money
    gross_total: Money | None = None
    scope_complete: bool = False


class Question(ContractModel):
    question_id: str
    reason_code: str
    target_field_ids: list[str]
    blocking_rule_ids: list[str]
    text: str
    required_materials: list[str] = Field(default_factory=list)
    reason: str = ""
    required_periods: list[Period] = Field(default_factory=list)
    glossary_ids: list[str] = Field(default_factory=list)


class AnalysisResultCandidate(ContractModel):
    owner_id: str
    case_id: str
    workplace_id: str
    input_version: int
    snapshot_id: str
    snapshot_hash: str
    context_id: str
    context_hash: str
    rule_bundle_version: str
    support_manifest_version: str
    engine_version: str
    wage_section: WageSection
    comparisons: list[ComparisonResult]
    questions: list[Question]
    evidence_catalog: list[EvidenceRef]
    dependencies: list[str]
    result_hash: str = ""


class AnalysisResult(AnalysisResultCandidate):
    result_id: str
    result_version: int = Field(ge=1)
    freshness: Literal["current", "stale"]
    created_at: AwareDateTime


class ExtractedField(ContractModel):
    field_id: str
    raw_value: str | None
    normalized_value: str | None
    suggested_value: str | None = None
    validation_status: Literal["valid", "needs_review", "missing"] = "valid"
    validation_reasons: list[str] = Field(default_factory=list)
    source_quote: str | None = None
    source_page: int | None = None
    unit: str | None
    evidence_refs: list[str]
    issues: list[str] = Field(default_factory=list)


class ExtractionCandidate(ContractModel):
    document_id: str
    document_version: int
    document_type: str
    extraction_version: str
    model_config_version: str
    fields: list[ExtractedField]
    evidence_catalog: list[EvidenceRef]
    issues: list[str]
    page_count: int
    verification_method: Literal["text_match", "vision_unverified", "ocr_unverified"]
    recognized_pages: list[str] = Field(default_factory=list)
    source_method: Literal["pdf_text", "python_ocr", "mixed", "pdf_supplement"] = "python_ocr"
    page_methods: list[Literal["pdf_text", "python_ocr", "pdf_supplement"]] = Field(default_factory=list)


class ExplanationCandidate(ContractModel):
    result_id: str
    result_version: int
    result_hash: str
    status: Literal["generated", "template_fallback"]
    paragraphs: list[str]
    question_ids: list[str]
    model_config_version: str
    prompt_version: str


class ArtifactRef(ContractModel):
    artifact_id: str
    content_hash: str
    mime_type: str
    size_bytes: int = Field(ge=0)


class ReportCandidate(ContractModel):
    result_id: str
    result_version: int
    explanation_version: str
    artifacts: list[ArtifactRef]
    included_source_ids: list[str]
