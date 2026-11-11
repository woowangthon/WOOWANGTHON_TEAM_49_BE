import io
from datetime import UTC, datetime
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas

from agent.config import AgentSettings
from agent.schemas.public.base import Period
from agent.schemas.public.inputs import (
    ConfirmedField,
    EvidenceRef,
    InputSnapshot,
    SourceRef,
    StatementItem,
    TransactionAllocation,
)
from agent.schemas.public.results import AnalysisResult
from agent.schemas.public.tasks import AgentTask, AnalysisJobInput, ExplanationInput, JobContext
from agent.services.facts import allowed_facts
from agent.services.hashing import content_hash, snapshot_hash


def pdf_bytes(lines, *, scanned=False, font_path=None):
    font_path = font_path or Path(__file__).resolve().parents[2] / "assets/fonts/NotoSansKR-Regular.ttf"
    buffer = io.BytesIO()
    # Stable metadata/document IDs are essential for reproducible checkpoint fingerprints.
    canvas = Canvas(buffer, pagesize=(595, 842), invariant=1)
    if scanned:
        from PIL import Image, ImageDraw, ImageFont
        from reportlab.lib.utils import ImageReader

        image = Image.new("RGB", (1190, 1684), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(font_path), 28)
        for i, line in enumerate(lines):
            draw.text((100, 120 + i * 60), line, font=font, fill="black")
        canvas.drawImage(ImageReader(image), 0, 0, width=595, height=842)
    else:
        canvas.setFont("Helvetica", 12)
        for i, line in enumerate(lines):
            canvas.drawString(50, 780 - i * 25, line)
    canvas.save()
    return buffer.getvalue()


def source(data, *, source_id="source-demo", status="active"):
    return SourceRef(
        source_id=source_id,
        owner_id="owner-demo",
        workplace_id="workplace-demo",
        link_id="link-" + source_id,
        link_version=1,
        document_id="doc-" + source_id,
        document_version=1,
        content_hash=content_hash(data),
        status=status,
    )


def context(kind, *, job_id=None, run_id="run-1", token="fixture-execution-token"):
    return JobContext(
        job_id=job_id or "demo-" + kind,
        run_id=run_id,
        execution_token=token,
        owner_id="owner-demo",
        case_id="case-demo",
        task_kind=kind,
        idempotency_key="fixture-request-" + kind,
        requested_at=datetime(2026, 11, 4, tzinfo=UTC),
    )


def analysis_fixture():
    data = pdf_bytes(
        [
            "SYNTHETIC PAYROLL - NOT A REAL PERSON",
            "contract_rate: 10000 KRW/hour",
            "slip_rate: 10000 KRW/hour",
            "worked_minutes: 12000 minute",
            "gross: 2000000 KRW",
            "deduction: 200000 KRW",
            "net: 1800000 KRW",
        ]
    )
    ref = source(data)
    period = Period(start="2026-10-01", end="2026-10-31")
    ev = EvidenceRef(
        evidence_id="evidence-demo",
        source_id=ref.source_id,
        page=1,
        quote="SYNTHETIC PAYROLL - NOT A REAL PERSON",
        verification_method="manual_confirmed",
    )
    values = {
        "contract_rate": ("10000", "KRW/hour"),
        "slip_rate": ("10000", "KRW/hour"),
        "worked_minutes": ("12000", "minute"),
        "slip_minutes": ("12000", "minute"),
        "gross": ("2000000", "KRW"),
        "deduction": ("200000", "KRW"),
        "net": ("1800000", "KRW"),
    }
    fields = [
        ConfirmedField(
            field_id=key,
            value=value,
            unit=unit,
            confirmation_id="confirmed-" + key,
            evidence_refs=[ev.evidence_id],
            period=period,
            time_kind="actual" if unit == "minute" else None,
        )
        for key, (value, unit) in values.items()
    ]
    snapshot = InputSnapshot(
        snapshot_id="snapshot-demo",
        input_version=1,
        owner_id=ref.owner_id,
        workplace_id=ref.workplace_id,
        case_id="case-demo",
        period=period,
        confirmed_fields=fields,
        authorized_sources=[ref],
        evidence_catalog=[ev],
        earnings=[StatementItem(item_id="earning-1", amount="2000000", evidence_refs=[ev.evidence_id])],
        deductions=[StatementItem(item_id="deduction-1", amount="200000", evidence_refs=[ev.evidence_id])],
        earnings_complete=True,
        deductions_complete=True,
        transaction_allocations=[
            TransactionAllocation(
                transaction_id="transaction-demo",
                amount="1800000",
                transaction_amount="1800000",
                evidence_refs=[ev.evidence_id],
            )
        ],
        payment_scope_confirmed=True,
    )
    snapshot.snapshot_hash = snapshot_hash(snapshot)
    task = AgentTask(
        context=context("analyze_case"),
        payload=AnalysisJobInput(
            snapshot=snapshot, snapshot_id=snapshot.snapshot_id, snapshot_hash=snapshot.snapshot_hash
        ),
    )
    return task, ref, data


def confirmed_result(candidate):
    return AnalysisResult(
        **candidate.model_dump(),
        result_id="result-demo",
        result_version=1,
        freshness="current",
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
    )


def explanation_task(result, *, job_id=None):
    return AgentTask(
        context=context("explain_result", job_id=job_id),
        payload=ExplanationInput(result=result, allowed_facts=allowed_facts(result), generation_mode="model"),
    )


def settings(**overrides):
    return AgentSettings(**overrides)
