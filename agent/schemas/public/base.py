from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict


def validate_decimal(value):
    if not isinstance(value, (str, Decimal)):
        raise ValueError("Amounts must be decimal strings, not JSON numbers")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal string") from exc
    if not parsed.is_finite():
        raise ValueError("Non-finite amount")
    return parsed


Money = Annotated[Decimal, BeforeValidator(validate_decimal, json_schema_input_type=str)]


def aware(value: datetime):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone required")
    return value


AwareDateTime = Annotated[datetime, AfterValidator(aware)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Period(ContractModel):
    start: date
    end: date

    def model_post_init(self, context):
        if self.end < self.start:
            raise ValueError("Period end precedes start")
