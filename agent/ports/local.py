"""File-backed ports for the single-user local workspace."""

import asyncio
import re
from pathlib import Path

from agent.schemas.public.errors import (
    AccessRevoked,
    ExecutionRevoked,
    IntegrityError,
    JobCancelled,
    OCRRequired,
)
from agent.schemas.public.ports import CheckpointEnvelope, SourceContent
from agent.schemas.public.results import ArtifactRef
from agent.services.hashing import content_hash, digest


class LocalFilePorts:
    def __init__(self, *, directory: Path | None = None):
        self.directory = directory
        self.current_tokens = {}
        self.jobs = {}
        self.sources = {}
        self.source_refs = {}
        self.revoked = set()
        self.cancelled = set()
        self.checkpoints = {}
        self.artifacts = {}
        self.events = []
        self.lock = asyncio.Lock()
        if directory:
            directory.mkdir(parents=True, exist_ok=True)

    def bind(self, context):
        token = context.execution_token.get_secret_value()
        self.current_tokens[context.job_id] = token
        self.jobs[context.job_id] = (context.owner_id, context.case_id)

    def add_source(self, ref, data):
        if content_hash(data) != ref.content_hash:
            raise ValueError("Source content hash mismatch")
        self.sources[ref.source_id] = data
        self.source_refs[ref.source_id] = ref.model_copy(deep=True)

    async def check_control(self, context):
        if context.job_id in self.cancelled:
            raise JobCancelled()
        if self.current_tokens.get(
            context.job_id
        ) != context.execution_token.get_secret_value() or self.jobs.get(context.job_id) != (
            context.owner_id,
            context.case_id,
        ):
            raise ExecutionRevoked()

    async def check_access(self, context, sources):
        await self.check_control(context)
        # Empty source lists still trigger result/job-level access revocation.
        if context.case_id in self.revoked:
            raise AccessRevoked()
        for source in sources:
            registered = self.source_refs.get(source.source_id)
            if (
                source.source_id in self.revoked
                or source.owner_id != context.owner_id
                or registered is None
                or registered != source
            ):
                raise AccessRevoked()

    async def read_source(self, context, source):
        await self.check_access(context, [source])
        data = self.sources[source.source_id]
        return SourceContent(
            data=data,
            mime_type="application/pdf",
            document_version=source.document_version,
            content_hash=content_hash(data),
        )

    async def read_ocr(self, context, source, ocr_result_id):
        await self.check_access(context, [source])
        raise OCRRequired("A completed OCR result must be provided by the document store")

    async def load_checkpoint(self, context, fingerprint):
        await self.check_control(context)
        checkpoint = self.checkpoints.get(context.job_id)
        path = self._path("checkpoint", context.job_id)
        if checkpoint is None and path and path.exists():
            checkpoint = CheckpointEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
        if checkpoint and checkpoint.input_fingerprint != fingerprint:
            raise IntegrityError("Same logical job cannot change its input or configuration")
        if checkpoint:
            self.checkpoints[context.job_id] = checkpoint.model_copy(deep=True)
        return checkpoint.model_copy(deep=True) if checkpoint else None

    async def save_checkpoint(self, context, checkpoint):
        async with self.lock:
            await self.check_control(context)
            if checkpoint.job_id != context.job_id:
                raise IntegrityError("Checkpoint job mismatch")
            previous = self.checkpoints.get(context.job_id)
            if previous and any(
                checkpoint.call_budget.get(k, 0) < v for k, v in previous.call_budget.items()
            ):
                raise IntegrityError("Call budget cannot decrease")
            path = self._path("checkpoint", context.job_id)
            if path:
                temp = path.with_suffix(".tmp")
                temp.write_text(checkpoint.model_dump_json(), encoding="utf-8")
                temp.replace(path)
            self.checkpoints[context.job_id] = checkpoint.model_copy(deep=True)

    async def stage_artifact(self, context, data, mime_type):
        async with self.lock:
            await self.check_control(context)
            await self.check_access(context, [])
            artifact_id = digest([context.job_id, content_hash(data), mime_type])
            self.artifacts[artifact_id] = data
            if self.directory:
                suffix = ".pdf" if mime_type == "application/pdf" else ".zip"
                (self.directory / (artifact_id + suffix)).write_bytes(data)
            return ArtifactRef(
                artifact_id=artifact_id,
                content_hash=content_hash(data),
                mime_type=mime_type,
                size_bytes=len(data),
            )

    async def emit_progress(self, context, event):
        await self.check_control(context)
        self.events.append(event.model_dump())

    def _path(self, kind, job_id):
        return self.directory / f"{kind}-{digest(job_id)}.json" if self.directory else None

    def artifact_bytes(self, ref):
        """Development helper for recovering bytes referenced by cached report results."""
        if not re.fullmatch(r"[0-9a-f]{64}", ref.artifact_id):
            raise IntegrityError("Invalid development artifact ID")
        data = self.artifacts.get(ref.artifact_id)
        if data is None and self.directory:
            suffix = ".pdf" if ref.mime_type == "application/pdf" else ".zip"
            data = (self.directory / (ref.artifact_id + suffix)).read_bytes()
        if data is None or content_hash(data) != ref.content_hash or len(data) != ref.size_bytes:
            raise IntegrityError("Development artifact missing or corrupt")
        return data
