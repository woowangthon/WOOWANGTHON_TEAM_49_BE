"""Backend execution orchestration using Python calls, never an agent HTTP API."""

import asyncio
import io
from datetime import datetime, timezone

from agent.config import SCHEMA_VERSION
from agent.runner import run_task
from agent.schemas.public.errors import AgentError, ExecutionRevoked, IntegrityError, JobCancelled
from agent.schemas.public.inputs import InputSnapshot, SourceRef
from agent.schemas.public.results import ExplanationCandidate
from agent.schemas.public.tasks import AgentTask, ExplanationInput, ExtractionInput, JobContext, ReportInput
from agent.services.document_ocr import recognize_document
from agent.services.facts import allowed_facts
from agent.services.hashing import content_hash
from app.agent_ports import DatabaseAgentPorts
from app.db import dump, load, now
from app.errors import ApiError
from app.schemas import AGENT_DOCUMENT_TYPES, TYPE_REVIEW_REASONS
from app.integration_store import (
    IntegrationStore,
    check_job,
    identifier,
    owned_document,
    owned_group,
    source_ref,
)


class AgentService:
    def __init__(self, db, settings, agent_settings, *, provider=None, recognizer=None):
        self.db, self.settings, self.agent_settings = db, settings, agent_settings
        self.provider, self.recognizer = provider, recognizer or recognize_document
        self.store = IntegrationStore(db, settings.data_dir)
        self.ports = DatabaseAgentPorts(self.store)
        self.semaphore = asyncio.Semaphore(
            getattr(settings, "worker_concurrency", getattr(settings, "agent_concurrency", 1))
        )

    def _require_unsegmented_document(self, con, document_id):
        if (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_export_documents'"
            ).fetchone()
            and con.execute(
                "SELECT 1 FROM review_export_documents WHERE document_id=?", (document_id,)
            ).fetchone()
        ):
            raise ApiError(
                409,
                "REVIEW_PART_REPROCESS_REQUIRED",
                "검토 작업실에서 원문을 재처리하고 새 확인값을 연결해 주세요.",
            )

    def enqueue_analysis(self, owner_id, group_id, *, idempotency_key=None):
        from app.agent_mapping import analysis_payload

        with self.db.connect(write=True) as con:
            group = owned_group(con, group_id, owner_id)
            if idempotency_key:
                old = con.execute(
                    "SELECT * FROM agent_jobs WHERE owner_id=? AND idempotency_key=?",
                    (owner_id, idempotency_key),
                ).fetchone()
                if old:
                    saved = con.execute(
                        "SELECT * FROM agent_snapshots WHERE id=?", (old["snapshot_id"],)
                    ).fetchone()
                    current = con.execute(
                        "SELECT input_version FROM cases WHERE id=?", (group["case_id"],)
                    ).fetchone()
                    if (
                        old["task_kind"] != "analyze_case"
                        or old["group_id"] != group_id
                        or not saved
                        or saved["group_version"] != group["version"]
                        or saved["input_version"] != current[0]
                    ):
                        raise ApiError(409, "IDEMPOTENCY_KEY_REUSED")
                    return old["id"]
            snapshot = self.store.freeze_snapshot(con, group_id, owner_id)
            return self.store.enqueue(
                con,
                owner_id,
                "analyze_case",
                analysis_payload(snapshot),
                case_id=group["case_id"],
                group_id=group_id,
                snapshot_id=snapshot.snapshot_id,
                idempotency_key=idempotency_key,
            )

    def enqueue_read(self, owner_id, document_id, *, idempotency_key=None):
        with self.db.connect(write=True) as con:
            ref = source_ref(con, document_id, owner_id)
            self._require_unsegmented_document(con, document_id)
            job_id = self.store.enqueue(
                con,
                owner_id,
                "read_document",
                {"source": ref.model_dump(mode="json")},
                document_id=document_id,
                idempotency_key=idempotency_key,
            )
            if con.execute("SELECT status FROM agent_jobs WHERE id=?", (job_id,)).fetchone()[0] in (
                "PENDING",
                "RUNNING",
            ):
                con.execute(
                    "UPDATE documents SET processing_status='PROCESSING',progress=20,error_code=NULL WHERE id=?",
                    (document_id,),
                )
            return job_id

    async def read_document(self, owner_id, document_id):
        job_id = self.enqueue_read(owner_id, document_id)
        await self.execute_job(job_id)
        return self.store.job(owner_id, job_id)

    def enqueue_extraction(
        self,
        owner_id,
        document_id,
        *,
        document_type=None,
        method="text",
        target_fields=(),
        case_id=None,
        idempotency_key=None,
    ):
        with self.db.connect(write=True) as con:
            ref = source_ref(con, document_id, owner_id, case_id)
            self._require_unsegmented_document(con, document_id)
            doc = owned_document(con, document_id, owner_id)
            reading = con.execute(
                """SELECT * FROM document_ocr WHERE document_id=? AND document_version=?
                AND content_hash=? ORDER BY created_at DESC,id DESC LIMIT 1""",
                (document_id, ref.document_version, ref.content_hash),
            ).fetchone()
            if reading is None:
                raise ApiError(409, "OCR_REQUIRED")
            kinds = {
                "CONTRACT": "contract",
                "ATTENDANCE": "attendance",
                "PAYSLIP": "payslip",
                "DEPOSIT": "deposit",
                "EMPLOYMENT_CONTRACT": "contract",
                "WORK_RECORD": "attendance",
                "BANK_STATEMENT": "deposit",
            }
            kind = document_type or kinds.get(doc["document_type"], doc["document_type"].lower())
            if kind not in ("contract", "attendance", "payslip", "deposit", "payslip_ocr", "auto"):
                kind = "auto"
            base_id = None
            if method == "pdf_supplement":
                base = con.execute(
                    "SELECT * FROM extraction_candidates WHERE document_id=? AND document_version=? AND ocr_result_id=? AND method='text' ORDER BY created_at DESC,id DESC LIMIT 1",
                    (document_id, ref.document_version, reading["id"]),
                ).fetchone()
                if base is None:
                    raise ApiError(409, "BASE_EXTRACTION_REQUIRED")
                candidate = load(base["data_json"])
                kind, base_id = candidate["document_type"], base["id"]
                available = {
                    f["field_id"]
                    for f in candidate["fields"]
                    if f["validation_status"] in ("needs_review", "missing")
                }
                confirmed = {
                    r["key"]
                    for r in con.execute(
                        "SELECT key FROM fields WHERE document_id=? AND status='CONFIRMED'", (document_id,)
                    )
                }
                confirmed.update(
                    r["key"]
                    for r in con.execute(
                        "SELECT f.key FROM fields f JOIN field_overrides o ON o.field_id=f.id WHERE f.document_id=? AND o.status='CONFIRMED'",
                        (document_id,),
                    )
                )
                selected = {
                    item.field_id if hasattr(item, "field_id") else item["field_id"] for item in target_fields
                }
                if not selected or not selected <= available - confirmed:
                    raise ApiError(422, "SUPPLEMENT_TARGET_NOT_ELIGIBLE")
            payload = ExtractionInput(
                source=ref,
                ocr_result_id=reading["id"],
                document_type=kind,
                extraction_method=method,
                target_fields=list(target_fields),
                model_config_version=self.agent_settings.model_config_version,
            )
            job_id = self.store.enqueue(
                con,
                owner_id,
                "extract_document",
                payload,
                case_id=case_id,
                document_id=document_id,
                idempotency_key=idempotency_key,
                metadata={"base_candidate_id": base_id} if base_id else None,
            )
            # The base candidate is immutable request metadata outside the
            # agent's public payload; retained for supplement lineage.
            if con.execute("SELECT status FROM agent_jobs WHERE id=?", (job_id,)).fetchone()[0] in (
                "PENDING",
                "RUNNING",
            ):
                con.execute(
                    "UPDATE documents SET processing_status='PROCESSING',progress=50,error_code=NULL WHERE id=?",
                    (document_id,),
                )
            return job_id

    def enqueue_explanation(self, owner_id, result_id, *, generation_mode="template", idempotency_key=None):
        with self.db.connect(write=True) as con:
            result, row, _ = self.store.result(con, owner_id, result_id)
            payload = ExplanationInput(
                result=result,
                allowed_facts=allowed_facts(result),
                generation_mode=generation_mode,
                model_config_version=self.agent_settings.model_config_version,
            )
            return self.store.enqueue(
                con,
                owner_id,
                "explain_result",
                payload,
                case_id=row["case_id"],
                group_id=row["group_id"],
                snapshot_id=row["snapshot_id"],
                idempotency_key=idempotency_key,
            )

    def enqueue_report(
        self, owner_id, result_id, explanation_job_id, *, selected_document_ids=(), idempotency_key=None
    ):
        from agent.schemas.public.tasks import ReportDisplayContext

        with self.db.connect(write=True) as con:
            result, row, snapshot = self.store.result(con, owner_id, result_id)
            if idempotency_key:
                old = con.execute(
                    "SELECT * FROM agent_jobs WHERE owner_id=? AND idempotency_key=?",
                    (owner_id, idempotency_key),
                ).fetchone()
                if old:
                    original = load(old["payload_json"])
                    if (
                        old["task_kind"] != "build_report"
                        or original.get("result", {}).get("result_id") != result_id
                        or original.get("explanation_version") != explanation_job_id
                        or {s["document_id"] for s in original.get("selected_sources", [])}
                        != set(map(str, selected_document_ids))
                    ):
                        raise ApiError(409, "IDEMPOTENCY_KEY_REUSED")
                    return old["id"]
            explanation_job = con.execute(
                "SELECT * FROM agent_jobs WHERE id=? AND owner_id=? AND task_kind='explain_result' AND status='COMPLETED'",
                (explanation_job_id, owner_id),
            ).fetchone()
            if explanation_job is None:
                raise ApiError(409, "EXPLANATION_REQUIRED")
            explanation = ExplanationCandidate.model_validate(load(explanation_job["output_json"]))
            if explanation.result_id != result_id or explanation.result_hash != result.result_hash:
                raise ApiError(409, "EXPLANATION_RESULT_MISMATCH")
            source_map = {
                str(s["document_id"]): SourceRef.model_validate(s)
                for s in snapshot["authorized_sources"]
                if s["kind"] == "document"
            }
            if (
                len(set(map(str, selected_document_ids))) != len(selected_document_ids)
                or not set(map(str, selected_document_ids)) <= source_map.keys()
            ):
                raise ApiError(422, "REPORT_SOURCE_OUTSIDE_RESULT")
            selected = [source_map[str(did)] for did in selected_document_ids]
            # Display values are frozen alongside the snapshot at submission.
            # Never use today's group fields to describe yesterday's result.
            frozen = load(
                con.execute(
                    "SELECT display_json FROM agent_snapshots WHERE id=?", (row["snapshot_id"],)
                ).fetchone()[0]
            )
            display = ReportDisplayContext(
                snapshot_id=result.snapshot_id,
                snapshot_hash=result.snapshot_hash,
                workplace_name=frozen.get("workplace_name", ""),
                period=snapshot["period"],
                conditions=[
                    {
                        "label": f["field_id"],
                        "value": str(f["value"]) if f["value"] is not None else "",
                        "state": f["state"],
                        "evidence_refs": f["evidence_refs"],
                    }
                    for f in snapshot["confirmed_fields"]
                ],
                sources=[
                    {
                        "source_id": s.source_id,
                        "filename": frozen.get("source_names", {}).get(s.document_id, s.document_id),
                    }
                    for s in selected
                ],
            )
            payload = ReportInput(
                result=result,
                explanation=explanation,
                explanation_version=explanation_job_id,
                selected_sources=selected,
                generated_at=datetime.now(timezone.utc),
                template_version="report-v2",
                display_context=display,
            )
            return self.store.enqueue(
                con,
                owner_id,
                "build_report",
                payload,
                case_id=row["case_id"],
                group_id=row["group_id"],
                snapshot_id=row["snapshot_id"],
                idempotency_key=idempotency_key,
            )

    def context(self, job):
        return JobContext(
            job_id=job["id"],
            run_id=job["run_id"],
            execution_token=job["token"],
            owner_id=str(job["owner_id"]),
            case_id=str(job["case_id"]) if job["case_id"] is not None else None,
            task_kind="extract_document" if job["task_kind"] == "read_document" else job["task_kind"],
            schema_version=SCHEMA_VERSION,
            idempotency_key=job["idempotency_key"],
            requested_at=job["created_at"],
        )

    def inquiry(self, owner_id, result_id, *, question_ids=None, text=None, rule_ids=None):
        from agent.services.inquiry import compose_inquiry

        with self.db.connect(write=True) as con:
            result, row, snapshot = self.store.result(con, owner_id, result_id)
            frozen = load(
                con.execute(
                    "SELECT display_json FROM agent_snapshots WHERE id=?", (row["snapshot_id"],)
                ).fetchone()[0]
            )
            if rule_ids is not None:
                question_ids = [
                    q.question_id for q in result.questions if set(q.blocking_rule_ids) & set(rule_ids)
                ]
            body = compose_inquiry(
                result,
                question_ids if question_ids is not None else [q.question_id for q in result.questions],
                workplace_name=frozen.get("workplace_name", ""),
                period=snapshot["period"],
                rule_ids=rule_ids,
            )
            if text is not None:
                if not isinstance(text, str) or not 1 <= len(text) <= 20000:
                    raise ApiError(422, "INVALID_INQUIRY_TEXT")
                body["text"] = text
                body["user_edited"] = True
            body["inquiry_id"] = identifier()
            con.execute(
                "INSERT INTO agent_inquiries(id,result_id,owner_id,data_json,created_at) VALUES(?,?,?,?,?)",
                (body["inquiry_id"], result_id, owner_id, dump(body), now()),
            )
            return body

    def _check_extraction_baseline(self, con, job):
        if job["task_kind"] != "extract_document":
            return
        payload = load(job["payload_json"])
        latest = con.execute(
            """SELECT id FROM document_ocr WHERE document_id=? AND document_version=?
            AND content_hash=? ORDER BY created_at DESC,id DESC LIMIT 1""",
            (job["document_id"], payload["source"]["document_version"], payload["source"]["content_hash"]),
        ).fetchone()
        if latest is None or latest["id"] != payload["ocr_result_id"]:
            raise IntegrityError("Stored reading baseline changed; request a new extraction")
        if payload["extraction_method"] == "pdf_supplement":
            latest_a = con.execute(
                "SELECT id FROM extraction_candidates WHERE document_id=? AND ocr_result_id=? AND method='text' ORDER BY created_at DESC,id DESC LIMIT 1",
                (job["document_id"], payload["ocr_result_id"]),
            ).fetchone()
            if latest_a is None or latest_a["id"] != (load(job["metadata_json"]) or {}).get(
                "base_candidate_id"
            ):
                raise IntegrityError("Base candidate changed; request a new supplement")
            confirmed = {
                r["key"]
                for r in con.execute(
                    "SELECT key FROM fields WHERE document_id=? AND status='CONFIRMED'", (job["document_id"],)
                )
            }
            confirmed.update(
                r["key"]
                for r in con.execute(
                    "SELECT f.key FROM fields f JOIN field_overrides o ON o.field_id=f.id WHERE f.document_id=? AND o.status='CONFIRMED'",
                    (job["document_id"],),
                )
            )
            if confirmed & {target["field_id"] for target in payload["target_fields"]}:
                raise IntegrityError("Selected supplement field was already confirmed")

    async def execute_next(self):
        async with self.semaphore:
            job = self.store.claim()
            if job is None:
                return False
            await self._execute(job)
            return True

    async def execute_job(self, job_id):
        async with self.semaphore:
            job = self.store.claim(job_id)
            if job is not None:
                await self._execute(job)
            return job is not None

    async def _execute(self, job):
        context = self.context(job)
        try:
            if job["task_kind"] == "read_document":
                await self._read(job, context)
                return
            with self.db.connect() as con:
                self._check_extraction_baseline(con, job)
            task = AgentTask(context=context, payload=load(job["payload_json"]))
            # There is deliberately no open DB connection at this await.
            outcome = await run_task(
                task, ports=self.ports, settings=self.agent_settings, provider=self.provider
            )
            if outcome.status == "completed":
                await self.verify_sources(context)
            self.finalize(context, outcome)
        except asyncio.CancelledError:
            # Startup recovery takes this job back with a new execution token.
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, AgentError) else "INTEGRATION_EXECUTION_FAILED"
            self.fail(
                context, code, str(exc) if isinstance(exc, AgentError) else "작업 실행을 완료하지 못했습니다."
            )

    async def verify_sources(self, context):
        """Verify referenced file bytes without holding the final writer lock."""
        from app.integration_store import task_sources

        with self.db.connect() as con:
            row = self.ports._check(con, context)
            payload = load(row["payload_json"])
            refs = task_sources(payload)
            rid = payload.get("result", {}).get("result_id")
            if rid:
                _, _, snapshot = self.store.result(con, context.owner_id, rid)
                refs += [SourceRef.model_validate(item) for item in snapshot["authorized_sources"]]
        checked = set()
        for ref in refs:
            if ref.kind == "document" and ref.source_id not in checked:
                await self.ports.read_source(context, ref)
                await self.ports.read_original(context, ref)
                checked.add(ref.source_id)

    def fail(self, context, code, message):
        with self.db.connect(write=True) as con:
            try:
                row = check_job(con, context)
            except JobCancelled:
                # A caller may request cancellation by flag while execution is
                # between control checks. Acknowledge only this still-owned run.
                from app.integration_store import token_hash

                current = con.execute("SELECT * FROM agent_jobs WHERE id=?", (context.job_id,)).fetchone()
                if (
                    current
                    and current["status"] == "RUNNING"
                    and current["run_id"] == context.run_id
                    and current["execution_hash"] == token_hash(context.execution_token.get_secret_value())
                ):
                    con.execute(
                        "UPDATE agent_jobs SET status='CANCELLED',error_code='JOB_CANCELLED',completed_at=?,execution_hash=NULL WHERE id=?",
                        (now(), context.job_id),
                    )
                return
            except ExecutionRevoked:
                return
            status = (
                "CANCELLED" if code in ("JOB_CANCELLED", "ACCESS_REVOKED", "EXECUTION_REVOKED") else "FAILED"
            )
            con.execute(
                "UPDATE agent_jobs SET status=?,error_code=?,error_message=?,execution_hash=NULL,completed_at=? WHERE id=?",
                (status, code, message, now(), context.job_id),
            )
            from app.compatibility import complete_job

            complete_job(con, row, error_code=code)
            if row["document_id"]:
                con.execute(
                    "UPDATE documents SET processing_status='FAILED',progress=100,error_code=? WHERE id=? AND deleted_at IS NULL",
                    (code, row["document_id"]),
                )

    async def _read(self, job, context):
        ref = SourceRef.model_validate(load(job["payload_json"])["source"])
        content = await self.ports.read_source(context, ref)
        data = content.data
        derivative = None
        if content.mime_type in ("image/jpeg", "image/png"):

            def convert():
                from PIL import Image, ImageOps

                with Image.open(io.BytesIO(data)) as image:
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    target = io.BytesIO()
                    image.save(target, format="PDF", resolution=150)
                    return target.getvalue()

            converted = await asyncio.to_thread(convert)
            derived_hash = content_hash(converted)
            filename = derived_hash + ".pdf"
            derivative = {
                "id": identifier(),
                "storage_name": filename,
                "content_hash": derived_hash,
                "original_hash": content_hash(data),
            }
            data = converted

        async def control():
            await self.ports.check_access(context, [ref])

        recognized = await self.recognizer(data, self.agent_settings, control=control)
        await control()
        await self.ports.read_source(context, ref)
        rid = identifier()
        recognized.update(
            id=rid,
            document_id=ref.document_id,
            document_version=ref.document_version,
            source_id=ref.source_id,
            content_hash=content_hash(data),
        )
        temporary, destination, moved, existed = None, None, False, False
        if derivative:
            destination = self.store.data_dir / "derived" / derivative["storage_name"]
            temporary = destination.with_suffix("." + context.run_id + ".tmp")
            existed = destination.exists()
            await asyncio.to_thread(temporary.write_bytes, data)
        try:
            with self.db.connect(write=True) as con:
                self.ports._check(con, context, [ref])
                if derivative:
                    temporary.replace(destination)
                    moved = True
                    con.execute(
                        """INSERT OR IGNORE INTO document_derivatives(id,document_id,document_version,original_hash,
                        content_hash,storage_name,media_type,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            derivative["id"],
                            ref.document_id,
                            ref.document_version,
                            derivative["original_hash"],
                            derivative["content_hash"],
                            derivative["storage_name"],
                            "application/pdf",
                            now(),
                        ),
                    )
                con.execute(
                    """INSERT INTO document_ocr(id,job_id,document_id,document_version,source_id,content_hash,data_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        rid,
                        job["id"],
                        ref.document_id,
                        ref.document_version,
                        ref.source_id,
                        recognized["content_hash"],
                        dump(recognized),
                        now(),
                    ),
                )
                from app.agent_review import invalidate_document_confirmations

                invalidate_document_confirmations(con, ref.document_id, "SOURCE_READ_REFRESHED")
                con.execute(
                    """UPDATE comparison_groups SET needs_review=1,version=version+1,updated_at=? WHERE id IN
                    (SELECT group_id FROM group_documents WHERE document_id=?)""",
                    (now(), ref.document_id),
                )
                self._schedule_document_cases(con, ref.document_id)
                con.execute(
                    "UPDATE documents SET processing_status='READ_READY',progress=40,error_code=NULL,page_count=? WHERE id=?",
                    (recognized["page_count"], ref.document_id),
                )
                con.execute(
                    "UPDATE agent_jobs SET status='COMPLETED',output_json=?,completed_at=?,execution_hash=NULL WHERE id=?",
                    (dump({"ocr_result_id": rid, "page_count": recognized["page_count"]}), now(), job["id"]),
                )
        except BaseException:
            if moved and not existed:
                destination.unlink(missing_ok=True)
            raise
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _set_detected_type(con, document_id, detected):
        """Record (or clear) the agent's document-type judgement in aiMetadata.detectedType."""
        doc = con.execute("SELECT ai_metadata FROM documents WHERE id=?", (document_id,)).fetchone()
        if doc is None:
            return
        metadata = load(doc["ai_metadata"]) or {}
        metadata.pop("detectedType", None)
        if detected:
            metadata["detectedType"] = detected
        con.execute("UPDATE documents SET ai_metadata=? WHERE id=?", (dump(metadata), document_id))

    def _schedule_document_cases(self, con, document_id):
        from app.compatibility import schedule

        for link in con.execute(
            "SELECT case_id FROM document_links WHERE document_id=?", (document_id,)
        ).fetchall():
            schedule(con, link["case_id"], delay=getattr(self.settings, "recompute_debounce_seconds", 0.3))

    def finalize(self, context, outcome):
        """Guard publication and job success together in one fresh transaction."""
        from app.agent_mapping import confirm_result

        # Potentially large ZIP bytes are checked before taking the final write
        # lock. The publication transaction below rechecks their immutable
        # metadata and the job's execution token.
        if outcome.status == "completed" and hasattr(outcome.payload, "artifacts"):
            with self.db.connect() as con:
                self.ports._check(con, context)
                staged_files = {
                    a.artifact_id: con.execute(
                        "SELECT * FROM agent_artifacts WHERE id=? AND job_id=?",
                        (a.artifact_id, context.job_id),
                    ).fetchone()
                    for a in outcome.payload.artifacts
                }
            for artifact in outcome.payload.artifacts:
                staged = staged_files[artifact.artifact_id]
                if staged is None:
                    raise IntegrityError("Report artifact is not durably staged")
                data = (self.store.data_dir / "artifacts" / staged["storage_name"]).read_bytes()
                if content_hash(data) != artifact.content_hash or len(data) != artifact.size_bytes:
                    raise IntegrityError("Staged report bytes changed")
        with self.db.connect(write=True) as con:
            row = self.ports._check(con, context)
            self._check_extraction_baseline(con, row)
            if outcome.job_id != context.job_id or outcome.run_id != context.run_id:
                raise IntegrityError("Outcome belongs to another execution")
            if outcome.status != "completed":
                # A document-type conflict is a user decision, not a broken document:
                # surface its specific code and keep the document reviewable.
                type_review = (
                    row["task_kind"] == "extract_document"
                    and outcome.status == "failed"
                    and outcome.error.reason in TYPE_REVIEW_REASONS
                )
                code = outcome.error.reason if type_review else outcome.error.code
                con.execute(
                    "UPDATE agent_jobs SET status=?,error_code=?,error_message=?,completed_at=?,execution_hash=NULL WHERE id=?",
                    (
                        "CANCELLED" if outcome.status == "cancelled" else "FAILED",
                        code,
                        outcome.error.message,
                        now(),
                        row["id"],
                    ),
                )
                from app.compatibility import complete_job

                complete_job(con, row, error_code=code)
                if row["document_id"] and type_review:
                    detected = AGENT_DOCUMENT_TYPES.get(outcome.error.detected_document_type or "")
                    self._set_detected_type(con, row["document_id"], detected)
                    con.execute(
                        "UPDATE documents SET processing_status='REVIEW_REQUIRED',error_code=?,progress=100 WHERE id=? AND deleted_at IS NULL",
                        (code, row["document_id"]),
                    )
                elif row["document_id"]:
                    con.execute(
                        "UPDATE documents SET processing_status='FAILED',error_code=?,progress=100 WHERE id=? AND deleted_at IS NULL",
                        (code, row["document_id"]),
                    )
                return
            output = outcome.payload.model_dump(mode="json")
            result_id = None
            if row["task_kind"] == "analyze_case":
                saved = con.execute(
                    "SELECT * FROM agent_snapshots WHERE id=?", (row["snapshot_id"],)
                ).fetchone()
                snapshot = InputSnapshot.model_validate_json(saved["data_json"])
                group = owned_group(con, row["group_id"], row["owner_id"])
                case = con.execute("SELECT * FROM cases WHERE id=?", (row["case_id"],)).fetchone()
                fresh = (
                    case["input_version"] == saved["input_version"]
                    and group["version"] == saved["group_version"]
                    and not group["needs_review"]
                )
                result_id = identifier()
                version = con.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM agent_results WHERE group_id=?",
                    (row["group_id"],),
                ).fetchone()[0]
                result = confirm_result(
                    outcome.payload,
                    snapshot,
                    result_id=result_id,
                    result_version=version,
                    freshness="current" if fresh else "stale",
                    created_at=datetime.now(timezone.utc),
                )
                output = result.model_dump(mode="json")
                con.execute(
                    """INSERT INTO agent_results(id,job_id,owner_id,case_id,group_id,snapshot_id,version,freshness,result_hash,data_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        result_id,
                        row["id"],
                        row["owner_id"],
                        row["case_id"],
                        row["group_id"],
                        row["snapshot_id"],
                        version,
                        result.freshness,
                        result.result_hash,
                        dump(output),
                        now(),
                    ),
                )
                if fresh:
                    con.execute(
                        "UPDATE comparison_groups SET latest_result_id=? WHERE id=?",
                        (result_id, row["group_id"]),
                    )
            elif row["task_kind"] == "extract_document":
                payload = load(row["payload_json"])
                candidate_id = identifier()
                if (
                    str(output["document_id"]) != str(row["document_id"])
                    or output["document_version"] != payload["source"]["document_version"]
                ):
                    raise IntegrityError("Candidate source mismatch")
                con.execute(
                    """INSERT INTO extraction_candidates(id,job_id,document_id,document_version,ocr_result_id,method,base_candidate_id,data_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        candidate_id,
                        row["id"],
                        row["document_id"],
                        output["document_version"],
                        payload["ocr_result_id"],
                        payload["extraction_method"],
                        (load(row["metadata_json"]) or {}).get("base_candidate_id"),
                        dump(output),
                        now(),
                    ),
                )
                if payload["extraction_method"] == "text":
                    from app.source_location import bounding_box, evidence_page

                    reading = con.execute(
                        "SELECT data_json FROM document_ocr WHERE id=?", (payload["ocr_result_id"],)
                    ).fetchone()
                    recognized = load(reading["data_json"]) if reading else None
                    current_keys = {field["field_id"] for field in output["fields"]}
                    for old in con.execute("SELECT id,key FROM fields WHERE document_id=?", (row["document_id"],)).fetchall():
                        if old["key"] not in current_keys:
                            con.execute("UPDATE fields SET status='NEEDS_RECONFIRMATION',confirmed_at=NULL WHERE id=?", (old["id"],))
                    for field in output["fields"]:
                        source = {
                            "documentId": row["document_id"],
                            "page": evidence_page(field["source_page"], field["source_quote"]),
                            "text": field["source_quote"],
                            "boundingBox": bounding_box(
                                recognized, field["source_page"], field["source_quote"], field.get("raw_value")
                            ),
                            "coordinateSystem": "RELATIVE_0_1",
                            "evidenceRefs": field["evidence_refs"],
                            "candidateId": candidate_id,
                            "documentVersion": output["document_version"],
                            "unit": field["unit"],
                        }
                        con.execute(
                            """INSERT INTO fields(document_id,key,label,value_json,value_type,status,source_json)
                            VALUES(?,?,?,?,?,?,?) ON CONFLICT(document_id,key) DO UPDATE SET value_json=excluded.value_json,
                            status=excluded.status,source_json=excluded.source_json WHERE fields.status!='CONFIRMED' AND fields.edited=0""",
                            (
                                row["document_id"],
                                field["field_id"],
                                field["field_id"],
                                dump(field["normalized_value"]),
                                "TEXT",
                                "MISSING" if field["normalized_value"] is None else "UNCONFIRMED" if field["validation_status"] == "valid" else "CONFLICTED",
                                dump(source),
                            ),
                        )
                    from app.agent_review import invalidate_document_confirmations

                    invalidate_document_confirmations(con, row["document_id"], "BASE_EXTRACTION_REFRESHED")
                    con.execute(
                        """UPDATE comparison_groups SET needs_review=1,version=version+1,updated_at=? WHERE id IN
                        (SELECT group_id FROM group_documents WHERE document_id=?)""",
                        (now(), row["document_id"]),
                    )
                detected = None
                if payload["document_type"] == "auto":
                    # AUTO uploads take the type the agent verified; an explicit type is never overwritten.
                    detected = AGENT_DOCUMENT_TYPES.get(output["document_type"])
                    if detected:
                        con.execute(
                            "UPDATE documents SET document_type=? WHERE id=? AND document_type='AUTO'",
                            (detected, row["document_id"]),
                        )
                # Also clears a judgement left by an earlier type conflict.
                self._set_detected_type(con, row["document_id"], detected)
                self._schedule_document_cases(con, row["document_id"])
                con.execute(
                    "UPDATE documents SET processing_status='REVIEW_REQUIRED',progress=100,error_code=NULL WHERE id=?",
                    (row["document_id"],),
                )
                output = {"candidate_id": candidate_id, "candidate": output}
            elif row["task_kind"] == "build_report":
                for artifact in outcome.payload.artifacts:
                    staged = con.execute(
                        "SELECT * FROM agent_artifacts WHERE id=? AND job_id=?",
                        (artifact.artifact_id, row["id"]),
                    ).fetchone()
                    if (
                        staged is None
                        or staged["content_hash"] != artifact.content_hash
                        or staged["size_bytes"] != artifact.size_bytes
                        or staged["mime_type"] != artifact.mime_type
                    ):
                        raise IntegrityError("Report artifact is not durably staged")
                    # Checkpoint replay can reuse bytes staged by an earlier
                    # run of the same logical immutable job, after these checks.
                    con.execute(
                        "UPDATE agent_artifacts SET status='PUBLISHED',run_id=? WHERE id=?",
                        (context.run_id, artifact.artifact_id),
                    )
            if (
                row["task_kind"] == "analyze_case"
                and fresh
                and (row["group_id"].startswith("default-") or row["group_id"].startswith("compat-"))
            ):
                from app.compatibility import project

                metadata = load(row["metadata_json"]) or {}
                project(con, output, checks=metadata.get("checks"), fingerprints=metadata.get("fingerprints"))
            con.execute(
                "UPDATE agent_jobs SET status='COMPLETED',output_json=?,result_id=?,completed_at=?,execution_hash=NULL WHERE id=?",
                (dump(output), result_id, now(), row["id"]),
            )
            from app.compatibility import complete_job

            if row["task_kind"] == "analyze_case":
                complete_job(con, row, outcome=output)
