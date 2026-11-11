from typing import Literal

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator

from agent.config import ENGINE_VERSION, SCHEMA_VERSION
from agent.schemas.public.base import AwareDateTime, ContractModel, Period
from agent.schemas.public.inputs import InputSnapshot, SourceRef
from agent.schemas.public.results import (
    AnalysisResult,
    AnalysisResultCandidate,
    ExplanationCandidate,
    ExtractionCandidate,
    ReportCandidate,
)

TaskKind = Literal["extract_document", "analyze_case", "explain_result", "build_report"]


class JobContext(ContractModel):
    model_config = ConfigDict(json_schema_extra={
        "allOf": [{
            "if": {"properties": {"case_id": {"type": "null"}}},
            "then": {"properties": {"schema_version": {"const": "1.1"},
                                     "task_kind": {"const": "extract_document"}},
                     "required": ["schema_version"]},
            "else": {"properties": {"case_id": {"type": "string", "minLength": 1}}},
        }],
    })
    job_id: str
    run_id: str
    execution_token: SecretStr
    owner_id: str
    case_id: str | None
    task_kind: TaskKind
    schema_version: str = "1.0"
    idempotency_key: str
    requested_at: AwareDateTime

    @model_validator(mode="after")
    def document_scope(self):
        # 1.1 supports workplace-library extraction before a payroll case exists.
        # Old 1.0 clients retain their case-scoped contract; never invent a case ID.
        if self.case_id is None and (self.task_kind != "extract_document" or self.schema_version != "1.1"):
            raise ValueError("Only schema 1.1 document extraction may omit case_id")
        if self.case_id is not None and not self.case_id.strip():
            raise ValueError("case_id must be an actual identifier or null")
        return self


class SupplementTarget(ContractModel):
    field_id: str = Field(min_length=1, max_length=100)
    page: int | None = Field(default=None, ge=1)
    source_quote: str | None = None
    row_context: dict[str, str | None] = Field(default_factory=dict)


class ExtractionInput(ContractModel):
    source: SourceRef
    ocr_result_id: str | None = None
    document_type: Literal["auto", "contract", "attendance", "payslip", "deposit", "payslip_ocr"]
    extraction_version: str = "1.2"
    prompt_version: str = "extract-v1"
    model_config_version: str = "luna-v1"
    extraction_method: Literal["text", "pdf_supplement"] = "text"
    target_fields: list[SupplementTarget] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def supplement_scope(self):
        ids = [target.field_id for target in self.target_fields]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate supplement target")
        if self.extraction_method == "pdf_supplement":
            if not ids or self.document_type == "auto":
                raise ValueError("PDF supplement requires an established type and selected fields")
        elif ids:
            raise ValueError("Text extraction cannot include supplement targets")
        return self


class AnalysisJobInput(ContractModel):
    snapshot: InputSnapshot
    snapshot_id: str
    snapshot_hash: str
    rule_bundle_version: str = "pending-v1"
    support_manifest_version: str = "pending-v1"
    engine_version: str = ENGINE_VERSION


class ExplanationInput(ContractModel):
    result: AnalysisResult
    allowed_facts: dict[str, str]
    prompt_version: str = "explain-v1"
    model_config_version: str = "luna-v1"
    generation_mode: Literal["template", "model"] = "template"


class ReportCondition(ContractModel):
    model_config = ConfigDict(frozen=True)

    label: str
    value: str
    state: Literal["known", "missing", "unknown", "pending", "conflict"] = "known"
    evidence_refs: tuple[str, ...] = ()


class ReportSource(ContractModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    filename: str


class ReportPeriod(Period):
    model_config = ConfigDict(frozen=True)


class ReportDisplayContext(ContractModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    snapshot_hash: str
    workplace_name: str
    period: ReportPeriod
    conditions: tuple[ReportCondition, ...] = ()
    sources: tuple[ReportSource, ...] = ()

    @field_validator("period", mode="before")
    @classmethod
    def copy_period(cls, value):
        return value.model_dump() if isinstance(value, Period) else value


class ReportInput(ContractModel):
    result: AnalysisResult
    explanation: ExplanationCandidate
    explanation_version: str
    selected_sources: list[SourceRef] = Field(default_factory=list)
    generated_at: AwareDateTime
    template_version: str = "report-v1"
    display_context: ReportDisplayContext | None = None


Payload = ExtractionInput | AnalysisJobInput | ExplanationInput | ReportInput


class AgentTask(ContractModel):
    context: JobContext
    payload: Payload

    @model_validator(mode="after")
    def matching_payload(self):
        types = {
            "extract_document": ExtractionInput,
            "analyze_case": AnalysisJobInput,
            "explain_result": ExplanationInput,
            "build_report": ReportInput,
        }
        if not isinstance(self.payload, types[self.context.task_kind]):
            raise ValueError("task_kind does not match payload")
        return self


class AgentFailure(ContractModel):
    code: str
    message: str
    retryable: bool = False


class AgentOutcome(ContractModel):
    job_id: str
    run_id: str
    schema_version: str = SCHEMA_VERSION
    provider_mode: Literal["openai"]
    status: Literal["completed", "failed", "cancelled"]
    payload: AnalysisResultCandidate | ExtractionCandidate | ExplanationCandidate | ReportCandidate | None = (
        None
    )
    error: AgentFailure | None = None
    usage: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent_status(self):
        if self.status == "completed":
            if self.payload is None or self.error is not None:
                raise ValueError("Completed outcome requires only a success payload")
        elif self.payload is not None or self.error is None:
            raise ValueError("Failed/cancelled outcomes require only an error")
        return self
