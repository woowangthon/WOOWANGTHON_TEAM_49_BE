from typing import Any

from pydantic import Field

from agent.schemas.public.base import ContractModel
from agent.schemas.public.inputs import InputSnapshot


class CalculationContext(ContractModel):
    snapshot_hash: str
    context_hash: str
    normalizer_version: str = "normalizer-v1"
    snapshot: InputSnapshot
    normalized_intervals: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
