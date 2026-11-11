from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ModelField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_id: str
    raw_value: str | None
    normalized_value: str | None
    unit: str | None
    page: int
    quote: str
    issues: list[str]


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_type: str
    fields: list[ModelField]
    issues: list[str]


class TableRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int
    quote: str
    raw_values: list[str | None]
    normalized_values: list[str | None]
    issues: list[str]


class ModelTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str
    columns: list[str]
    header_quote: str
    rows: list[TableRow]


class TableExtractionResponse(ExtractionResponse):
    tables: list[ModelTable]


class ExplanationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    templates: list[str]
    question_ids: list[str]


class ModelProvider(Protocol):
    async def invoke(self, operation: str, data: dict) -> tuple[dict, dict[str, int]]: ...
