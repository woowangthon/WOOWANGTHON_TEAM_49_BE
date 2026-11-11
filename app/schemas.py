from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

Money = Annotated[int, Field(strict=True, ge=0, le=10**12)]
DocType = Literal['CONTRACT', 'ATTENDANCE', 'PAYSLIP', 'DEPOSIT']


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Signup(Input):
    email: str = Field(min_length=3, max_length=254, pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=100)


class Login(Input):
    email: str
    password: str


class ForgotPassword(Input):
    email: str = Field(min_length=3, max_length=254, pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


class ResetPassword(Input):
    token: str = Field(min_length=20, max_length=200)
    newPassword: str = Field(min_length=8, max_length=128)


class WorkplaceCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    industry: str | None = Field(default=None, max_length=200)
    region: str | None = Field(default=None, max_length=200)
    employmentStatus: Literal['CURRENT', 'FORMER'] = 'CURRENT'


class WorkplacePatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    industry: str | None = Field(default=None, max_length=200)
    region: str | None = Field(default=None, max_length=200)
    employmentStatus: Literal['CURRENT', 'FORMER'] | None = None


class Versioned(Input):
    expectedInputVersion: int | None = Field(default=None, ge=1)


class CaseCreate(Input):
    workplaceId: int = Field(gt=0)
    payType: Literal['HOURLY', 'MONTHLY']
    periodStart: date
    periodEnd: date
    scheduledPayDate: date | None = None

    @model_validator(mode='after')
    def period_order(self):
        if self.periodStart > self.periodEnd:
            raise ValueError('periodStart must be on or before periodEnd')
        return self


class CasePatch(Versioned):
    payType: Literal['HOURLY', 'MONTHLY'] | None = None
    periodStart: date | None = None
    periodEnd: date | None = None
    scheduledPayDate: date | None = None


class Workflow(Input):
    status: Literal['CREATED', 'IN_PROGRESS', 'COMPLETED', 'ARCHIVED'] | None = None
    currentStep: Literal['UPLOAD', 'REVIEW', 'CONDITIONS', 'COMPARE', 'RESULT'] | None = None


class LinkCreate(Versioned):
    documentId: int = Field(gt=0)
    purpose: Literal['payroll', 'context', 'both'] | None = None
    applicableFrom: date | None = None
    applicableTo: date | None = None

    @field_validator('purpose', mode='before')
    @classmethod
    def normalize_purpose(cls, value):
        return value.lower() if isinstance(value, str) else value


class LinkPatch(Versioned):
    purpose: Literal['payroll', 'context', 'both'] | None = None
    applicableFrom: date | None = None
    applicableTo: date | None = None

    @field_validator('purpose', mode='before')
    @classmethod
    def normalize_purpose(cls, value):
        return value.lower() if isinstance(value, str) else value


class FieldPatch(Versioned):
    value: JsonValue = None
    confirmed: bool | None = None

    @model_validator(mode='after')
    def has_change(self):
        if not {'value', 'confirmed'} & self.model_fields_set:
            raise ValueError('value or confirmed is required')
        return self


class Answer(Input):
    conditionId: str = Field(min_length=1, max_length=100)
    value: JsonValue


class ConditionsPut(Versioned):
    answers: list[Answer] = Field(max_length=30)


class Execute(Versioned):
    confirmed: bool = True


class PayslipData(Input):
    grossPay: Money | None = None
    totalDeductions: Money | None = None
    netPay: Money | None = None
    paymentItems: list[Money] | None = Field(default=None, max_length=100)
    deductionItems: list[Money] | None = Field(default=None, max_length=100)
    hourlyWage: Money | None = None
    basePay: Money | None = None
    workedMinutes: Annotated[int, Field(strict=True, ge=0, le=1000000)] | None = None


class ContractData(Input):
    hourlyWage: Money | None = None
    monthlyBasePay: Money | None = None


class AttendanceData(Input):
    workDate: date
    clockIn: datetime
    clockOut: datetime
    breakMinutes: int = Field(ge=0, le=1440, strict=True)

    @model_validator(mode='after')
    def valid_times(self):
        if self.clockIn.utcoffset() is None or self.clockOut.utcoffset() is None:
            raise ValueError('clockIn/clockOut require timezone offsets')
        if any(t.second or t.microsecond for t in (self.clockIn, self.clockOut)):
            raise ValueError('clockIn/clockOut require whole-minute precision')
        duration = (self.clockOut - self.clockIn).total_seconds() / 60
        if duration <= 0 or duration > 1440 or self.breakMinutes > duration:
            raise ValueError('invalid work interval or breakMinutes')
        if self.clockIn.date() != self.workDate:
            raise ValueError('workDate must match clockIn local date')
        return self


class TransactionCreate(Input):
    paymentMethod: Literal['BANK_TRANSFER', 'CASH']
    transactionDate: date
    amount: Money
    sender: str | None = Field(default=None, max_length=200)
    memo: str | None = Field(default=None, max_length=1000)
    source: Literal['USER_INPUT', 'DOCUMENT'] = 'USER_INPUT'
    confirmationStatus: Literal['USER_REPORTED', 'USER_CONFIRMED'] = 'USER_REPORTED'
    sourceDocumentId: int | None = Field(default=None, gt=0)
    sourceCandidateId: str | None = Field(default=None, min_length=1, max_length=100)
    sourceRowId: str | None = Field(default=None, min_length=1, max_length=100)
    bankReference: str | None = Field(default=None, min_length=1, max_length=200)


class TransactionPatch(Input):
    paymentMethod: Literal['BANK_TRANSFER', 'CASH'] | None = None
    transactionDate: date | None = None
    amount: Money | None = None
    sender: str | None = Field(default=None, max_length=200)
    memo: str | None = Field(default=None, max_length=1000)
    confirmationStatus: Literal['USER_REPORTED', 'USER_CONFIRMED'] | None = None
    bankReference: str | None = Field(default=None, min_length=1, max_length=200)


class ManualCreate(Versioned):
    recordType: DocType
    data: dict
    source: Literal['USER_INPUT'] = 'USER_INPUT'

    @model_validator(mode='after')
    def typed_data(self):
        cls = {'PAYSLIP': PayslipData, 'CONTRACT': ContractData,
               'ATTENDANCE': AttendanceData, 'DEPOSIT': TransactionCreate}[self.recordType]
        self.data = cls.model_validate(self.data).model_dump(mode='json', exclude_none=True)
        if not self.data:
            raise ValueError('data must contain at least one value')
        return self


class AllocationCreate(Versioned):
    transactionId: int = Field(gt=0)
    allocatedAmount: Money


class AllocationPatch(Versioned):
    allocatedAmount: Money


class Scope(Input):
    scopeType: Literal['CASE', 'WORKPLACE_ALL']
    caseId: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def valid_scope(self):
        if (self.scopeType == 'CASE') != (self.caseId is not None):
            raise ValueError('caseId is required only for CASE scope')
        return self


class ChatCreate(Input):
    title: str = Field(default='자료 질문', min_length=1, max_length=200)
    defaultScope: Scope

    @model_validator(mode='after')
    def case_default(self):
        if self.defaultScope.scopeType != 'CASE':
            raise ValueError('new chats require CASE defaultScope')
        return self


class ChatPatch(Input):
    title: str = Field(min_length=1, max_length=200)


class MessageCreate(Input):
    content: str = Field(min_length=1, max_length=10000)
    scope: Scope | None = None


class Inquiry(Input):
    resultItemIds: list[str] = Field(default_factory=list, max_length=50)
    tone: Literal['POLITE'] = 'POLITE'
    basedOnResultVersions: dict[str, int] | None = None


class DeleteAccount(Input):
    confirmation: Literal['DELETE_ACCOUNT']


class Envelope(BaseModel):
    code: int
    message: str
    data: JsonValue
