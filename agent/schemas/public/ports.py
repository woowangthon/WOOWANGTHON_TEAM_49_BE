from typing import Any

from pydantic import Field

from agent.schemas.public.base import ContractModel


class SourceContent(ContractModel):
    data: bytes = Field(repr=False, exclude=True)
    mime_type: str
    document_version: int
    content_hash: str


class CheckpointEnvelope(ContractModel):
    job_id: str
    input_fingerprint: str
    engine_version: str
    checkpoint_schema_version: str = "1.0"
    completed_nodes: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict, repr=False)
    payload_hash: str = ""
    call_budget: dict[str, int] = Field(default_factory=dict)


class ProgressEvent(ContractModel):
    node_id: str
    status: str
