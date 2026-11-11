from typing import Any, Protocol

from agent.schemas.public.inputs import SourceRef
from agent.schemas.public.ports import CheckpointEnvelope, ProgressEvent, SourceContent
from agent.schemas.public.results import ArtifactRef
from agent.schemas.public.tasks import JobContext


class AgentPorts(Protocol):
    async def check_control(self, context: JobContext) -> None: ...
    async def check_access(self, context: JobContext, sources: list[SourceRef]) -> None: ...
    async def read_source(self, context: JobContext, source: SourceRef) -> SourceContent: ...
    async def read_ocr(
        self, context: JobContext, source: SourceRef, ocr_result_id: str
    ) -> dict[str, Any]: ...
    async def load_checkpoint(self, context: JobContext, fingerprint: str) -> CheckpointEnvelope | None: ...
    async def save_checkpoint(self, context: JobContext, checkpoint: CheckpointEnvelope) -> None: ...
    async def stage_artifact(self, context: JobContext, data: bytes, mime_type: str) -> ArtifactRef: ...
    async def emit_progress(self, context: JobContext, event: ProgressEvent) -> None: ...


class OriginalSourcePorts(Protocol):
    """Optional report capability for uploads with a normalized PDF derivative.

    Implementations authorize the uploaded original against the exact SourceRef
    and verify its stored original hash, document version and derivative link.
    read_source remains the processing PDF; read_original preserves upload bytes.
    """

    async def read_original(self, context: JobContext, source: SourceRef) -> SourceContent: ...
