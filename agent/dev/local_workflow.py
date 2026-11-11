"""File-backed development cases; intentionally independent of a production backend."""

import asyncio
import copy
import io
import re
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pypdf import PdfReader

from agent import run_task
from agent.config import ENGINE_VERSION, EXTRACTION_COMPATIBILITY_VERSION
from agent.dev.candidate_selection import (
    clear_candidates,
    initialize_candidates,
    select_candidate,
    supplement_entry,
    supplement_targets,
    validate_supplement_lineage,
)
from agent.dev.payroll_pages import archive_current, result_entry, sources_for_entry
from agent.ports.local import LocalFilePorts
from agent.schemas.public.base import Period
from agent.schemas.public.errors import AgentError, IntegrityError, JobCancelled
from agent.schemas.public.inputs import (
    ConfirmedField,
    EvidenceRef,
    InputSnapshot,
    SourceRef,
    StatementItem,
    TransactionAllocation,
    WorkRecord,
)
from agent.schemas.public.results import AnalysisResult, ExplanationCandidate
from agent.schemas.public.tasks import (
    AgentTask,
    AnalysisJobInput,
    ExplanationInput,
    ExtractionInput,
    JobContext,
    ReportInput,
)
from agent.schemas.public.wages import WageConditions
from agent.services.extraction_profiles import CONTRACT_FIELDS, validate_document_type
from agent.services.extraction_review import candidate_value, row_requires_replacement
from agent.services.facts import allowed_facts
from agent.services.hashing import content_hash, digest, snapshot_hash
from agent.services.local_json import read_json, write_json

LOCAL_OUTPUT = Path(__file__).resolve().parents[2] / "output/document-workspace"
FIELDS = CONTRACT_FIELDS

MANUAL_FIELDS = (
    "slip_rate",
    "worked_minutes",
    "slip_minutes",
    "gross",
    "deduction",
    "net",
    "deposit",
    "earning_item",
    "deduction_item",
)


def identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex


class ComparisonInputError(ValueError):
    """Safe, actionable input errors for the local materials UI."""


class ReviewedTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected: bool = False
    amount: str | None = None
    transaction_amount: str | None = None

    @field_validator("amount", "transaction_amount")
    @classmethod
    def allocation_amount(cls, value):
        if value not in (None, "") and not re.fullmatch(r"\d{1,13}(?:\.\d{1,2})?", value):
            raise ValueError("Allocation must be a nonnegative decimal string")
        return value


class ReviewedStatementRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    group: Literal["earnings", "deductions"]
    label: str = Field(default="", max_length=120)
    amount: str | None = None
    row_role: Literal["detail", "subtotal", "total", "excluded"] = "detail"
    page: int = Field(ge=1)

    @field_validator("label")
    @classmethod
    def row_label(cls, value):
        return value.strip()

    @field_validator("amount")
    @classmethod
    def row_amount(cls, value):
        if value not in (None, "") and not re.fullmatch(r"-?\d{1,13}(?:\.\d{1,2})?", value):
            raise ValueError("Row amount must be a decimal string")
        return value


class MaterialReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    earnings: dict[str, str | None] = Field(default_factory=dict)
    deductions: dict[str, str | None] = Field(default_factory=dict)
    row_roles: dict[str, Literal["detail", "subtotal", "total", "excluded"]] = Field(default_factory=dict)
    manual_rows: list[ReviewedStatementRow] = Field(default_factory=list, max_length=200)
    transactions: dict[str, ReviewedTransaction] = Field(default_factory=dict)
    distinct_transactions_confirmed: bool = False

    @field_validator("earnings", "deductions")
    @classmethod
    def row_amounts(cls, values):
        if len(values) > 9999:
            raise ValueError("Too many material rows")
        for value in values.values():
            if value not in (None, "") and not re.fullmatch(r"-?\d{1,13}(?:\.\d{1,2})?", value):
                raise ValueError("Row amount must be a decimal string")
        return values


class SourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_name: str = Field(default="", max_length=160)
    employee_id: str = Field(default="", max_length=160)
    employer_name: str = Field(default="", max_length=160)

    @field_validator("employee_name", "employee_id", "employer_name", mode="before")
    @classmethod
    def clean_identity(cls, value):
        return value.strip() if isinstance(value, str) else value


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str | None] = Field(default_factory=dict)
    contract_unit: Literal["KRW/hour", "KRW/month", "KRW/year", "KRW/day"] = "KRW/hour"
    slip_unit: Literal["", "KRW/hour", "KRW/month", "KRW/year", "KRW/day"] = ""
    contract_period_confirmed: bool = False
    period_start: str = ""
    period_end: str = ""
    manual: dict[str, str | None] = Field(default_factory=dict)
    time_kind: Literal["", "actual", "paid"] = ""
    worked_time_kind: Literal["", "actual", "paid"] = ""
    slip_time_kind: Literal["", "actual", "paid"] = ""
    subjects_confirmed: bool = False
    source_identity: dict[str, SourceIdentity] = Field(default_factory=dict)
    earnings_complete: bool = False
    deductions_complete: bool = False
    payment_scope_confirmed: bool = False
    material_review: MaterialReview = Field(default_factory=MaterialReview)
    wage_conditions: WageConditions | None = None
    work_records: list[WorkRecord] = Field(default_factory=list, max_length=1000)
    coverage: list[Period] = Field(default_factory=list, max_length=100)

    @field_validator("manual")
    @classmethod
    def manual_numbers(cls, values):
        if set(values) - set(MANUAL_FIELDS):
            raise ValueError("Unknown manual field")
        for key, value in values.items():
            pattern = (
                r"-?\d{1,13}(?:\.\d{1,2})?"
                if key in ("gross", "deduction", "net", "earning_item", "deduction_item")
                else r"\d{1,13}(?:\.\d{1,2})?"
            )
            if value not in (None, "") and (
                not re.fullmatch(pattern, value) or key in ("worked_minutes", "slip_minutes") and "." in value
            ):
                raise ValueError("Manual values must be nonnegative decimal strings; minutes integral")
        return values

    @field_validator("values")
    @classmethod
    def contract_values(cls, values):
        if set(values) - set(FIELDS):
            raise ValueError("Unknown extracted field")
        for key, value in values.items():
            if value is not None and len(value) > 100:
                raise ValueError("Field too long")
            if (
                key == "contract_rate"
                and value not in (None, "")
                and not re.fullmatch(r"\d{1,13}(?:\.\d{1,2})?", value)
            ):
                raise ValueError("Rate must be a nonnegative decimal string")
            if key in ("contract_start", "contract_end") and value:
                from datetime import date

                date.fromisoformat(value)
        return values

    def validate_for_analysis(self):
        if bool(self.period_start) != bool(self.period_end):
            raise ValueError("Both analysis period dates are required")
        if self.period_start:
            Period(start=self.period_start, end=self.period_end)
        for field_id, time_kind in (
            ("worked_minutes", self.worked_time_kind),
            ("slip_minutes", self.slip_time_kind),
        ):
            if self.manual.get(field_id) not in (None, "") and not (time_kind or self.time_kind):
                raise ValueError("Time basis actual/paid must be confirmed for each source")
        if self.manual.get("slip_rate") not in (None, "") and not self.slip_unit:
            raise ValueError("Statement rate unit must be confirmed")
        return self


def extraction_draft(payload):
    """Keep generated candidates separate from the user's working confirmation."""
    data = payload if isinstance(payload, dict) else payload.model_dump(mode="json")
    fields = {field["field_id"]: field for field in data.get("fields", [])}
    draft = Confirmation().model_dump(mode="json")
    draft["values"] = {key: candidate_value(field) for key, field in fields.items() if key in FIELDS}
    if data.get("document_type") != "contract":
        draft["manual"] = {
            key: candidate_value(field)
            for key, field in fields.items()
            if key in MANUAL_FIELDS and candidate_value(field) is not None
        }
        if (
            data.get("document_type") == "deposit"
            and candidate_value(fields.get("deposit_total", {})) is not None
        ):
            draft["manual"]["deposit"] = candidate_value(fields["deposit_total"])
        for key in ("period_start", "period_end"):
            if candidate_value(fields.get(key, {})):
                draft[key] = candidate_value(fields[key])
        if fields.get("slip_rate", {}).get("unit") in ("KRW/hour", "KRW/month", "KRW/year", "KRW/day"):
            draft["slip_unit"] = fields["slip_rate"]["unit"]
        actual_field = fields.get("slip_actual_minutes", {})
        actual_minutes = candidate_value(actual_field)
        if actual_minutes is not None or actual_field.get("validation_status") == "needs_review":
            draft["manual"]["slip_minutes"] = actual_minutes
            draft["slip_time_kind"] = "actual" if actual_minutes is not None else ""
    if fields.get("contract_rate", {}).get("unit") in ("KRW/hour", "KRW/month", "KRW/year", "KRW/day"):
        draft["contract_unit"] = fields["contract_rate"]["unit"]
    return draft


def merge_extraction_draft(current, baseline, candidate, confirmed=None):
    """Refresh untouched candidates while preserving edited, cleared, and confirmed values."""
    draft, changes = copy.deepcopy(current), []

    def empty(value):
        return None if value in (None, "") else value

    def protected(group, key):
        before = baseline.get(group, {}) if group else baseline
        target = current.get(group, {}) if group else current
        locked = (confirmed or {}).get(group, {}) if group else confirmed or {}
        value = target.get(key)
        return empty(value) != empty(before.get(key)) or (
            empty(value) is not None and empty(locked.get(key)) == empty(value)
        )

    paired = set()
    for group, rate, unit in (
        ("values", "contract_rate", "contract_unit"),
        ("manual", "slip_rate", "slip_unit"),
        ("manual", "worked_minutes", "worked_time_kind"),
        ("manual", "slip_minutes", "slip_time_kind"),
    ):
        if protected(group, rate) or protected("", unit):
            paired.update(((group, rate), ("", unit)))

    def merge(group, keys):
        incoming = candidate.get(group, {}) if group else candidate
        target = draft.setdefault(group, {}) if group else draft
        for key in sorted(keys):
            new, value = incoming.get(key), target.get(key)
            if protected(group, key) or (group, key) in paired:
                if empty(value) != empty(new):
                    changes.append({"group": group, "field_id": key, "preserved": value, "candidate": new})
            else:
                target[key] = new

    for group in ("values", "manual"):
        merge(group, set(baseline.get(group, {})) | set(candidate.get(group, {})))
    merge(
        "", ("contract_unit", "slip_unit", "period_start", "period_end", "worked_time_kind", "slip_time_kind")
    )
    # New rows and transactions always need a fresh completeness/scope confirmation.
    for key in ("earnings_complete", "deductions_complete", "payment_scope_confirmed"):
        draft[key] = False
    return Confirmation.model_validate(draft).model_dump(mode="json"), changes


def make_snapshot(case, document_ref, confirmation):
    period = Period(start=confirmation.period_start, end=confirmation.period_end)
    version = case["confirmed_revision"]
    manual_data = str(confirmation.model_dump(mode="json")).encode()
    manual_ref = SourceRef(
        source_id=identifier("manual"),
        owner_id="owner-local",
        workplace_id="workplace-local",
        link_id=identifier("link"),
        link_version=1,
        document_id=identifier("manualdoc"),
        document_version=version,
        content_hash=content_hash(manual_data),
        kind="manual",
    )
    evidence = EvidenceRef(
        evidence_id=identifier("confirmed"),
        source_id=manual_ref.source_id,
        quote="사용자가 확인한 수기 입력",
        verification_method="manual_confirmed",
    )
    fields = []
    values = {
        k: (v, "minute" if k in ("worked_minutes", "slip_minutes") else "KRW")
        for k, v in confirmation.manual.items()
        if k in ("worked_minutes", "slip_minutes", "gross", "deduction", "net") and v not in (None, "")
    }
    unit = confirmation.contract_unit
    if unit in ("KRW/hour", "KRW/month"):
        values["contract_rate"] = (confirmation.values.get("contract_rate"), unit)
        values["slip_rate"] = (confirmation.manual.get("slip_rate"), confirmation.slip_unit or unit)
    for key, (value, value_unit) in values.items():
        fields.append(
            ConfirmedField(
                field_id=key,
                value=value if value not in (None, "") else None,
                state="known" if value not in (None, "") else "unknown",
                unit=value_unit,
                confirmation_id=evidence.evidence_id + "-" + key,
                confirmation_version=version,
                evidence_refs=[evidence.evidence_id],
                period=(
                    Period(
                        start=confirmation.values["contract_start"], end=confirmation.values["contract_end"]
                    )
                    if key == "contract_rate"
                    and confirmation.values.get("contract_start")
                    and confirmation.values.get("contract_end")
                    else period
                    if key != "contract_rate" or confirmation.contract_period_confirmed
                    else None
                ),
                time_kind=(
                    (
                        confirmation.worked_time_kind
                        if key == "worked_minutes"
                        else confirmation.slip_time_kind
                    )
                    or confirmation.time_kind
                    or None
                )
                if value_unit == "minute"
                else None,
            )
        )
    manual = confirmation.manual
    snapshot = InputSnapshot(
        snapshot_id=identifier("snapshot"),
        input_version=version,
        owner_id="owner-local",
        workplace_id="workplace-local",
        case_id=case["id"],
        period=period,
        profile="M-STD" if unit == "KRW/month" else "H-STD",
        confirmed_fields=fields,
        authorized_sources=([document_ref] if document_ref is not None else []) + [manual_ref],
        evidence_catalog=[evidence],
        earnings=[
            StatementItem(
                item_id="earning", amount=manual["earning_item"], evidence_refs=[evidence.evidence_id]
            )
        ]
        if manual.get("earning_item") not in (None, "")
        else [],
        deductions=[
            StatementItem(
                item_id="deduction", amount=manual["deduction_item"], evidence_refs=[evidence.evidence_id]
            )
        ]
        if manual.get("deduction_item") not in (None, "")
        else [],
        earnings_complete=confirmation.earnings_complete,
        deductions_complete=confirmation.deductions_complete,
        payment_scope_confirmed=confirmation.payment_scope_confirmed,
        transaction_allocations=[
            TransactionAllocation(
                transaction_id=identifier("deposit"),
                amount=manual["deposit"],
                transaction_amount=manual["deposit"],
                evidence_refs=[evidence.evidence_id],
            )
        ]
        if manual.get("deposit") not in (None, "")
        else [],
        wage_conditions=confirmation.wage_conditions,
        work_records=confirmation.work_records,
        coverage=confirmation.coverage,
        rule_bundle_version="standard-wages-v1",
        support_manifest_version="standard-wages-manifest-v1",
    )
    # User-confirmed structured edits have their own immutable manual source.
    # Original evidence is retained when the edit already points to a selected document.
    if snapshot.wage_conditions is not None:
        wage_data = snapshot.wage_conditions.model_dump(mode="json")
        for value in wage_data.values():
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                if not isinstance(entry, dict) or "evidence_refs" not in entry:
                    continue
                if not entry["evidence_refs"]:
                    entry["evidence_refs"] = [evidence.evidence_id]
                if "confirmation_id" in entry:
                    entry["confirmation_id"] = entry["confirmation_id"] or evidence.evidence_id
        snapshot.wage_conditions = WageConditions.model_validate(wage_data)
    for record in snapshot.work_records:
        if not record.evidence_refs:
            record.evidence_refs = [evidence.evidence_id]
    snapshot.snapshot_hash = snapshot_hash(snapshot)
    return snapshot, manual_ref, manual_data


class LocalPorts(LocalFilePorts):
    def __init__(self, store, job):
        super().__init__(directory=store.state / "artifacts-and-checkpoints")
        self.store, self.job = store, job
        self.current_tokens, self.jobs = store.current_tokens, store.job_owners

    async def emit_progress(self, context, event):
        await super().emit_progress(context, event)
        self.job["events"].append(event.model_dump(mode="json"))
        self.store.save_job(self.job)

    async def read_ocr(self, context, source, ocr_result_id):
        await self.check_access(context, [source])
        case = self.store.cases.get(context.case_id)
        result = case.get("ocr") if case else None
        if (
            not result
            or case["document_id"] != source.document_id
            or self.store.document_ref(case["document_id"]) != source
            or result.get("id") != ocr_result_id
            or result.get("document_id") != source.document_id
            or result.get("document_version") != source.document_version
            or result.get("content_hash") != source.content_hash
        ):
            raise IntegrityError("저장된 OCR 결과와 문서 버전이 다릅니다. OCR을 다시 실행해 주세요.")
        self.store.document_data(case["document_id"])
        return copy.deepcopy(result)


class LocalStore:
    def __init__(self, settings, *, corpus=LOCAL_OUTPUT, state=None, provider=None, origin="local_console"):
        self.settings, self.corpus, self.provider = settings, Path(corpus), provider
        self.origin = origin
        self.state = Path(state) if state else self.corpus
        for folder in ("cases", "comparisons", "jobs", "uploads", "artifacts-and-checkpoints"):
            (self.state / folder).mkdir(parents=True, exist_ok=True)
        documents_path = self.state / "documents.json"
        self.documents = read_json(documents_path) if documents_path.exists() else {}
        self.cases = {p.stem: read_json(p) for p in (self.state / "cases").glob("*.json")}
        self.comparisons = {p.stem: read_json(p) for p in (self.state / "comparisons").glob("*.json")}
        self.jobs = {p.stem: read_json(p) for p in (self.state / "jobs").glob("*.json")}
        self.tasks, self.ports = {}, {}
        self.current_tokens, self.job_owners = {}, {}
        self.semaphore = asyncio.Semaphore(2)
        for case in self._all_records():
            case.setdefault("ocr", None)
            case.setdefault("document_type_revision", 1)
            case.setdefault("document_type", (case.get("extraction") or {}).get("document_type", "contract"))
            case.setdefault("extraction_revision", 1 if case.get("extraction") else 0)
            initialize_candidates(case)
            case.setdefault("materials", None)
            case.setdefault("materials_revision", 0)
            case.setdefault("confirmed_materials", None)
            case.setdefault("materials_invalid", False)
            case.setdefault("analysis_history", [])
            case.setdefault("extraction_changes", [])
            case.setdefault("draft_history", [])
            archive_current(case)
            case["extraction_needs_review"] = bool(
                case.get("extraction")
                and (
                    case.get("extraction_compatibility_version", EXTRACTION_COMPATIBILITY_VERSION)
                    != EXTRACTION_COMPATIBILITY_VERSION
                    or case["extraction"].get("extraction_version") not in {"1.0", "1.1", "1.2"}
                )
            )
            if case.get("analysis") and case["analysis"].get("engine_version") != ENGINE_VERSION:
                case.update(stale=True)
        for job in self.jobs.values():
            if job["status"] in ("queued", "running"):
                job.update(status="interrupted", error="서버가 중단되었습니다. 이어서 실행할 수 있습니다.")
                self.save_job(job)

    def save_case(self, case):
        folder = "comparisons" if case.get("kind") == "comparison" else "cases"
        write_json(self.state / folder / (case["id"] + ".json"), case)

    def _record(self, record_id):
        return self.comparisons[record_id] if record_id in self.comparisons else self.cases[record_id]

    def _all_records(self):
        return (*self.cases.values(), *self.comparisons.values())

    def _record_view(self, case):
        result = copy.deepcopy(case)
        if case.get("analysis"):
            result["report_sources"] = sources_for_entry(self, result_entry(case))
            rid = case["analysis"]["result_id"]
            result["inquiry"] = copy.deepcopy(case.get("inquiries", {}).get(rid))
        for entry in result.get("analysis_history", []):
            entry["report_sources"] = sources_for_entry(self, entry)
            entry["inquiry"] = copy.deepcopy(case.get("inquiries", {}).get(entry["analysis"]["result_id"]))
        if case.get("kind") == "comparison":
            from agent.dev.comparison_context import comparison_compatibility

            result["compatibility"] = comparison_compatibility(case.get("materials"), case["draft"])
        return result

    async def inquiry(self, case_id, request, *, save=False):
        from agent.dev.payroll_pages import generate_inquiry, save_inquiry

        case = self._record(case_id)
        (save_inquiry if save else generate_inquiry)(case, request)
        self.save_case(case)
        return self._record_view(case)

    async def answer_questions(self, case_id, request):
        case = self._record(case_id)
        self._refresh_materials(case)
        if case.get("dirty") or case.get("stale"):
            raise ComparisonInputError(
                "입력이 변경되었습니다. 변경한 입력을 확인하고 재계산한 뒤 질문에 답해 주세요."
            )
        if set(request) - {"result_id", "answers", "confirm"}:
            raise ComparisonInputError("지원하지 않는 답변 형식입니다.")
        entry = result_entry(case, request.get("result_id"))
        if entry["analysis"]["result_id"] != case["analysis"]["result_id"]:
            raise ComparisonInputError("과거 결과의 질문은 최신 결과에서 다시 확인해 주세요.")
        questions = {q["question_id"]: q for q in entry["analysis"]["questions"]}
        answers = request.get("answers")
        if not isinstance(answers, list) or not answers or len(answers) > 100:
            raise ComparisonInputError("확인할 답변을 입력해 주세요.")
        draft = copy.deepcopy(case["draft"])
        wage = draft.get("wage_conditions") or WageConditions().model_dump(mode="json")
        for answer in answers:
            if not isinstance(answer, dict) or set(answer) - {"question_id", "field_id", "state", "value"}:
                raise ComparisonInputError("답변 형식을 확인해 주세요.")
            question = questions.get(answer.get("question_id"))
            field = answer.get("field_id", "").removeprefix("wage_conditions.")
            allowed = [
                key.removeprefix("wage_conditions.") for key in (question or {}).get("target_field_ids", [])
            ]
            if (
                question is None
                or field not in allowed
                or not isinstance(wage.get(field), dict)
                or "state" not in wage[field]
            ):
                raise ComparisonInputError("이 질문은 관련 입력 화면에서 자료와 함께 확인해 주세요.")
            state = answer.get("state", "known")
            wage[field] = {
                "state": state,
                "value": answer.get("value") if state == "known" else None,
                "confirmation_id": "",
                "evidence_refs": [],
            }
        draft["wage_conditions"] = WageConditions.model_validate(wage).model_dump(mode="json")
        await self.save_draft(case_id, draft)
        case.setdefault("question_answers", []).append(
            {
                "result_id": entry["analysis"]["result_id"],
                "answers": copy.deepcopy(answers),
                "confirmed": request.get("confirm") is True,
                "revision": case["revision"],
                "saved_at": datetime.now(UTC).isoformat(),
            }
        )
        if request.get("confirm") is True:
            return await self.confirm(case_id)
        self.save_case(case)
        return self._record_view(case)

    def _validate_comparison_context(self, case, confirmation):
        if case.get("kind") != "comparison":
            return
        from agent.dev.comparison_context import comparison_compatibility

        compatibility = comparison_compatibility(case.get("materials"), confirmation.model_dump(mode="json"))
        blocking = next(
            (issue for issue in compatibility.get("issues", []) if issue.get("severity") == "blocking"), None
        )
        if blocking or compatibility.get("blocked"):
            raise ComparisonInputError(
                (blocking or {}).get("message") or "선택한 자료의 근로자·사업장·대상기간을 확인해 주세요."
            )
        if not confirmation.subjects_confirmed:
            raise ComparisonInputError(
                "같은 근로자·사업장에 관한 자료인지 원문과 확인한 뒤 대조에 반영해 주세요."
            )

    def save_job(self, job):
        write_json(self.state / "jobs" / (job["id"] + ".json"), job)

    def _refresh_materials(self, case):
        from agent.dev.comparison_materials import materials_current

        if case.get("materials") and not materials_current(self, case["materials"]):
            if not case.get("materials_invalid"):
                case.update(
                    materials_invalid=True,
                    dirty=True,
                    revision=case["revision"] + 1,
                    stale=bool(case["analysis"]),
                    explanation=None,
                    report=None,
                )
                self.save_case(case)
        return case

    def _invalidate_material_dependents(self, source_case_id):
        for dependent in self._all_records():
            if source_case_id in (dependent.get("materials") or {}).get("source_versions", {}):
                self._refresh_materials(dependent)

    async def list_comparison_sources(self):
        from agent.dev.comparison_context import list_comparison_sources

        return list_comparison_sources(self)

    async def load_materials(self, case_id, selection):
        from agent.dev.comparison_materials import build_materials
        from agent.dev.material_rebase import material_candidate, rebase_review, same_selection

        case = self._record(case_id)
        if any(self.jobs[j]["status"] in ("queued", "running") for j in case["jobs"]):
            raise ComparisonInputError("실행 중인 작업을 마친 뒤 대조 자료를 변경해 주세요.")
        expected = {"contract_case_id", "attendance_case_ids", "payslip_case_id", "deposit_case_ids"}
        if not isinstance(selection, dict) or set(selection) - expected:
            raise ComparisonInputError("대조할 문서를 유형에 맞게 선택해 주세요.")
        for key in ("attendance_case_ids", "deposit_case_ids"):
            if not isinstance(selection.get(key, []), list) or any(
                not isinstance(v, str) for v in selection.get(key, [])
            ):
                raise ComparisonInputError("대조 자료 목록을 확인해 주세요.")
        for key in ("contract_case_id", "payslip_case_id"):
            if selection.get(key) is not None and not isinstance(selection[key], str):
                raise ComparisonInputError("대조할 문서를 선택해 주세요.")
        previous = case.get("materials")
        draft = Confirmation(
            period_start=case["draft"].get("period_start", ""),
            period_end=case["draft"].get("period_end", ""),
        ).model_dump()
        materials = None
        changes = []
        if any(selection.get(key) for key in expected):
            try:
                materials = build_materials(self, selection)
            except (ValueError, KeyError) as exc:
                raise ComparisonInputError(
                    str(exc) if isinstance(exc, ValueError) else "선택한 자료를 찾을 수 없습니다."
                ) from exc
            candidate = material_candidate(draft, materials)
            if previous and same_selection(previous["selection"], materials["selection"]):
                baseline = material_candidate(Confirmation().model_dump(mode="json"), previous)
                draft, changes = merge_extraction_draft(
                    case["draft"], baseline, candidate, case.get("confirmed")
                )
                review, warnings = rebase_review(
                    previous,
                    materials,
                    case["draft"].get("material_review", {}),
                    candidate["material_review"],
                    (case.get("confirmed") or {}).get("material_review"),
                )
                draft["material_review"] = review
                materials["warnings"] = list(dict.fromkeys(materials["warnings"] + warnings))
            else:
                draft = candidate
                if previous:
                    review, warnings = rebase_review(
                        previous,
                        materials,
                        {"manual_rows": case["draft"].get("material_review", {}).get("manual_rows", [])},
                        candidate["material_review"],
                    )
                    draft["material_review"] = review
                    materials["warnings"] = list(dict.fromkeys(materials["warnings"] + warnings))
        if previous or case["draft"] != Confirmation().model_dump(mode="json"):
            case.setdefault("draft_history", []).append(
                {
                    "revision": case["revision"],
                    "saved_at": datetime.now(UTC).isoformat(),
                    "reason": "before_material_refresh"
                    if previous
                    and materials
                    and same_selection(previous["selection"], materials["selection"])
                    else "before_material_selection_change",
                    "draft": copy.deepcopy(case["draft"]),
                    "materials": copy.deepcopy(previous),
                }
            )
        selected_ids = set((materials or {}).get("source_versions", {}))
        previous_identities = {
            source["case_id"]: source.get("identity", {}) for source in (previous or {}).get("sources", [])
        }
        draft["source_identity"] = {}
        for source_id, identity in case["draft"].get("source_identity", {}).items():
            if source_id not in selected_ids:
                continue
            baseline = previous_identities.get(source_id, {})
            # Older clients saved displayed defaults as overrides. Refresh those
            # from the source; only a value different from the old source is an edit.
            edited = {
                key: value
                for key, value in identity.items()
                if value.strip() and value.strip() != (baseline.get(key) or "").strip()
            }
            if edited:
                draft["source_identity"][source_id] = edited
        draft["subjects_confirmed"] = False
        case.update(
            materials=materials,
            materials_revision=case["revision"] + 1,
            materials_invalid=False,
            confirmed_materials=None,
            draft=Confirmation.model_validate(draft).model_dump(mode="json"),
            confirmed=None,
            confirmed_revision=None,
            dirty=True,
            revision=case["revision"] + 1,
            stale=bool(case["analysis"]),
            explanation=None,
            report=None,
            extraction_changes=changes,
        )
        self.save_case(case)
        return self._record_view(case)

    def _validate_material_review(self, case, confirmation, *, allocations=False):
        from decimal import Decimal, InvalidOperation

        from agent.dev.comparison_materials import materials_current, reviewed_duplicate_groups

        materials = case.get("materials")
        review = confirmation.material_review.model_dump()
        if set(confirmation.source_identity) - set((materials or {}).get("source_versions", {})):
            raise ComparisonInputError(
                "연결한 자료에 없는 근로자·사업장 정보입니다. 자료를 다시 확인해 주세요."
            )
        if not materials:
            if any(review.values()):
                raise ComparisonInputError("자료를 먼저 연결한 뒤 상세 항목을 확인해 주세요.")
            return
        if not materials_current(self, materials):
            raise ComparisonInputError("연결한 문서가 변경되었습니다. 자료를 다시 불러온 뒤 확인해 주세요.")
        for group, key in (
            ("earnings", "item_id"),
            ("deductions", "item_id"),
            ("transactions", "transaction_id"),
        ):
            ids = {row[key] for row in materials[group]}
            if set(review[group]) - ids:
                raise ComparisonInputError("연결 자료에 없는 상세 항목입니다. 자료를 다시 불러와 주세요.")
        row_ids = {row["item_id"] for group in ("earnings", "deductions") for row in materials[group]}
        if set(review["row_roles"]) - row_ids:
            raise ComparisonInputError("연결 자료에 없는 항목 분류입니다. 자료를 다시 불러와 주세요.")
        manual_rows = review["manual_rows"]
        if len({row["row_id"] for row in manual_rows}) != len(manual_rows):
            raise ComparisonInputError("수기로 보완한 항목 ID가 중복되었습니다.")
        payslip = next(
            (
                source
                for source in materials["sources"]
                if source["case_id"] == materials["selection"].get("payslip_case_id")
            ),
            None,
        )
        if manual_rows and not payslip:
            raise ComparisonInputError("누락 항목을 보완하려면 급여명세서를 먼저 연결해 주세요.")
        if allocations and any(not row["label"] for row in manual_rows):
            raise ComparisonInputError("수기로 보완한 항목의 원문 항목명을 입력해 주세요.")
        if payslip and any(
            row["page"] > self.documents[payslip["document_id"]]["pages"] for row in manual_rows
        ):
            raise ComparisonInputError("보완 항목의 원문 페이지가 명세서 범위를 벗어났습니다.")
        selected = {}
        for row in materials["transactions"]:
            item = review["transactions"].get(row["transaction_id"], {})
            if not item.get("selected"):
                continue
            if row_requires_replacement(row, "deposit_amount"):
                raise ComparisonInputError(
                    "거래일·거래 식별정보를 확인하지 못한 입금입니다. 해당 거래의 선택을 해제하고 원문을 확인해 주세요."
                )
            try:
                amount = Decimal(item.get("amount") or "")
                original = Decimal(
                    item.get("transaction_amount")
                    if item.get("transaction_amount") is not None
                    else row["transaction_amount"] or ""
                )
                if not amount.is_finite() or amount < 0 or amount > original:
                    raise ValueError
            except InvalidOperation, ValueError, TypeError:
                raise ComparisonInputError(
                    "선택한 거래의 배분액은 확인된 입금액 이내의 숫자로 입력해 주세요."
                ) from None
            tid = row["transaction_id"]
            if tid in selected:
                raise ComparisonInputError("같은 입금 거래가 중복 선택됐습니다. 연결 자료를 확인해 주세요.")
            selected[tid] = (amount, original)
        if not allocations:
            return
        effective_groups = reviewed_duplicate_groups(materials, review)
        groups = {}
        for row in materials["transactions"]:
            group = effective_groups.get(row["transaction_id"])
            if group and row["transaction_id"] in selected:
                groups[group] = groups.get(group, 0) + 1
        if any(count > 1 for count in groups.values()) and not review["distinct_transactions_confirmed"]:
            raise ComparisonInputError(
                "날짜·금액이 같은 입금 중복 후보가 있습니다. 하나만 선택하거나 각각 별개 거래인지 확인해 주세요."
            )
        used, period_allocations = {}, {}
        target_period = (confirmation.period_start, confirmation.period_end)
        for other in self._all_records():
            saved = other.get("confirmed_materials")
            if (
                other["id"] == case["id"]
                or other.get("dirty")
                or not saved
                or not materials_current(self, saved)
            ):
                continue
            other_confirmation = other.get("confirmed") or {}
            other_period = (other_confirmation.get("period_start"), other_confirmation.get("period_end"))
            alternative = case.get("kind") == other.get("kind") == "comparison"
            if alternative and other_period == target_period:
                continue
            other_review = other_confirmation.get("material_review", {}).get("transactions", {})
            for tid, item in other_review.items():
                if item.get("selected") and item.get("amount") not in (None, ""):
                    value = Decimal(item["amount"])
                    if alternative:
                        key = (other_period, tid)
                        period_allocations[key] = max(period_allocations.get(key, Decimal(0)), value)
                    else:
                        used[tid] = used.get(tid, Decimal(0)) + value
        for (_, tid), value in period_allocations.items():
            used[tid] = used.get(tid, Decimal(0)) + value
        for tid, (amount, original) in selected.items():
            if used.get(tid, Decimal(0)) + amount > original:
                raise ComparisonInputError(
                    "다른 대조 사례에 배분한 금액과 합치면 입금액을 초과합니다. 거래 배분액을 조정해 주세요."
                )

    def document_data(self, doc_id):
        doc = self.documents[doc_id]
        data = (self.state / "uploads" / (doc_id + ".pdf")).read_bytes()
        if content_hash(data) != doc["sha256"]:
            raise ValueError("자료 해시가 달라졌습니다.")
        return data

    def document_ref(self, doc_id):
        doc = self.documents[doc_id]
        return SourceRef(
            source_id="source-" + doc_id,
            owner_id="owner-local",
            workplace_id="workplace-local",
            link_id="link-" + doc_id,
            link_version=1,
            document_id=doc_id,
            document_version=1,
            content_hash=doc["sha256"],
        )

    async def list_documents(self):
        documents = []
        for document in self.documents.values():
            candidates = [case for case in self.cases.values() if case.get("document_id") == document["id"]]
            selected = max(
                candidates,
                key=lambda case: (
                    bool(case.get("extraction")),
                    case.get("revision", 0),
                    case.get("extraction_revision", 0),
                ),
                default=None,
            )
            documents.append(
                {
                    **{key: document[key] for key in ("id", "kind", "category", "title", "pages")},
                    "case_id": selected["id"] if selected else None,
                }
            )
        return documents

    def _new_record(self, record_id, doc_id, document_type):
        return {
            "id": record_id,
            "document_id": doc_id,
            "document_type": document_type,
            "document_type_revision": 1,
            "revision": 1,
            "confirmed_revision": None,
            "dirty": True,
            "draft": Confirmation().model_dump(mode="json"),
            "confirmed": None,
            "extraction": None,
            "extraction_a": None,
            "extraction_a_revision": 0,
            "extraction_a_context": {},
            "supplements": [],
            "candidate_selections": {},
            "candidate_selection_history": [],
            "ocr": None,
            "extraction_revision": 0,
            "materials": None,
            "materials_revision": 0,
            "confirmed_materials": None,
            "materials_invalid": False,
            "analysis_history": [],
            "analysis": None,
            "explanation": None,
            "report": None,
            "jobs": [],
            "stale": False,
            "extraction_changes": [],
            "draft_history": [],
            "extraction_engine_version": None,
            "extraction_needs_review": False,
        }

    async def create_case(self, doc_id, document_type="auto"):
        validate_document_type(document_type)
        if doc_id not in self.documents:
            raise KeyError("자료를 찾을 수 없습니다.")
        case = self._new_record(identifier("case"), doc_id, document_type)
        self.cases[case["id"]] = case
        self.save_case(case)
        return copy.deepcopy(case)

    async def create_comparison(
        self, title="새 비교 묶음", *, period_start="", period_end="", selection=None
    ):
        if not isinstance(title, str) or len(title.strip()) > 120:
            raise ComparisonInputError("비교 묶음 이름은 120자 이내로 입력해 주세요.")
        confirmation = Confirmation(period_start=period_start, period_end=period_end)
        confirmation.validate_for_analysis()
        case = self._new_record(identifier("comparison"), None, "comparison")
        case.update(
            kind="comparison",
            title=title.strip() or "새 비교 묶음",
            draft=confirmation.model_dump(mode="json"),
            created_at=datetime.now(UTC).isoformat(),
        )
        self.comparisons[case["id"]] = case
        try:
            if selection is not None:
                return await self.load_materials(case["id"], selection)
            result = self._record_view(case)
            self.save_case(case)
            return result
        except Exception:
            self.comparisons.pop(case["id"], None)
            # Only this newly generated independent record can have been written.
            (self.state / "comparisons" / (case["id"] + ".json")).unlink(missing_ok=True)
            raise

    async def list_comparisons(self):
        return [self._record_view(self._refresh_materials(case)) for case in self.comparisons.values()]

    async def get_comparison(self, comparison_id):
        return self._record_view(self._refresh_materials(self.comparisons[comparison_id]))

    async def upload(self, data, document_type="auto", filename=None):
        validate_document_type(document_type)
        if not data.startswith(b"%PDF-") or len(data) > self.settings.max_file_bytes:
            raise ValueError("20MB 이하 PDF만 업로드할 수 있습니다.")
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= self.settings.max_pages:
                raise ValueError("페이지 수 또는 암호화 상태를 확인해 주세요.")
        except Exception as exc:
            raise ValueError("읽을 수 있는 암호화되지 않은 PDF가 필요합니다.") from exc
        doc_id = identifier("upload")
        (self.state / "uploads" / (doc_id + ".pdf")).write_bytes(data)
        self.documents[doc_id] = {
            "id": doc_id,
            "kind": "upload",
            "category": "사용자 업로드",
            "title": re.sub(r"[\x00-\x1f]", "", Path(filename).name)[:160] if filename else "업로드 PDF",
            "pages": len(reader.pages),
            "sha256": content_hash(data),
        }
        write_json(self.state / "documents.json", self.documents)
        return await self.create_case(doc_id, document_type)

    async def change_document_type(self, case_id, document_type):
        validate_document_type(document_type)
        case = self.cases[case_id]
        if any(self.jobs[j]["status"] in ("queued", "running") for j in case["jobs"]):
            raise ValueError("실행 중인 작업을 마친 뒤 문서 유형을 변경해 주세요.")
        if case["document_type"] == document_type:
            return copy.deepcopy(case)
        clear_candidates(case, "document_type_changed")
        case.update(
            document_type=document_type,
            document_type_revision=case["revision"] + 1,
            extraction=None,
            draft=Confirmation().model_dump(mode="json"),
            confirmed=None,
            confirmed_revision=None,
            dirty=True,
            revision=case["revision"] + 1,
            stale=bool(case["analysis"]),
            explanation=None,
            report=None,
            materials=None,
            confirmed_materials=None,
            materials_invalid=False,
            extraction_changes=[],
            extraction_draft=None,
            extraction_engine_version=None,
            extraction_needs_review=False,
        )
        self.save_case(case)
        self._invalidate_material_dependents(case_id)
        return copy.deepcopy(case)

    async def get_case(self, case_id):
        return copy.deepcopy(self._refresh_materials(self.cases[case_id]))

    async def select_candidate(self, case_id, request):
        case = self.cases[case_id]
        if any(self.jobs[j]["status"] in ("queued", "running") for j in case["jobs"]):
            raise ComparisonInputError("실행 중인 작업을 마친 뒤 후보를 선택해 주세요.")
        self._validate_ocr(case)
        ref = self.document_ref(case["document_id"])
        try:
            payload, record = select_candidate(
                case, request, document_hash=ref.content_hash, source_id=ref.source_id
            )
        except ValueError as exc:
            raise ComparisonInputError(str(exc)) from exc
        previous = extraction_draft(case["extraction"])
        candidate = extraction_draft(payload)
        draft, changes = merge_extraction_draft(case["draft"], previous, candidate, case.get("confirmed"))
        case.setdefault("draft_history", []).append(
            {
                "revision": case["revision"],
                "saved_at": datetime.now(UTC).isoformat(),
                "reason": "before_candidate_selection",
                "draft": copy.deepcopy(case["draft"]),
            }
        )
        case["candidate_selections"][record["field_id"]] = copy.deepcopy(record)
        case["candidate_selection_history"].append(record)
        case.update(
            extraction=payload.model_dump(mode="json"),
            extraction_revision=case["extraction_revision"] + 1,
            extraction_draft=candidate,
            draft=draft,
            extraction_changes=changes,
            revision=case["revision"] + 1,
            dirty=True,
            confirmed=None,
            confirmed_revision=None,
            stale=bool(case["analysis"]),
            explanation=None,
            report=None,
        )
        self.save_case(case)
        self._invalidate_material_dependents(case_id)
        return self._record_view(case)

    async def save_draft(self, case_id, draft):
        confirmation = Confirmation.model_validate(draft)
        case = self._record(case_id)
        self._validate_material_review(case, confirmation)
        case.update(
            draft=confirmation.model_dump(mode="json"),
            revision=case["revision"] + 1,
            dirty=True,
            stale=bool(case["analysis"]),
        )
        self.save_case(case)
        return self._record_view(case)

    async def confirm(self, case_id):
        case = self._record(case_id)
        self._refresh_materials(case)
        if not case.get("extraction") and not case.get("materials"):
            raise ComparisonInputError("AI 처리 페이지에서 항목을 처리하거나 AI 처리된 자료를 연결해 주세요.")
        if case.get("extraction_needs_review") and (
            not case.get("materials") or case_id in case["materials"]["source_versions"]
        ):
            raise ComparisonInputError(
                "추출 검증 기준이 변경되었습니다. 수정값은 보존되니 문서를 다시 추출해 주세요."
            )
        if case.get("materials_invalid"):
            raise ComparisonInputError("연결한 문서가 변경되었습니다. 대조 자료를 다시 불러와 주세요.")
        confirmation = Confirmation.model_validate(case["draft"])
        confirmation.validate_for_analysis()
        if not confirmation.period_start or not confirmation.period_end:
            raise ValueError("대조할 급여 기간의 시작일과 종료일을 입력해 주세요.")
        self._validate_material_review(case, confirmation, allocations=True)
        self._validate_comparison_context(case, confirmation)
        case.update(
            confirmed=confirmation.model_dump(mode="json"),
            confirmed_revision=case["revision"],
            confirmed_materials=copy.deepcopy(case.get("materials")),
            dirty=False,
        )
        self.save_case(case)
        return self._record_view(case)

    def public_job(self, job):
        return {k: copy.deepcopy(v) for k, v in job.items() if k not in ("task", "manual_sources")}

    async def get_job(self, job_id):
        return self.public_job(self.jobs[job_id])

    def _ocr_fingerprint(self):
        return digest(
            [
                self.settings.ocr_command,
                str(self.settings.ocr_data_path),
                self.settings.ocr_dpi,
                self.settings.ocr_page_timeout,
            ]
        )

    async def create_job(
        self,
        case_id,
        stage,
        *,
        include_original=False,
        field_ids=None,
        extraction_revision=None,
        selected_source_ids=None,
        result_id=None,
    ):
        if stage not in ("ocr", "extract", "supplement", "analyze", "explain", "report"):
            raise ValueError("알 수 없는 작업입니다.")
        if stage != "report" and (selected_source_ids is not None or result_id is not None):
            raise ComparisonInputError("원본 선택과 과거 결과 선택은 보고서 작업에만 사용할 수 있습니다.")
        if stage != "supplement" and (field_ids is not None or extraction_revision is not None):
            raise ComparisonInputError("보완 항목은 원문 보완 작업에서만 지정할 수 있습니다.")
        case = self._record(case_id)
        if case.get("kind") == "comparison" and stage in ("ocr", "extract", "supplement"):
            raise ComparisonInputError(
                "비교 묶음에서는 OCR·AI 추출을 실행할 수 없습니다. 개별 문서에서 실행해 주세요."
            )
        if (
            stage not in ("ocr", "extract", "supplement")
            and case.get("extraction_needs_review")
            and (not case.get("materials") or case_id in case["materials"]["source_versions"])
        ):
            raise ComparisonInputError("새 검증 기준으로 문서를 다시 추출한 뒤 확인해 주세요.")
        self._refresh_materials(case)
        if any(self.jobs[j]["status"] in ("queued", "running") for j in case["jobs"]):
            raise ValueError("현재 작업이 끝난 뒤 실행해 주세요.")
        if stage == "ocr":
            return self._create_ocr_job(case)
        targets = []
        if stage in ("extract", "supplement"):
            self._validate_ocr(case)
        if stage == "supplement":
            try:
                targets = supplement_targets(case, field_ids, extraction_revision)
                if case["extraction_a_context"].get("ocr_result_id") != case["ocr"]["id"]:
                    raise ValueError("저장 OCR이 변경되었습니다. 기본 항목 정리를 다시 실행해 주세요.")
            except ValueError as exc:
                raise ComparisonInputError(str(exc)) from exc
        historical_report = stage == "report" and result_id is not None
        if (
            stage not in ("ocr", "extract", "supplement")
            and not historical_report
            and (case["dirty"] or case["confirmed_revision"] is None)
        ):
            raise ValueError("수정한 값을 먼저 확인해 주세요.")
        if (
            stage in ("explain", "report")
            and not historical_report
            and (not case["analysis"] or case["stale"])
        ):
            raise ValueError("최신 입력으로 대조를 실행해 주세요.")
        jid = identifier("job")
        kind = {
            "extract": "extract_document",
            "supplement": "extract_document",
            "analyze": "analyze_case",
            "explain": "explain_result",
            "report": "build_report",
        }[stage]
        context = JobContext(
            job_id=jid,
            run_id=identifier("run"),
            execution_token=secrets.token_urlsafe(32),
            owner_id="owner-local",
            case_id=case_id,
            task_kind=kind,
            idempotency_key=jid,
            requested_at=datetime.now(UTC),
        )
        ref = self.document_ref(case["document_id"]) if case.get("document_id") else None
        manual_sources = []
        if stage in ("extract", "supplement"):
            payload = ExtractionInput(
                source=ref,
                document_type=case["document_type"],
                model_config_version=self.settings.model_config_version,
                ocr_result_id=case["ocr"]["id"],
                **(
                    {"extraction_method": "pdf_supplement", "target_fields": targets}
                    if stage == "supplement"
                    else {}
                ),
            )
        elif stage == "analyze":
            self._validate_comparison_context(case, Confirmation.model_validate(case["confirmed"]))
            snapshot, manual_ref, manual_data = make_snapshot(
                case, ref, Confirmation.model_validate(case["confirmed"])
            )
            if case.get("confirmed_materials"):
                from agent.dev.comparison_materials import enrich_snapshot

                self._validate_material_review(
                    case, Confirmation.model_validate(case["confirmed"]), allocations=True
                )
                snapshot = enrich_snapshot(
                    snapshot, case["confirmed_materials"], case["confirmed"]["material_review"]
                )
            manual_sources = [{"ref": manual_ref.model_dump(mode="json"), "data": manual_data.decode()}]
            payload = AnalysisJobInput(
                snapshot=snapshot,
                snapshot_id=snapshot.snapshot_id,
                snapshot_hash=snapshot.snapshot_hash,
                rule_bundle_version=snapshot.rule_bundle_version,
                support_manifest_version=snapshot.support_manifest_version,
            )
        elif stage == "explain":
            result = AnalysisResult.model_validate(case["analysis"])
            payload = ExplanationInput(
                result=result,
                allowed_facts=allowed_facts(result),
                model_config_version=self.settings.model_config_version,
            )
        else:
            from agent.dev.payroll_pages import report_context
            from agent.services.facts import template_explanation

            entry = result_entry(case, result_id)
            result = AnalysisResult.model_validate(entry["analysis"])
            if case.get("stale") or case.get("dirty") or result.result_id != case["analysis"]["result_id"]:
                result.freshness = "stale"
            snapshot_sources = (entry.get("snapshot") or {}).get("authorized_sources", [])
            available = {
                s["source_id"]: SourceRef.model_validate(s)
                for s in snapshot_sources
                if s.get("kind", "document") == "document"
            }
            if not available:
                available = {
                    s["ref"]["source_id"]: SourceRef.model_validate(s["ref"])
                    for s in (entry.get("materials") or {}).get("sources", [])
                }
            if not available and ref is not None:
                available = {ref.source_id: ref}
            selected = list(available) if include_original else []
            if selected_source_ids is not None:
                if (
                    not isinstance(selected_source_ids, list)
                    or any(not isinstance(s, str) for s in selected_source_ids)
                    or len(selected_source_ids) != len(set(selected_source_ids))
                    or set(selected_source_ids) - set(available)
                ):
                    raise ComparisonInputError("이번 결과에 사용한 원본 문서만 선택해 주세요.")
                selected = selected_source_ids
            display = report_context(self, case, entry)
            payload = ReportInput(
                result=result,
                explanation=ExplanationCandidate.model_validate(entry["explanation"])
                if entry.get("explanation")
                else template_explanation(result),
                explanation_version=str(result.result_version),
                selected_sources=[available[s] for s in selected],
                display_context=display,
                template_version="report-v2" if display else "report-v1",
                generated_at=datetime.now(UTC),
            )
        task = AgentTask(context=context, payload=payload)
        # SecretStr dumps mask the execution token. It is deliberately regenerated on resume.
        job = {
            "id": jid,
            "case_id": case_id,
            "stage": stage,
            "document_type": case["document_type"],
            "status": "queued",
            "revision": case["revision"],
            "events": [],
            "error": None,
            "usage": {},
            "task": task.model_dump(mode="json"),
            "manual_sources": manual_sources,
            "provider_mode": self.settings.provider_mode,
            "origin": self.origin,
            "model_config_version": self.settings.model_config_version,
            "model": self.settings.extraction_model
            if stage in ("extract", "supplement")
            else "Python 규칙"
            if stage == "analyze"
            else "검수 문구·보고서 서식",
            "material_versions": copy.deepcopy((case.get("materials") or {}).get("source_versions", {})),
            "created_at": datetime.now(UTC).isoformat(),
        }
        if stage == "supplement":
            job.update(
                base_extraction_revision=case["extraction_a_revision"],
                ocr_result_id=case["ocr"]["id"],
                document_hash=ref.content_hash,
            )
        self.jobs[jid] = job
        case["jobs"].append(jid)
        self.save_case(case)
        self.save_job(job)
        self.tasks[jid] = asyncio.create_task(self._execute(job, task))
        return self.public_job(job)

    def _validate_ocr(self, case):
        result = case.get("ocr")
        ref = self.document_ref(case["document_id"])
        if not result:
            raise ComparisonInputError("OCR 추출 페이지에서 문자를 먼저 추출해 주세요.")
        if (
            result.get("document_id") != ref.document_id
            or result.get("document_version") != ref.document_version
            or result.get("content_hash") != ref.content_hash
        ):
            raise ComparisonInputError("문서가 변경되었습니다. OCR 추출을 다시 실행해 주세요.")
        self.document_data(case["document_id"])

    def _create_ocr_job(self, case):
        jid = identifier("job")
        job = {
            "id": jid,
            "case_id": case["id"],
            "stage": "ocr",
            "document_type": case["document_type"],
            "status": "queued",
            "revision": case["revision"],
            "events": [],
            "error": None,
            "usage": {},
            "call_budget": {},
            "manual_sources": [],
            "task": None,
            "provider_mode": "local_ocr",
            "origin": self.origin,
            "model": "Python OCR",
            "model_config_version": None,
            "ocr_fingerprint": self._ocr_fingerprint(),
            "source": self.document_ref(case["document_id"]).model_dump(mode="json"),
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.jobs[jid] = job
        case["jobs"].append(jid)
        self.save_case(case)
        self.save_job(job)
        self.tasks[jid] = asyncio.create_task(self._execute_ocr(job))
        return self.public_job(job)

    async def _execute_ocr(self, job):
        from agent.services.document_ocr import recognize_document

        case = self.cases[job["case_id"]]
        token = secrets.token_urlsafe(32)
        self.current_tokens[job["id"]] = token

        async def control():
            if self.current_tokens.get(job["id"]) != token or job["status"] == "cancelled":
                raise JobCancelled("OCR 작업이 취소되었습니다.")
            if case["revision"] != job["revision"]:
                raise IntegrityError("입력이 바뀌었습니다. OCR 추출을 새로 실행해 주세요.")

        try:
            async with self.semaphore:
                await control()
                job.update(status="running", error=None)
                job["events"].append({"node_id": "recognize_document", "status": "running"})
                self.save_job(job)
                ref = self.document_ref(case["document_id"])
                if ref.model_dump(mode="json") != job["source"]:
                    raise IntegrityError("OCR 작업과 문서 버전이 다릅니다.")
                data = self.document_data(case["document_id"])
                recognized = await recognize_document(data, self.settings, control=control)
                await control()
                clear_candidates(case, "ocr_replaced")
                case.update(
                    ocr={
                        **recognized,
                        "id": job["id"],
                        "document_id": ref.document_id,
                        "document_version": ref.document_version,
                        "content_hash": ref.content_hash,
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                    extraction=None,
                    confirmed=None,
                    confirmed_revision=None,
                    extraction_revision=case.get("extraction_revision", 0) + 1,
                    extraction_needs_review=False,
                    extraction_engine_version=None,
                    revision=case["revision"] + 1,
                    dirty=True,
                    stale=bool(case["analysis"]),
                    explanation=None,
                    report=None,
                )
                self.save_case(case)
                self._invalidate_material_dependents(case["id"])
                job["events"].append({"node_id": "recognize_document", "status": "completed"})
                job.update(status="completed", error=None)
        except asyncio.CancelledError, JobCancelled:
            job.update(status="cancelled", error="OCR 추출을 취소했습니다. 다시 실행할 수 있습니다.")
        except AgentError as exc:
            job.update(status="failed", error=str(exc), error_code=exc.code)
        except Exception:
            job.update(
                status="failed",
                error="OCR 자료를 처리하지 못했습니다. 문서와 OCR 설정을 확인해 주세요.",
                error_code="LOCAL_OCR_ERROR",
            )
        finally:
            self.save_job(job)

    async def resume_job(self, job_id):
        job = self.jobs[job_id]
        case = self._record(job["case_id"])
        self._refresh_materials(case)
        if case.get("kind") == "comparison" and job["stage"] in ("ocr", "extract", "supplement"):
            raise ComparisonInputError("비교 묶음에서는 원본 문서의 OCR·AI 추출을 실행할 수 없습니다.")
        if any(self.jobs[j]["status"] in ("queued", "running") for j in case["jobs"]):
            raise ValueError("현재 작업이 끝난 뒤 이어서 실행해 주세요.")
        if job["status"] not in ("interrupted", "cancelled", "failed"):
            raise ValueError("중단 또는 실패한 작업만 이어서 실행할 수 있습니다.")
        if case["revision"] != job["revision"]:
            raise ValueError("입력이 바뀌었습니다. 새 작업을 실행해 주세요.")
        if job["stage"] == "supplement":
            self._validate_ocr(case)
            try:
                validate_supplement_lineage(case, job)
                supplement_targets(
                    case,
                    [item["field_id"] for item in job["task"]["payload"]["target_fields"]],
                    case["extraction_revision"],
                )
            except ValueError as exc:
                raise ComparisonInputError(str(exc)) from exc
        if job["stage"] == "analyze":
            self._validate_comparison_context(case, Confirmation.model_validate(case["confirmed"]))
        if job["stage"] == "ocr":
            if job.get("ocr_fingerprint") != self._ocr_fingerprint():
                raise ValueError("OCR 설정이 바뀌었습니다. 새 작업을 실행해 주세요.")
            job.update(status="queued", error=None)
            job.pop("error_code", None)
            self.save_job(job)
            self.tasks[job_id] = asyncio.create_task(self._execute_ocr(job))
            return self.public_job(job)
        if (
            job["provider_mode"] != self.settings.provider_mode
            or job["model_config_version"] != self.settings.model_config_version
        ):
            raise ValueError("이전 작업과 모델 설정이 달라 이어서 실행할 수 없습니다.")
        task = AgentTask.model_validate(job["task"])
        task.context.execution_token = secrets.token_urlsafe(32)
        task.context.run_id = identifier("run")
        job["task"]["context"]["run_id"] = task.context.run_id
        job.update(status="queued", error=None)
        job.pop("error_code", None)
        self.save_job(job)
        self.tasks[job_id] = asyncio.create_task(self._execute(job, task))
        return self.public_job(job)

    async def cancel_job(self, job_id):
        job = self.jobs[job_id]
        if job["status"] not in ("queued", "running"):
            return self.public_job(job)
        if job_id in self.ports:
            self.ports[job_id].cancelled.add(job_id)
        self.tasks[job_id].cancel()
        job.update(
            status="cancelled",
            usage_may_be_incomplete=job["stage"] != "ocr",
            error="OCR 추출을 취소했습니다."
            if job["stage"] == "ocr"
            else "사용자가 작업을 취소했습니다. 이미 시작한 호출의 사용량은 발생할 수 있습니다.",
        )
        self.save_job(job)
        await asyncio.gather(self.tasks[job_id], return_exceptions=True)
        return self.public_job(job)

    async def _execute(self, job, task):
        ports = LocalPorts(self, job)
        self.ports[job["id"]] = ports
        ports.bind(task.context)
        case = self._record(job["case_id"])
        try:
            ref = self.document_ref(case["document_id"]) if case.get("document_id") else None
            if ref is not None:
                ports.add_source(ref, self.document_data(case["document_id"]))
            source_refs = (
                task.payload.snapshot.authorized_sources
                if job["stage"] == "analyze"
                else task.payload.selected_sources
                if job["stage"] == "report"
                else []
            )
            for linked_ref in source_refs:
                if linked_ref.kind == "document" and (ref is None or linked_ref.source_id != ref.source_id):
                    ports.add_source(linked_ref, self.document_data(linked_ref.document_id))
            for manual in job["manual_sources"]:
                ports.add_source(SourceRef.model_validate(manual["ref"]), manual["data"].encode())
            async with self.semaphore:
                job["status"] = "running"
                self.save_job(job)
                outcome = await run_task(task, ports=ports, settings=self.settings, provider=self.provider)
                if self.current_tokens.get(job["id"]) != task.context.execution_token.get_secret_value():
                    return
                if job["stage"] not in ("extract", "supplement"):
                    self._refresh_materials(case)
                if case["revision"] != job["revision"]:
                    job.update(
                        status="superseded", error="입력이 바뀌어 이전 작업 결과를 적용하지 않았습니다."
                    )
                elif outcome.status != "completed":
                    job.update(
                        status=outcome.status, error=outcome.error.message, error_code=outcome.error.code
                    )
                else:
                    self._apply(case, job, outcome.payload)
                    job["status"] = "completed"
                job["usage"] = outcome.usage
                self.save_case(case)
                self.save_job(job)
        except asyncio.CancelledError:
            job.update(
                status="cancelled",
                error="작업이 취소되었습니다. 이어서 실행하면 누적 호출 예산을 복구합니다.",
            )
            self.save_job(job)
        except Exception:
            job.update(
                status="failed",
                error="로컬 연결 작업에 실패했습니다. 원본·입력과 작업 상태를 확인해 주세요.",
                error_code="LOCAL_ADAPTER_ERROR",
            )
            self.save_job(job)
        finally:
            checkpoint = ports.checkpoints.get(job["id"])
            if (
                checkpoint
                and self.current_tokens.get(job["id"]) == task.context.execution_token.get_secret_value()
            ):
                job["call_budget"] = dict(checkpoint.call_budget)
                job["usage"] = dict(checkpoint.payload.get("__usage__", {}))
                self.save_job(job)
                if self.settings.provider_mode == "openai" and job.get("origin") == "local_console":
                    write_json(
                        self.state / "usage" / ("interactive-" + job["id"] + ".json"),
                        {
                            "usage": job["usage"],
                            "job_id": job["id"],
                            "status": job["status"],
                            "call_budget": job["call_budget"],
                            "model_calls": sum(
                                v for k, v in job["call_budget"].items() if not k.endswith(":format_failures")
                            ),
                            "usage_may_be_incomplete": bool(job.get("usage_may_be_incomplete"))
                            or job["status"] in ("cancelled", "interrupted"),
                        },
                    )

    def _apply(self, case, job, payload):
        stage = job["stage"]
        if stage == "supplement":
            entry = supplement_entry(case, job, payload)
            case["supplements"] = [item for item in case["supplements"] if item["id"] != entry["id"]] + [
                entry
            ]
        elif stage == "extract":
            if case["document_type"] == "auto":
                if payload.document_type not in ("contract", "attendance", "payslip", "deposit"):
                    raise ValueError(
                        "문서 유형을 분류하지 못했습니다. 문서를 종류별로 나누거나 유형을 직접 선택해 주세요."
                    )
                case["document_type"] = payload.document_type
                # Automatic classification resolves this extraction; it is not a manual type reset.
                case["document_type_revision"] = job.get("revision", case["revision"])
                job["resolved_document_type"] = payload.document_type
            elif payload.document_type != case["document_type"]:
                raise ValueError("추출 결과와 선택한 문서 유형이 다릅니다.")
            previous = case.get("extraction")
            candidate = extraction_draft(payload)
            baseline = case.get("extraction_draft") or (
                extraction_draft(previous) if previous else Confirmation().model_dump(mode="json")
            )
            if previous or case["draft"] != Confirmation().model_dump(mode="json"):
                case.setdefault("draft_history", []).append(
                    {
                        "revision": case["revision"],
                        "saved_at": datetime.now(UTC).isoformat(),
                        "reason": "before_reextraction",
                        "draft": copy.deepcopy(case["draft"]),
                    }
                )
            if case.get("materials"):
                # Connected data retains its own source versions; it is refreshed explicitly below.
                draft, changes = copy.deepcopy(case["draft"]), []
                for key in ("earnings_complete", "deductions_complete", "payment_scope_confirmed"):
                    draft[key] = False
            else:
                draft, changes = merge_extraction_draft(
                    case["draft"], baseline, candidate, case.get("confirmed")
                )
            clear_candidates(case, "base_extraction_replaced")
            case["extraction"] = payload.model_dump(mode="json")
            case["extraction_revision"] = case.get("extraction_revision", 0) + 1
            case.update(
                extraction_a=copy.deepcopy(case["extraction"]),
                extraction_a_revision=case["extraction_revision"],
                extraction_a_context={
                    "ocr_result_id": (case.get("ocr") or {}).get("id"),
                    "document_hash": self.documents[case["document_id"]]["sha256"],
                    "job_id": job.get("id"),
                    "run_id": ((job.get("task") or {}).get("context") or {}).get("run_id"),
                },
            )
            case.update(
                draft=draft,
                extraction_draft=candidate,
                extraction_engine_version=ENGINE_VERSION,
                extraction_compatibility_version=EXTRACTION_COMPATIBILITY_VERSION,
                extraction_needs_review=False,
                extraction_changes=changes,
                revision=case["revision"] + 1,
                dirty=True,
                stale=bool(case["analysis"]),
                explanation=None,
                report=None,
            )
            self._invalidate_material_dependents(case["id"])
        elif stage == "analyze":
            from agent.services.facts import template_explanation

            archive_current(case)
            history = case.setdefault("analysis_history", [])
            if case["analysis"] and not any(
                entry["analysis"]["result_id"] == case["analysis"]["result_id"] for entry in history
            ):
                history.append(
                    {
                        "analysis": copy.deepcopy(case["analysis"]),
                        "period": copy.deepcopy(case.get("analysis_period")),
                        "materials": copy.deepcopy(case.get("analysis_materials")),
                        "snapshot": copy.deepcopy(case.get("analysis_snapshot")),
                    }
                )
            snapshot = copy.deepcopy(job["task"]["payload"]["snapshot"])
            case["analysis_period"] = snapshot["period"]
            result = AnalysisResult(
                **payload.model_dump(),
                result_id=identifier("result"),
                result_version=case["confirmed_revision"],
                freshness="current",
                created_at=datetime.now(UTC),
            )
            case.update(
                analysis=result.model_dump(mode="json"),
                explanation=template_explanation(result).model_dump(mode="json"),
                report=None,
                stale=False,
            )
            case["analysis_materials"] = copy.deepcopy(case.get("confirmed_materials"))
            case["analysis_snapshot"] = snapshot
            case["analysis_confirmed"] = copy.deepcopy(case.get("confirmed"))
            identities = (case.get("confirmed") or {}).get("source_identity", {})
            names = {
                identities.get(s["case_id"], {}).get("employer_name")
                or s.get("identity", {}).get("employer_name")
                for s in (case.get("analysis_materials") or {}).get("sources", [])
            } - {None, ""}
            case["analysis_workplace_name"] = " · ".join(sorted(names)) or "사업장 미확인"
            history.append(
                {
                    "analysis": copy.deepcopy(case["analysis"]),
                    "period": copy.deepcopy(snapshot["period"]),
                    "materials": copy.deepcopy(case["analysis_materials"]),
                    "snapshot": snapshot,
                    "confirmed": copy.deepcopy(case["analysis_confirmed"]),
                    "workplace_name": case["analysis_workplace_name"],
                }
            )
            case["report_sources"] = sources_for_entry(self, result_entry(case))
            archive_current(case)
        elif stage == "explain":
            case.update(explanation=payload.model_dump(mode="json"), report=None)
            archive_current(case)
        else:
            report = payload.model_dump(mode="json")
            case.setdefault("report_history", []).append(copy.deepcopy(report))
            if case["analysis"]["result_id"] == payload.result_id:
                case["report"] = report
                archive_current(case)
            else:
                result_entry(case, payload.result_id)["report"] = report
            for artifact in payload.artifacts:
                self.jobs[job["id"]].setdefault("downloads", []).append(
                    {
                        "id": artifact.artifact_id,
                        "mime_type": artifact.mime_type,
                        "size_bytes": artifact.size_bytes,
                    }
                )

    async def artifact(self, artifact_id):
        for case in self._all_records():
            self._refresh_materials(case)
            reports = [
                case.get("report"),
                *case.get("report_history", []),
                *[entry.get("report") for entry in case.get("analysis_history", [])],
            ]
            for report in reports:
                for value in (report or {}).get("artifacts", []):
                    if value["artifact_id"] == artifact_id:
                        from agent.schemas.public.results import ArtifactRef

                        ref = ArtifactRef.model_validate(value)
                        ports = LocalFilePorts(directory=self.state / "artifacts-and-checkpoints")
                        return ports.artifact_bytes(ref), ref.mime_type
        raise KeyError("저장된 산출물을 찾을 수 없습니다.")
