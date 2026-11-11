import asyncio
import io
import json
import zipfile
from html import escape

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from agent.schemas.public.errors import IntegrityError
from agent.schemas.public.results import ReportCandidate
from agent.services.facts import COMPARISON_SIDES, STATUS_LABELS, WAGE_LABELS, format_value, wage_formula_text
from agent.services.glossary import GLOSSARY_VERSION, glossary_for_ids
from agent.services.hashing import content_hash
from agent.services.messages import RULE_LABELS
from agent.services.questions import condition_label, question_reason


def _operand_value(value):
    return {
        "true": "예",
        "false": "아니요",
        "weekly_rest_reference_for_eligible_dates": "조건을 충족한 날짜의 주휴 참고 금액",
    }.get(value, value)


def report_glossary(result):
    ids = {
        "gross",
        "deduction",
        "net",
        "ordinary_wage",
        "scheduled_hours",
        "paid_hours",
        "payroll_period",
        "payment_allocation",
        "subtotal",
    }
    for question in result.questions:
        ids.update(question.glossary_ids)
    return glossary_for_ids(ids)


def validate_display_context(result, context, template_version):
    if template_version not in {"report-v1", "report-v2"}:
        raise IntegrityError("Unsupported report template")
    if context is None:
        if template_version == "report-v2":
            raise IntegrityError("Report v2 requires a confirmed display context")
        return
    if context.snapshot_id != result.snapshot_id or context.snapshot_hash != result.snapshot_hash:
        raise IntegrityError("Report display context belongs to another snapshot")
    source_ids = [source.source_id for source in context.sources]
    if len(source_ids) != len(set(source_ids)):
        raise IntegrityError("Duplicate report source filename mapping")
    evidence_ids = {evidence.evidence_id for evidence in result.evidence_catalog}
    evidence_sources = {evidence.source_id for evidence in result.evidence_catalog}
    for source_id in source_ids:
        if source_id not in evidence_sources and not any(
            dependency.startswith(f"source:{source_id}:") for dependency in result.dependencies
        ):
            raise IntegrityError("Report filename refers to a source outside this result")
    if any(ref not in evidence_ids for condition in context.conditions for ref in condition.evidence_refs):
        raise IntegrityError("Report condition refers to evidence outside this result")


def render_pdf(result, explanation, settings, generated_at, display_context=None):
    if not settings.font_path.is_file():
        raise IntegrityError("A licensed Hangul TrueType font must be configured")
    # Registering happens only when generating a PDF, never on import.
    font_name = "AgentHangul-" + content_hash(str(settings.font_path.resolve()).encode())[:10]
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, str(settings.font_path)))
    body = ParagraphStyle("body", fontName=font_name, fontSize=9, leading=15, wordWrap="CJK")
    heading = ParagraphStyle(
        "heading", parent=body, fontSize=20, leading=28, textColor=colors.HexColor("#174f42")
    )
    section = ParagraphStyle(
        "section",
        parent=body,
        fontSize=13,
        leading=20,
        spaceBefore=14,
        spaceAfter=7,
        keepWithNext=True,
        textColor=colors.HexColor("#174f42"),
    )
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=40)

    def p(text):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), body)

    def title(text):
        return Paragraph(escape(text), section)

    def table(rows, widths):
        output = LongTable([[p(value) for value in row] for row in rows], colWidths=widths, repeatRows=1)
        output.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2eee8")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#bdccc3")),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        return output

    source_names = (
        {source.source_id: source.filename for source in display_context.sources} if display_context else {}
    )
    evidence = {ev.evidence_id: ev for ev in result.evidence_catalog}

    def sources(refs):
        labels = []
        for identifier in refs:
            ev = evidence.get(identifier)
            if ev is not None:
                if ev.page is None and ev.verification_method == "manual_confirmed":
                    labels.append("사용자가 확인한 입력")
                    continue
                label = source_names.get(ev.source_id, ev.source_id)
                labels.append(f"{label} {ev.page}쪽" if ev.page else f"{label} (수기 확인)")
        return "; ".join(dict.fromkeys(labels)) or "연결된 출처 없음"

    story = [
        Paragraph("월급잇다 상담 자료", heading),
        Spacer(1, 12),
        p(
            f"결과 {result.result_id} / 버전 {result.result_version} / {'최신 결과' if result.freshness == 'current' else '이전 입력 결과'}"
        ),
        p(f"생성 기준 시각 {generated_at.isoformat()}"),
        p(
            "사용자가 확인한 자료와 지원하는 표준 근로조건에 따른 계산·대조 자료입니다. 차이만으로 미지급이나 법 위반을 확정하지 않습니다."
        ),
        Spacer(1, 12),
    ]
    if display_context:
        story.extend(
            [
                p(f"사업장: {display_context.workplace_name or '이름 미확인'}"),
                p(f"급여기간: {display_context.period.start} ~ {display_context.period.end}"),
                title("1. 확인한 입력과 조건"),
            ]
        )
        states = {
            "known": "확인됨",
            "missing": "자료 없음",
            "unknown": "모름",
            "pending": "확인 예정",
            "conflict": "자료 간 충돌",
        }
        for condition in display_context.conditions:
            value = condition.value if condition.state == "known" else "미확정"
            if condition.label.startswith("계산·명세서 대응 범위:") and " / 명세서 항목: " in value:
                period, _, identifiers = value.partition(" / 명세서 항목: ")
                count = len([item for item in identifiers.split(",") if item.strip()])
                value = f"{period} / 선택한 명세서 항목 {count}개"
            story.append(p(f"{condition_label(condition.label)}: {value} [{states[condition.state]}]"))
            if condition.evidence_refs:
                story.append(p("근거: " + sources(condition.evidence_refs)))
        if not display_context.conditions:
            story.append(p("표시할 확인 조건이 없습니다."))
    else:
        story.append(p("이전 보고서 입력: 사업장 이름·급여기간·조건의 표시 정보가 함께 저장되지 않았습니다."))
    story.append(title("2. 결과 설명"))
    story.extend(p(text) for text in explanation.paragraphs)
    story.append(title("3. 임금 항목별 계산과 포함 범위"))
    wages = result.wage_section
    rows = [["항목", "상태", "산정액", "이미 포함된 금액", "총액에 더한 금액"]]
    for item in wages.items:
        rows.append(
            [
                WAGE_LABELS.get(item.rule_id, item.rule_id),
                STATUS_LABELS[item.status],
                format_value(item.amount),
                format_value(item.included_amount),
                format_value(item.payable_amount),
            ]
        )
    if len(rows) > 1:
        story.append(table(rows, [100, 82, 110, 110, A4[0] - 474]))
    story.append(
        p(
            f"계산 소계: {format_value(wages.subtotal)}"
            if any(item.status == "calculated" for item in wages.items)
            else "계산 소계: 아직 계산을 완료한 임금 항목이 없습니다."
        )
    )
    story.append(
        p(
            f"전체 지급총액: {format_value(wages.gross_total) if wages.scope_complete else '미확정 (보류·범위 확인 필요)'}"
        )
    )
    story.append(
        p(
            "산정액과 총액에 더한 금액은 다를 수 있습니다. 월급에 이미 포함된 수당을 중복 합산하지 않습니다. 미확정 항목을 0원으로 처리하지 않습니다."
        )
    )
    for item in wages.items:
        story.append(p(f"{WAGE_LABELS.get(item.rule_id, item.rule_id)} 계산식: {wage_formula_text(item)}"))
        if item.status == "calculated" and item.rule_version == "standard-2026-v1":
            story.append(p("항목의 마지막 금액에서 원 단위로 반올림했습니다."))
        if item.operands:
            story.append(
                p(
                    "사용한 값: "
                    + ", ".join(
                        f"{condition_label(key)}={_operand_value(value)}"
                        for key, value in item.operands.items()
                    )
                )
            )
        if item.reason_codes:
            story.append(p("범위·사유: " + " ".join(question_reason(code) for code in item.reason_codes)))
        for dates in item.required_periods:
            story.append(p(f"추가 확인 기간: {dates.start} ~ {dates.end}"))
        story.append(p("근거: " + sources(item.evidence_refs)))
        for url in item.official_sources:
            story.append(p("계산 기준 출처: " + url))
    story.append(title("4. 자료별 대조"))
    rows = [["항목", "상태", "왼쪽 값", "오른쪽 값", "차이", "범위·사유"]]
    for item in result.comparisons:
        rows.append(
            [
                RULE_LABELS.get(item.rule_id, item.rule_id),
                STATUS_LABELS[item.status],
                format_value(item.lhs, item.unit),
                format_value(item.rhs, item.unit),
                "비교 보류" if item.delta is None else format_value(item.delta, item.unit),
                ("전체 범위 확인. " if item.scope_complete else "확인된 일부 범위. ")
                + " ".join(question_reason(code) for code in item.reason_codes),
            ]
        )
    story.append(table(rows, [58, 65, 80, 80, 75, A4[0] - 430]))
    for item in result.comparisons:
        left, right = COMPARISON_SIDES.get(item.rule_id, ("왼쪽 값", "오른쪽 값"))
        story.append(
            p(
                f"{RULE_LABELS.get(item.rule_id, item.rule_id)}: 왼쪽은 {left}, 오른쪽은 {right}. 근거: {sources(item.evidence_refs)}"
            )
        )
        for component in item.wage_items:
            story.append(
                p(
                    f"대응 항목 {', '.join(WAGE_LABELS.get(rule, rule) for rule in component.rule_ids)} / 선택한 명세서 항목 {len(component.statement_item_ids)}개: "
                    f"계산 {format_value(component.lhs)}, 명세서 {format_value(component.rhs)}, "
                    f"차이 {format_value(component.delta)}"
                )
            )
            if not component.independent:
                story.append(
                    p(
                        "계산 근거와 명세서 항목이 같은 자료에 연결되어 있어 독립된 자료로 검증한 결과는 아닙니다."
                    )
                )
    story.append(title("5. 보완 질문"))
    if not result.questions:
        story.append(p("현재 결과에 연결된 보완 질문이 없습니다."))
    for question in result.questions:
        story.append(p(question.text))
        story.append(p("확인이 필요한 이유: " + (question.reason or question_reason(question.reason_code))))
        story.append(
            p(
                "영향받는 항목: "
                + ", ".join(
                    WAGE_LABELS.get(rule, RULE_LABELS.get(rule, rule)) for rule in question.blocking_rule_ids
                )
            )
        )
        if question.required_materials:
            story.append(p("필요한 자료: " + ", ".join(question.required_materials)))
        for dates in question.required_periods:
            story.append(p(f"필요한 기간: {dates.start} ~ {dates.end}"))
    story.append(title("6. 출처 파일과 원문 위치"))
    for ev in result.evidence_catalog:
        story.append(p(f"{sources([ev.evidence_id])} / {ev.evidence_id[:12]}"))
        if ev.quote:
            story.append(p(ev.quote))
    story.append(title("7. 쉬운 용어"))
    story.append(p(f"용어 사전 {GLOSSARY_VERSION}. 아래 예시는 실제 사용자 금액이 아닙니다."))
    for entry in report_glossary(result):
        story.append(p(entry["term"] + ": " + entry["explanation"]))
        story.append(p(entry["example"]))
        story.append(p("구분: " + entry["distinction"]))
        for source in entry["sources"]:
            story.append(p("정의 확인: " + source["title"] + " " + source["url"]))
        story.append(Spacer(1, 7))

    def footer(canvas, _):
        canvas.setFont(font_name, 8)
        canvas.drawRightString(A4[0] - 36, 22, f"월급잇다 | {canvas.getPageNumber()}")

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    data = buffer.getvalue()
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if not reader.pages or "월급잇다" not in text or result.result_id not in text:
        raise IntegrityError("Generated PDF failed content validation")
    return data


async def build_report(runtime):
    payload = runtime.task.payload
    result, explanation = payload.result, payload.explanation
    if (
        explanation.result_id != result.result_id
        or explanation.result_version != result.result_version
        or explanation.result_hash != result.result_hash
    ):
        raise IntegrityError("Explanation belongs to another result")
    validate_display_context(result, payload.display_context, payload.template_version)
    if len({s.source_id for s in payload.selected_sources}) != len(payload.selected_sources):
        raise IntegrityError("Duplicate selected source")
    originals = {}
    source_manifest = []
    for index, source in enumerate(payload.selected_sources, 1):
        if source.workplace_id != result.workplace_id:
            raise IntegrityError("Selected original belongs to another workplace")
        dependency = f"source:{source.source_id}:{source.document_version}:{source.link_version}"
        if dependency not in result.dependencies:
            raise IntegrityError("Selected source version is not in result")
        await runtime.control()
        content = await runtime.ports.read_source(runtime.context, source)
        if (
            content.document_version != source.document_version
            or content.content_hash != source.content_hash
            or content_hash(content.data) != source.content_hash
        ):
            raise IntegrityError("Original source changed")
        if len(content.data) > runtime.settings.max_file_bytes or content.mime_type != "application/pdf":
            raise IntegrityError("Selected original exceeds limits or is not PDF")
        original_reader = getattr(runtime.ports, "read_original", None)
        original = await original_reader(runtime.context, source) if callable(original_reader) else content
        extensions = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}
        if (
            original.document_version != source.document_version
            or content_hash(original.data) != original.content_hash
            or original.mime_type not in extensions
            or len(original.data) > runtime.settings.max_file_bytes
        ):
            raise IntegrityError("Uploaded original metadata or bytes changed")
        archive_name = f"originals/{index:03d}-{content_hash(source.source_id.encode())[:12]}{extensions[original.mime_type]}"
        originals[archive_name] = original.data
        source_manifest.append({
            "source_id": source.source_id, "document_id": source.document_id,
            "document_version": source.document_version, "link_version": source.link_version,
            "processing_pdf_hash": source.content_hash, "original_hash": original.content_hash,
            "original_mime_type": original.mime_type, "archive_name": archive_name,
        })
    await runtime.control()
    pdf = await asyncio.to_thread(
        render_pdf, result, explanation, runtime.settings, payload.generated_at, payload.display_context
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("summary.pdf", pdf)
        archive.writestr("result.json", result.model_dump_json(indent=2))
        archive.writestr("explanation.json", explanation.model_dump_json(indent=2))
        archive.writestr(
            "report_context.json",
            json.dumps(
                {
                    "template_version": payload.template_version,
                    "generated_at": payload.generated_at.isoformat(),
                    "display_context": payload.display_context.model_dump(mode="json")
                    if payload.display_context
                    else None,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        archive.writestr(
            "glossary.json",
            json.dumps(
                {"version": GLOSSARY_VERSION, "entries": report_glossary(result)},
                ensure_ascii=False,
                indent=2,
            ),
        )
        archive.writestr(
            "manifest.json", json.dumps({name: content_hash(data) for name, data in originals.items()})
        )
        archive.writestr("source_manifest.json", json.dumps(source_manifest, ensure_ascii=False, indent=2))
        for name, data in originals.items():
            archive.writestr(name, data)
    zip_data = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(zip_data)) as archive:
        if archive.testzip() is not None or archive.read("summary.pdf") != pdf:
            raise IntegrityError("Generated ZIP failed validation")
    await runtime.control()
    pdf_ref = await runtime.ports.stage_artifact(runtime.context, pdf, "application/pdf")
    zip_ref = await runtime.ports.stage_artifact(runtime.context, zip_data, "application/zip")
    for ref, data, mime in [(pdf_ref, pdf, "application/pdf"), (zip_ref, zip_data, "application/zip")]:
        if ref.content_hash != content_hash(data) or ref.size_bytes != len(data) or ref.mime_type != mime:
            raise IntegrityError("Staged artifact metadata mismatch")
    return ReportCandidate(
        result_id=result.result_id,
        result_version=result.result_version,
        explanation_version=payload.explanation_version,
        artifacts=[pdf_ref, zip_ref],
        included_source_ids=[s.source_id for s in payload.selected_sources],
    ).model_dump(mode="json")
