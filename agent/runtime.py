import asyncio
import inspect
from copy import deepcopy
from datetime import UTC, datetime

from agent.config import CHECKPOINT_VERSION, ENGINE_VERSION
from agent.schemas.public.errors import BudgetExceeded, IntegrityError, ModelFormatError, ModelTransientError
from agent.schemas.public.ports import CheckpointEnvelope, ProgressEvent
from agent.services.hashing import digest


class Runtime:
    def __init__(self, task, ports, settings, provider):
        self.task, self.context = task, task.context
        self.ports, self.settings, self.provider = ports, settings, provider
        self.lock = asyncio.Lock()
        self.usage = {}
        self.fingerprint = digest(
            {
                "kind": task.context.task_kind,
                "payload": task.payload,
                "owner": task.context.owner_id,
                "case": task.context.case_id,
                "schema": task.context.schema_version,
                "model_config": settings.model_config_version,
                "provider_mode": settings.provider_mode,
                "models": [settings.extraction_model, settings.explanation_model],
                **(
                    {
                        "ocr": [
                            settings.ocr_command,
                            str(settings.ocr_data_path),
                            settings.ocr_dpi,
                            settings.ocr_page_timeout,
                        ]
                    }
                    if task.context.task_kind == "extract_document"
                    else {}
                ),
                "limits": [
                    settings.max_file_bytes,
                    settings.max_pages,
                    settings.max_stage_calls,
                    settings.max_output_tokens,
                ],
            }
        )
        self.checkpoint = CheckpointEnvelope(
            job_id=self.context.job_id, input_fingerprint=self.fingerprint, engine_version=ENGINE_VERSION
        )
        self.sources = []

    async def control(self):
        await self.ports.check_control(self.context)
        await self.ports.check_access(self.context, self.sources)

    async def start(self, sources):
        self.sources = sources
        await self.control()
        saved = await self.ports.load_checkpoint(self.context, self.fingerprint)
        if saved:
            if saved.job_id != self.context.job_id or saved.input_fingerprint != self.fingerprint:
                raise IntegrityError("Checkpoint belongs to different input")
            if saved.payload_hash != digest(saved.payload):
                raise IntegrityError("Checkpoint hash mismatch")
            if (
                saved.engine_version != ENGINE_VERSION
                or saved.checkpoint_schema_version != CHECKPOINT_VERSION
            ):
                raise IntegrityError("Checkpoint version requires a new compatible execution")
            self.checkpoint = saved.model_copy(deep=True)
            self.usage = dict(self.checkpoint.payload.get("__usage__", {}))

    async def persist(self):
        self.checkpoint.payload_hash = digest(self.checkpoint.payload)
        await self.control()
        await self.ports.save_checkpoint(self.context, self.checkpoint.model_copy(deep=True))

    async def step(self, name, function):
        await self.control()
        async with self.lock:
            if name in self.checkpoint.completed_nodes:
                return deepcopy(self.checkpoint.payload[name])
        try:
            await self.ports.emit_progress(self.context, ProgressEvent(node_id=name, status="running"))
        except Exception:
            # Progress is best effort; the next mandatory control check is not.
            pass
        started_at = datetime.now(UTC).isoformat()
        value = function()
        if inspect.isawaitable(value):
            value = await value
        await self.control()
        async with self.lock:
            self.checkpoint.payload[name] = deepcopy(value)
            self.checkpoint.payload.get("__pending_model_outputs__", {}).pop(name, None)
            self.checkpoint.completed_nodes.append(name)
            self.checkpoint.payload.setdefault("__trace__", []).append(
                {
                    "node_id": name,
                    "started_at": started_at,
                    "ended_at": datetime.now(UTC).isoformat(),
                    "status": "completed",
                    "input_hash": self.fingerprint,
                    "output_hash": digest(value),
                    "model_config_version": self.settings.model_config_version,
                    "provider_mode": self.settings.provider_mode,
                }
            )
            await self.persist()
        return value

    async def call_model(self, stage, operation, data, validator=None):
        pending = self.checkpoint.payload.get("__pending_model_outputs__", {}).get(stage)
        if pending:
            await self.control()
            if pending["operation"] != operation:
                raise IntegrityError("Stored model output belongs to a different operation")
            response = deepcopy(pending["response"])
            try:
                if validator:
                    validator(response)
            except ModelFormatError:
                pass
            else:
                # Revalidate the already paid response after a code fix, without resetting any budget.
                return response
        format_failures = self.checkpoint.call_budget.get(stage + ":format_failures", 0)
        if format_failures > 1:
            raise ModelFormatError("Format repair allowance already exhausted")
        request_data = dict(data)
        feedback = self.checkpoint.payload.get("__model_validation__", {}).get(stage)
        if feedback:
            request_data["validation_feedback"] = feedback
        while True:
            async with self.lock:
                count = self.checkpoint.call_budget.get(stage, 0)
                if count >= self.settings.max_stage_calls:
                    raise BudgetExceeded("Per-stage model call budget exhausted")
                self.checkpoint.call_budget[stage] = count + 1
                # Durably reserve before the network request, including unknown-outcome calls.
                await self.persist()
            await self.control()
            try:
                response, usage = await asyncio.wait_for(
                    self.provider.invoke(operation, request_data), timeout=self.settings.timeout_seconds
                )
                for key, value in usage.items():
                    self.usage[key] = self.usage.get(key, 0) + value
                async with self.lock:
                    self.checkpoint.payload["__usage__"] = dict(self.usage)
                    # Private checkpoint only: retain schema-parsed output to diagnose or revalidate it.
                    self.checkpoint.payload.setdefault("__pending_model_outputs__", {})[stage] = {
                        "operation": operation,
                        "response": deepcopy(response),
                    }
                    await self.persist()
                if validator:
                    validator(response)
                return response
            except ModelTransientError, TimeoutError:
                if count + 1 >= self.settings.max_stage_calls:
                    raise
                await asyncio.sleep(self.settings.retry_delay_seconds * 2**count)
            except ModelFormatError as exc:
                format_failures += 1
                feedback = {"reason": exc.reason, "field_id": exc.field_id}
                request_data["validation_feedback"] = feedback
                async with self.lock:
                    self.checkpoint.call_budget[stage + ":format_failures"] = format_failures
                    self.checkpoint.payload.setdefault("__model_validation__", {})[stage] = feedback
                    await self.persist()
                if format_failures > 1:
                    raise
                if exc.reason in {"DOCUMENT_TYPE_MISMATCH", "DOCUMENT_TYPE_UNRESOLVED"}:
                    raise
