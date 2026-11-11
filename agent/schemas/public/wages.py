"""Explicit, user-confirmed inputs for the standard wage profiles."""

from datetime import date
from typing import Generic, Literal, TypeVar

from pydantic import Field, model_validator

from agent.schemas.public.base import ContractModel, Money, Period

T = TypeVar("T")
InputState = Literal["known", "missing", "unknown", "pending", "conflict"]
WageRuleId = Literal["W-01", "W-02", "W-03", "W-04", "W-05"]


class WageCondition(ContractModel, Generic[T]):
    state: InputState = "missing"
    value: T | None = None
    confirmation_id: str = ""
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def confirmed_value(self):
        if (self.state == "known") != (self.value is not None):
            raise ValueError("Known wage conditions require a value; other states require null")
        return self


class ScheduleDay(ContractModel):
    weekday: int = Field(ge=0, le=6, description="Monday=0")
    minutes: int = Field(ge=1, le=480)


class FixedSchedule(ContractModel):
    days: list[ScheduleDay]
    week_start: int = Field(ge=0, le=6)
    weekly_rest_weekday: int = Field(ge=0, le=6)
    weekly_rest_minutes: int = Field(ge=0, le=480)

    @model_validator(mode="after")
    def unique_days(self):
        if not self.days or len({d.weekday for d in self.days}) != len(self.days):
            raise ValueError("A fixed schedule requires unique working weekdays")
        if sum(d.minutes for d in self.days) > 2400:
            raise ValueError("Standard schedules cannot exceed 40 contractual hours")
        if self.weekly_rest_weekday in {d.weekday for d in self.days}:
            raise ValueError("Weekly rest day cannot be a contractual working weekday")
        return self


class WageDay(ContractModel):
    date: date
    kind: Literal["workday", "unpaid_rest", "paid_holiday", "weekly_rest"]
    attendance: Literal["present", "absent", "paid_leave", "not_scheduled", "unknown", "pending", "conflict"]
    confirmed: bool = False
    evidence_refs: list[str] = Field(default_factory=list)


class IncludedAllowance(ContractModel):
    rule_id: WageRuleId
    amount: Money
    state: InputState = "missing"
    evidence_refs: list[str] = Field(default_factory=list)


class WageStatementMapping(ContractModel):
    rule_ids: list[WageRuleId]
    statement_item_ids: list[str]
    period: Period
    state: InputState = "missing"
    evidence_refs: list[str] = Field(default_factory=list)


class WageConditions(ContractModel):
    """Missing facts block only the rules that need them; no inferred 209-hour divisor."""

    standard_employment: WageCondition[bool] = Field(default_factory=WageCondition[bool])
    contract_unchanged: WageCondition[bool] = Field(default_factory=WageCondition[bool])
    five_or_more_workers: WageCondition[bool] = Field(default_factory=WageCondition[bool])
    full_period_employment: WageCondition[bool] = Field(default_factory=WageCondition[bool])
    records_complete: WageCondition[bool] = Field(default_factory=WageCondition[bool])
    composition_complete: WageCondition[bool] = Field(default_factory=WageCondition[bool])
    monthly_weekly_rest_included: WageCondition[bool] = Field(default_factory=WageCondition[bool])
    ordinary_hourly_rate: WageCondition[Money] = Field(default_factory=WageCondition[Money])
    ordinary_monthly_amount: WageCondition[Money] = Field(default_factory=WageCondition[Money])
    monthly_standard_minutes: WageCondition[int] = Field(default_factory=WageCondition[int])
    schedule: WageCondition[FixedSchedule] = Field(default_factory=WageCondition[FixedSchedule])
    days: list[WageDay] = Field(default_factory=list)
    included_allowances: list[IncludedAllowance] = Field(default_factory=list)
    statement_mappings: list[WageStatementMapping] = Field(default_factory=list)
    calculation_period: Period | None = None

    @model_validator(mode="after")
    def unique_entries(self):
        if len({d.date for d in self.days}) != len(self.days):
            raise ValueError("Duplicate wage calendar dates")
        if len({a.rule_id for a in self.included_allowances}) != len(self.included_allowances):
            raise ValueError("Duplicate included allowance")
        if any(a.amount < 0 for a in self.included_allowances):
            raise ValueError("Included allowance cannot be negative")
        for name in ("ordinary_hourly_rate", "ordinary_monthly_amount", "monthly_standard_minutes"):
            condition = getattr(self, name)
            if condition.state == "known" and condition.value <= 0:
                raise ValueError("Confirmed rate and divisor must be positive")
        return self
