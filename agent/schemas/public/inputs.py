from typing import Literal

from pydantic import Field, model_validator

from agent.schemas.public.base import AwareDateTime, ContractModel, Money, Period
from agent.schemas.public.wages import WageConditions


class SourceRef(ContractModel):
    source_id: str
    owner_id: str
    workplace_id: str
    link_id: str
    link_version: int = Field(ge=1)
    document_id: str
    document_version: int = Field(ge=1)
    content_hash: str
    usage: Literal["payroll", "context", "both"] = "payroll"
    status: Literal["active", "pending"] = "active"
    applicable_period: Period | None = None
    kind: Literal["document", "manual"] = "document"


class EvidenceRef(ContractModel):
    evidence_id: str
    source_id: str
    page: int | None = Field(default=None, ge=1)
    quote: str = ""
    locator: str = ""
    verification_method: Literal["text_match", "vision_unverified", "ocr_unverified", "manual_confirmed"]


class ConfirmedField(ContractModel):
    field_id: str
    value: Money | None = None
    unit: str
    confirmation_id: str
    confirmation_version: int = Field(default=1, ge=1)
    evidence_refs: list[str] = Field(default_factory=list)
    state: Literal["known", "missing", "unknown", "pending", "conflict"] = "known"
    period: Period | None = None
    time_kind: Literal["actual", "paid"] | None = None

    @model_validator(mode="after")
    def state_value(self):
        if self.state != "known" and self.value is not None:
            raise ValueError("Unconfirmed states must have null values")
        if self.state == "known" and self.value is None:
            raise ValueError("Known field requires a value")
        if self.unit == "minute" and self.value is not None and self.value != self.value.to_integral_value():
            raise ValueError("Minutes must be integral")
        return self


class StatementItem(ContractModel):
    item_id: str
    amount: Money | None
    evidence_refs: list[str]
    confirmed: bool = True
    is_total: bool = False


class TransactionAllocation(ContractModel):
    transaction_id: str
    amount: Money
    transaction_amount: Money
    evidence_refs: list[str]
    confirmed: bool = True
    linked: bool = True
    duplicate: bool = False
    kind: Literal["bank", "cash_memo"] = "bank"


class WorkRecord(ContractModel):
    record_id: str
    start_at: AwareDateTime
    end_at: AwareDateTime
    evidence_refs: list[str]
    breaks: list[tuple[AwareDateTime, AwareDateTime]] = Field(default_factory=list)
    break_location_known: bool = True


class ConditionState(ContractModel):
    field_id: str
    state: Literal["known", "missing", "unknown", "pending", "conflict"]
    confirmed_field_ref: str | None = None
    conflicting_source_refs: list[str] = Field(default_factory=list)


class InputSnapshot(ContractModel):
    snapshot_id: str
    input_version: int = Field(ge=1)
    snapshot_hash: str = ""
    owner_id: str
    workplace_id: str
    case_id: str
    period: Period
    timezone: str = "Asia/Seoul"
    profile: Literal["H-STD", "M-STD"] = "H-STD"
    confirmed_fields: list[ConfirmedField] = Field(default_factory=list)
    condition_states: list[ConditionState] = Field(default_factory=list)
    authorized_sources: list[SourceRef] = Field(default_factory=list)
    evidence_catalog: list[EvidenceRef] = Field(default_factory=list)
    earnings: list[StatementItem] = Field(default_factory=list)
    deductions: list[StatementItem] = Field(default_factory=list)
    earnings_complete: bool = False
    deductions_complete: bool = False
    transaction_allocations: list[TransactionAllocation] = Field(default_factory=list)
    payment_scope_confirmed: bool = False
    work_records: list[WorkRecord] = Field(default_factory=list)
    coverage: list[Period] = Field(default_factory=list)
    context_requirements: list[Period] = Field(default_factory=list)
    rule_bundle_version: str = "pending-v1"
    support_manifest_version: str = "pending-v1"
    wage_conditions: WageConditions | None = None
