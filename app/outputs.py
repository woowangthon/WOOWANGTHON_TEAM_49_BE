"""Public core response schemas for Swagger and frontend type generation."""
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, JsonValue

T = TypeVar('T')


class API(BaseModel, Generic[T]):
    code: int
    message: str
    data: T


class FieldError(BaseModel):
    field: str
    reason: str


class ErrorData(BaseModel):
    errorCode: str
    details: JsonValue = None
    errors: list[FieldError] | None = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    size: int
    total: int


class UserDTO(BaseModel):
    userId: int
    email: str
    name: str


class LoginDTO(BaseModel):
    accessToken: str
    tokenType: Literal['Bearer']
    expiresIn: int
    user: UserDTO


class WorkplaceDTO(BaseModel):
    workplaceId: int
    name: str
    industry: str | None
    region: str | None
    employmentStatus: Literal['CURRENT', 'FORMER']
    caseCount: int
    createdAt: str
    updatedAt: str


class CaseDTO(BaseModel):
    caseId: int
    workplaceId: int
    workplace: dict[str, JsonValue]
    payType: Literal['HOURLY', 'MONTHLY']
    periodStart: str
    periodEnd: str
    scheduledPayDate: str | None
    status: Literal['CREATED', 'IN_PROGRESS', 'COMPLETED', 'ARCHIVED']
    currentStep: Literal['UPLOAD', 'REVIEW', 'CONDITIONS', 'COMPARE', 'RESULT']
    documentCount: int
    latestCalculationId: str | None
    inputVersion: int
    attentionRequired: bool
    attentionCount: int
    updatedAt: str


class DocumentDTO(BaseModel):
    documentId: int
    workplaceId: int
    documentType: Literal['CONTRACT', 'ATTENDANCE', 'PAYSLIP', 'DEPOSIT']
    fileName: str
    processingStatus: Literal['UPLOADED', 'PROCESSING', 'READ_READY', 'READY', 'REVIEW_REQUIRED', 'FAILED']
    reviewStatus: Literal['NOT_STARTED', 'IN_PROGRESS', 'CONFIRMED']
    progress: int
    linkId: int | None
    linkedCaseIds: list[int]
    uploadedAt: str
    errorCode: str | None
    aiMetadata: dict[str, JsonValue] | None
    workplaceName: str | None = None


class FieldDTO(BaseModel):
    fieldId: int
    key: str
    label: str
    value: JsonValue
    valueType: str
    status: Literal['UNCONFIRMED', 'CONFIRMED', 'MISSING', 'UNREADABLE', 'CONFLICTED', 'DEFERRED', 'NEEDS_RECONFIRMATION']
    editedByUser: bool
    effectiveValueOrigin: Literal['SHARED_DEFAULT', 'CASE_OVERRIDE']
    hasCaseOverride: bool
    confirmedAt: str | None
    source: dict[str, JsonValue] | None


class FieldsDTO(BaseModel):
    documentId: int
    caseId: int
    processingStatus: str
    reviewStatus: str
    fields: list[FieldDTO]


class VersionRef(BaseModel):
    snapshotId: int | None
    version: int | None
    inputVersion: int
    createdAt: str | None
    freshness: Literal['CURRENT', 'STALE', 'UPDATING', 'FAILED']


class ResultItem(BaseModel):
    id: str
    title: str
    status: Literal['MATCH', 'DIFFERENCE', 'INSUFFICIENT_INFORMATION', 'REVIEW_REQUIRED']
    calculatedValue: int | None
    documentValue: int | None
    difference: int | None
    unit: str | None
    periodStart: str
    periodEnd: str
    formula: dict[str, JsonValue] | None
    rule: dict[str, JsonValue] | None
    comparisonBasis: dict[str, JsonValue] | None
    sources: list[dict[str, JsonValue]]
    confirmedAt: str | None
    nextAction: str | None


class ResultsDTO(BaseModel):
    caseId: int
    currentInputVersion: int
    resultVersion: int
    summary: dict[str, JsonValue]
    freshness: dict[str, JsonValue]
    versions: dict[str, VersionRef]
    items: list[ResultItem]
    calculation: dict[str, JsonValue] | None
    comparison: dict[str, JsonValue] | None
    payslipValidation: dict[str, JsonValue] | None
    depositComparison: dict[str, JsonValue] | None


class RecomputeDTO(BaseModel):
    caseId: int
    currentInputVersion: int
    activeJob: dict[str, JsonValue] | None
    latestJob: dict[str, JsonValue] | None
    results: dict[str, VersionRef]
