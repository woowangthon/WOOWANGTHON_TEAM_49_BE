"""Filled synthetic contracts from existing local JSON and HWP templates.

No API calls. Original materials and the existing 37-document campaign are read-only.
"""

import argparse
import html
import io
import re
import shutil
import subprocess
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from agent.services.hashing import content_hash
from agent.tests.support.contract_corpus import (
    FONT,
    ROOT,
    block_text,
    hwp_images,
    image_pdf,
    ordered_blocks,
    write_json,
)

OUTPUT = ROOT / "output/pdf/filled-contracts"
GREEN = colors.HexColor("#174f42")
INK = colors.HexColor("#173f69")
SAMPLES = {
    "hourly": {
        "rate": "15000",
        "unit": "KRW/hour",
        "start": "2026-10-01",
        "end": "2026-10-31",
        "employee": "가상근로자 가온",
        "term_months": 1,
        "title": "시급제 근로계약서",
    },
    "monthly": {
        "rate": "3000000",
        "unit": "KRW/month",
        "start": "2026-10-01",
        "end": "2027-09-30",
        "employee": "가상근로자 나래",
        "term_months": 12,
        "title": "월급제 근로계약서",
    },
}


def korean_date(iso):
    year, month, day = iso.split("-")
    return f"{year}년 {month}월 {day}일"


def register_fonts():
    pdfmetrics.registerFont(TTFont("FilledKR", str(FONT)))
    bold = FONT.with_name("malgunbd.ttf")
    pdfmetrics.registerFont(TTFont("FilledKRBold", str(bold if bold.is_file() else FONT)))


class TableReader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag in ("td", "th"):
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        if tag == "tr":
            self.rows.append(self.row)


def source_json(filename):
    base = (
        ROOT
        / "계약_법률_문서_서식_데이터/3.개방데이터/1.데이터/Validation/01.원천데이터/VS_인사_고용_노무_고용계약"
    )
    path = base / filename
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def replacements(variant):
    sample = SAMPLES[variant]
    start, end, employee = korean_date(sample["start"]), korean_date(sample["end"]), sample["employee"]
    parties = (
        "<table><tr><td>사용자</td><td>가상사업장 월급테스트</td><td>대표자</td><td>테스트대표</td></tr>"
        "<tr><td>사업장</td><td>서울특별시 가상구 샘플로 101, 2층 (가상 주소)</td><td>연락처</td><td>TEST-PHONE-001</td></tr>"
        f"<tr><td>근로자</td><td>{employee}</td><td>생년월일</td><td>1995-01-01 (가상)</td></tr>"
        "<tr><td>주 소</td><td>서울특별시 가상구 테스트로 202 (가상 주소)</td><td>서명</td><td>테스트용 서명 생략</td></tr></table>"
    )
    if variant == "hourly":
        return {
            1: "기간제 근로계약서 (시급제)",
            54: f"가상사업장 월급테스트(이하 ‘회사’)와 {employee}(이하 ‘사원’)은 다음과 같이 근로계약을 체결한다.",
            6: f"① 근로계약기간은 {start}부터 {end}까지 1개월로 한다.",
            9: "① ‘사원’의 근무 장소: 서울특별시 가상구 샘플로 101, 2층 테스트매장",
            10: "② ‘사원’의 업무 내용: 매장 상품 진열, 주문 안내 및 재고 정리",
            13: "① 근무일은 월요일부터 금요일까지 주 5일이다. 소정근로시간은 1일 8시간, 1주 40시간으로 한다.",
            14: "<table><tr><td>구분</td><td>월요일</td><td>화요일</td><td>수요일</td><td>목요일</td><td>금요일</td></tr>"
            "<tr><td>근무시각</td>" + "<td>09:00-18:00</td>" * 5 + "</tr>"
            "<tr><td>휴게시각</td>" + "<td>12:00-13:00</td>" * 5 + "</tr></table>",
            15: "② 휴게시간은 매일 12:00부터 13:00까지 60분으로 하며, 근로시간에 포함하지 않는다.",
            23: "① 임금 형태는 시급제이며 기본시급은 15,000원으로 한다. 임금은 실제로 확인된 근로시간에 따라 산정한다.",
            26: "③ 임금계산기간은 매월 1일부터 말일까지이며 임금 지급일은 매월 25일로 한다. 근로자 명의의 계좌로 지급하고 임금명세서를 교부한다.",
            50: "2026년 09월 25일",
            52: parties,
        }
    return {
        27: "촉탁근로 계약서 (월급제)",
        1: "아래 가상 당사자는 다음의 조건으로 근로계약서를 작성한다.",
        3: parties,
        4: "2. 촉탁 계약내용: 테스트매장 상품 관리 및 고객 주문 안내",
        6: "직종: 매장 운영 / 직위: 운영사원 / 근무장소: 서울특별시 가상구 샘플로 101, 2층 테스트매장",
        9: "근무일은 월요일부터 금요일까지 주 5일이며 1일 8시간, 1주 40시간으로 한다.\n"
        "출근 09:00, 퇴근 18:00. 휴게시간은 12:00-13:00이며 1일 60분이다.",
        11: "임금 형태: 월급제\n월급: 3,000,000원\n기본급: 월 3,000,000원\n고정수당: 없음 (0원) / 정기상여금: 없음 (0원)\n"
        "월급의 계산기간은 매월 1일부터 말일까지이며 임금 지급일은 매월 25일이다.\n"
        "지급방법: 근로자 명의 계좌 입금. 연장·야간·휴일근로 수당은 발생 시 별도로 구분한다.",
        16: f"근로계약기간: {start}부터 {end}까지 1년.\n수습기간은 두지 않는다.",
        28: "근무일은 월요일-금요일, 주휴일은 일요일로 정하며 토요일은 근무하지 않는다. 그 밖의 근로조건은 별도 확인된 취업규칙에 따른다.",
        29: "계약 변경 및 재계약 여부는 당사자가 서면으로 확인한다.",
        21: "2026년 09월 25일",
        23: "사용자: 가상사업장 월급테스트 / 대표 테스트대표 / 서명 생략 (테스트용)",
        25: f"근로자: {employee} / 서명 생략 (테스트용)",
    }


def render_text_contract(source, variant, path):
    body = ParagraphStyle(
        "contract-body", fontName="FilledKR", fontSize=9.2, leading=15.2, wordWrap="CJK", spaceAfter=7
    )
    heading = ParagraphStyle(
        "contract-heading",
        parent=body,
        fontName="FilledKRBold",
        fontSize=10.8,
        leading=17,
        spaceBefore=7,
        keepWithNext=True,
        textColor=GREEN,
    )
    title = ParagraphStyle("contract-title", parent=heading, fontSize=21, leading=30, spaceAfter=16)
    cell = ParagraphStyle("contract-cell", parent=body, fontSize=8, leading=12, spaceAfter=0)
    changed, story, mapping = replacements(variant), [], []

    def paragraph(text, style=body):
        return Paragraph(html.escape(text).replace("\n", "<br/>"), style)

    for index, block in enumerate(ordered_blocks(source)):
        original = block_text(block)
        text = changed.get(block["id"], original)
        # The source puts the boilerplate body immediately before its section title.
        # Preserve source order and record the original block IDs in the sidecar.
        mapping.append(
            {
                "source_paragraph_id": block["id"],
                "source_text": original,
                "filled_text": text,
                "changed": text != original,
            }
        )
        if "<table" in text:
            parsed = TableReader()
            parsed.feed(text)
            columns = max(len(row) for row in parsed.rows)
            rows = [
                [paragraph(value, cell) for value in row + [""] * (columns - len(row))] for row in parsed.rows
            ]
            widths = [55, 177, 48, 243] if columns == 4 else [523 / columns] * columns
            table = Table(rows, colWidths=widths, hAlign="LEFT", repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d3ca")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf3ed")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.extend([table, Spacer(1, 9)])
        else:
            style = (
                title
                if index == 0
                else heading
                if re.match(r"(?:제\s*\d+\s*조|\d+\.)", text) and len(text) < 60
                else body
            )
            story.append(paragraph(text, style))

    def page_frame(canvas, doc):
        canvas.saveState()
        canvas.setFont("FilledKR", 8)
        canvas.setFillColor(GREEN)
        canvas.drawString(36, A4[1] - 27, "가상 작성본 · 에이전트 테스트용 · 실제 계약 및 서명 아님")
        canvas.setStrokeColor(colors.HexColor("#cfdbce"))
        canvas.line(36, A4[1] - 35, A4[0] - 36, A4[1] - 35)
        canvas.setFillColor(colors.HexColor("#687b6b"))
        canvas.setFont("FilledKR", 7)
        canvas.drawString(36, 22, f"기존 JSON 양식 재구성 / {source.name} / 가상 작성값")
        canvas.drawRightString(A4[0] - 36, 22, str(doc.page))
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=52,
        bottomMargin=42,
        title=SAMPLES[variant]["title"],
        author="월급잇다 테스트 자료",
        invariant=1,
    )
    doc.build(story, onFirstPage=page_frame, onLaterPages=page_frame)
    return mapping


def render_filled_standard(images, variant, path):
    """Fill the actual fields/sections of the original bilingual standard form."""
    sample = SAMPLES[variant]
    canvas = Canvas(str(path), invariant=1)
    field_map = []
    for page_number, image in enumerate(images, 1):
        width, height = image.size
        canvas.setPageSize((595, 595 * height / width))
        canvas.saveState()
        canvas.scale(595 / width, 595 / width)
        canvas.drawImage(ImageReader(image), 0, 0, width=width, height=height)

        def field(name, text, x, top, maximum_width, size=17, *, white_height=None):
            if white_height:
                canvas.setFillColor(colors.white)
                canvas.rect(
                    x - 2, height - top - white_height, maximum_width + 4, white_height + 2, fill=1, stroke=0
                )
            while pdfmetrics.stringWidth(text, "FilledKR", size) > maximum_width and size > 10:
                size -= 0.5
            if pdfmetrics.stringWidth(text, "FilledKR", size) > maximum_width:
                raise ValueError("Form text exceeds input box: " + name)
            canvas.setFont("FilledKR", size)
            canvas.setFillColor(INK)
            canvas.drawString(x, height - top - size, text)
            field_map.append(
                {
                    "name": name,
                    "page": page_number,
                    "text": text,
                    "box_pixels": [x, top, maximum_width, white_height or size + 3],
                    "font_pixels": size,
                }
            )

        field("test_mark", "가상 작성본 / SYNTHETIC TEST ONLY / 실제 서명 아님", 75, 30, 1035, 19)
        if page_number == 1:
            field("employer_company", "가상사업장 월급테스트", 230, 286, 420)
            field("phone", "TEST-PHONE-001 (가상)", 675, 286, 420)
            field("employer_address", "서울특별시 가상구 샘플로 101, 2층 (가상 주소)", 230, 345, 860)
            field("employer_name", "테스트대표", 230, 405, 420)
            field("company_number", "TEST-COMPANY-001 (가상)", 670, 412, 430, 14, white_height=22)
            field("employee_name", sample["employee"], 230, 466, 420)
            field("employee_birth", "1995-01-01 (가상)", 675, 466, 420)
            field("employee_address", "서울특별시 가상구 테스트로 202 (가상 주소)", 230, 524, 860)
            term = f"신규 근로자: {sample['term_months']}개월, {korean_date(sample['start'])} ~ {korean_date(sample['end'])}"
            field("contract_period_ko", term, 242, 560, 858, 16.5, white_height=23)
            field("probation_ko", "- 수습기간: 미활용 (수습기간 없음)", 242, 603, 858, 16, white_height=23)
            field(
                "contract_period_en",
                f"New employee: {sample['term_months']} month(s), from {sample['start']} to {sample['end']}",
                242,
                668,
                858,
                16,
                white_height=23,
            )
            field(
                "probation_en",
                "- Probation period: Not included (no probation)",
                242,
                708,
                858,
                16,
                white_height=48,
            )
            field("workplace_ko", "서울특별시 가상구 샘플로 101, 2층 테스트매장", 246, 813, 850, 16)
            field(
                "workplace_en",
                "Test store, 101 Sample-ro, Fictional district, Seoul (synthetic)",
                246,
                866,
                850,
                15,
            )
            field("industry_ko", "서비스업", 317, 915, 760, 16)
            field("business_ko", "소매 매장 운영", 355, 940, 720, 16)
            field("job_ko", "상품 진열, 주문 안내 및 재고 정리", 355, 966, 720, 16)
            field("industry_en", "Services", 335, 1000, 740, 15)
            field("business_en", "Retail store operations", 441, 1025, 635, 15)
            field("job_en", "Stock display, order assistance and inventory checks", 408, 1051, 668, 15)
            field("working_hours_ko", "09시 00분 ~ 18시 00분", 245, 1110, 595, 18, white_height=30)
            field(
                "daily_overtime_ko", "- 1일 평균 시간외근로시간: 0시간", 245, 1142, 595, 16, white_height=26
            )
            field("schedule_ko", "월-금 주 5일 / 소정근로시간: 1일 8시간, 1주 40시간", 248, 1220, 600, 15.5)
            field("work_start_en", "09:00", 307, 1282, 70)
            field("work_end_en", "18:00", 450, 1282, 70)
            field("daily_overtime_en", "0", 487, 1310, 25)
            field("schedule_en", "Mon-Fri / 8 hours per day, 40 hours per week", 248, 1418, 600, 15)
            field("break_ko", "60", 549, 1454, 40)
            field("break_en", "60", 470, 1485, 35)
        else:
            field("holiday_ko", "X", 253, 82, 15)
            field("holiday_en", "X", 252, 158, 15)
            # Replace only the editable wage lines, preserving section borders and the additional-pay clause.
            canvas.setFillColor(colors.white)
            canvas.rect(238, height - 343, 865, 110, fill=1, stroke=0)
            monthly = "월 3,000,000원" if variant == "monthly" else "미산정 (시급제)"
            wage = "월급 3,000,000원" if variant == "monthly" else "시급 15,000원"
            for i, text in enumerate(
                [
                    f"1) 월 통상임금: {monthly}",
                    f"- 기본급: {wage}",
                    "- 고정적 수당: 없음 (0원)",
                    "- 상여금: 없음 (0원)",
                    "- 수습기간: 미활용 / 수습 감액 없음",
                ]
            ):
                field(f"wage_ko_{i}", text, 245, 237 + i * 21, 850, 16)
            canvas.setFillColor(colors.white)
            canvas.rect(238, height - 482, 865, 109, fill=1, stroke=0)
            english_rate = "KRW 3,000,000 per month" if variant == "monthly" else "KRW 15,000 per hour"
            english_total = "KRW 3,000,000" if variant == "monthly" else "Not specified (hourly wage)"
            for i, text in enumerate(
                [
                    f"1) Monthly normal wages: {english_total}",
                    f"- Basic pay: {english_rate}",
                    "- Fixed allowances: none (KRW 0)",
                    "- Bonus: none (KRW 0)",
                    "- Probation: not included; no wage reduction",
                ]
            ):
                field(f"wage_en_{i}", text, 245, 378 + i * 20, 850, 16)
            field(
                "payday_ko",
                "매월 25일 지급. 지급일이 공휴일이면 전날 지급한다.",
                242,
                508,
                858,
                16,
                white_height=25,
            )
            field(
                "payday_en",
                "On the 25th of every month; if a public holiday, payment is made one day earlier.",
                242,
                537,
                858,
                16,
                white_height=43,
            )
            field("deposit_ko", "X", 404, 581, 15)
            field("deposit_en", "X", 401, 628, 15)
            canvas.setFillColor(colors.white)
            canvas.rect(238, height - 849, 865, 156, fill=1, stroke=0)
            for name, text, top in [
                ("accommodation_ko", "1) 숙박시설 제공: 미제공", 697),
                ("accommodation_cost_ko", "- 숙박에 대한 근로자 부담금액: 0원", 747),
                ("meal_ko", "2) 식사 제공: 미제공", 788),
                ("meal_cost_ko", "- 식사에 대한 근로자 부담금액: 0원", 824),
            ]:
                field(name, text, 245, top, 850, 16)
            canvas.setFillColor(colors.white)
            canvas.rect(238, height - 1100, 865, 193, fill=1, stroke=0)
            for name, text, top in [
                ("accommodation_en", "Accommodation: Not provided", 914),
                ("accommodation_cost_en", "- Housing cost paid by the employee: KRW 0", 972),
                ("meal_en", "Meals: Not provided", 1034),
                ("meal_cost_en", "- Meal cost paid by the employee: KRW 0", 1070),
            ]:
                field(name, text, 245, top, 850, 16)
            field("signed_date_ko", "2026년 09월 25일", 924, 1339, 180, 16, white_height=24)
            field("signed_date_en", "2026/09/25", 981, 1372, 120, 15, white_height=22)
            field("employer_signature_ko", "테스트대표 / 서명 생략", 405, 1403, 360, 16)
            field("employer_signature_en", "TEST EMPLOYER / NO SIGNATURE", 405, 1438, 360, 15)
            field("employee_signature_ko", sample["employee"] + " / 서명 생략", 405, 1479, 360, 16)
            field("employee_signature_en", "TEST EMPLOYEE / NO SIGNATURE", 405, 1517, 360, 15)
        canvas.restoreState()
        canvas.showPage()
    canvas.save()
    return field_map


def poppler():
    found = shutil.which("pdftoppm")
    if found:
        return found
    fallback = (
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin/pdftoppm.exe"
    )
    if not fallback.is_file():
        raise FileNotFoundError("pdftoppm is required for page rendering")
    return str(fallback)


def render_pages(pdf, directory, stem, scale=1800):
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [poppler(), "-scale-to", str(scale), "-png", str(pdf), str(directory / stem)],
        check=True,
        capture_output=True,
        timeout=45,
    )
    return sorted(directory.glob(stem + "-*.png"))


def answers(variant):
    sample = SAMPLES[variant]
    return {
        "pay_type": {"value": variant, "unit": "category"},
        "contract_rate": {"value": sample["rate"], "unit": sample["unit"]},
        "contract_start": {"value": sample["start"], "unit": "date"},
        "contract_end": {"value": sample["end"], "unit": "date"},
        "work_start": {"value": "09:00", "unit": "time"},
        "work_end": {"value": "18:00", "unit": "time"},
        "break_minutes": {"value": "60", "unit": "minute"},
        "scheduled_daily_minutes": {"value": "480", "unit": "minute"},
        "scheduled_weekly_minutes": {"value": "2400", "unit": "minute"},
        "payday": {"value": "25", "unit": "day_of_month"},
    }


def create(output=OUTPUT):
    register_fonts()
    output.mkdir(parents=True, exist_ok=True)
    for folder in ("gold", "source-mapping", "qa"):
        (output / folder).mkdir(exist_ok=True)
    working = ROOT / "tmp/pdfs/filled-contracts"
    working.mkdir(parents=True, exist_ok=True)
    documents = []
    source_form = next((ROOT / "표준근로계약서(제조업,+건설업,+서비스업)").glob("03*.hwp"))
    original_images = [p[2] for p in hwp_images(source_form)]
    names = {
        ("text", "hourly"): "01_시급제_15000원_텍스트.pdf",
        ("text", "monthly"): "02_월급제_300만원_텍스트.pdf",
        ("image", "hourly"): "03_표준양식_시급15000원_스캔.pdf",
        ("image", "monthly"): "04_표준양식_월급300만원_스캔.pdf",
    }
    for kind in ("text", "image"):
        for variant in ("hourly", "monthly"):
            name = names[kind, variant]
            doc_id = f"{kind}-{variant}"
            pdf = output / name
            if kind == "text":
                source = source_json(
                    "근로계약서_0002.json" if variant == "hourly" else "근로계약서_0041.json"
                )
                mapping = render_text_contract(source, variant, pdf)
            else:
                source = source_form
                intermediate = working / (doc_id + ".pdf")
                mapping = render_filled_standard(original_images, variant, intermediate)
                pngs = render_pages(intermediate, working, doc_id, scale=2100)
                images = []
                for png in pngs:
                    with Image.open(png) as image:
                        images.append(image.convert("RGB"))
                image_pdf(images, pdf)
            reader = PdfReader(io.BytesIO(pdf.read_bytes()))
            extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
            if kind == "text":
                assert "가상 작성본" in extracted
                assert ("15,000원" if variant == "hourly" else "3,000,000원") in extracted
                assert "\x00" not in extracted
                assert not re.search(r"○|@금액|20\s+년\s+월\s+일", extracted)
            else:
                assert not extracted.strip() and len(reader.pages) == 2
            sample = {
                "id": doc_id,
                "pdf": name,
                "variant": variant,
                "format": kind + "_pdf",
                "page_count": len(reader.pages),
                "sha256": content_hash(pdf.read_bytes()),
                "source": str(source.relative_to(ROOT)),
                "source_hash": content_hash(source.read_bytes()),
                "gold": f"gold/{doc_id}.json",
                "source_mapping": f"source-mapping/{doc_id}.json",
            }
            write_json(
                output / sample["source_mapping"],
                {
                    "source": sample["source"],
                    "source_hash": sample["source_hash"],
                    "method": "Filled original source fields; fictitious values",
                    "fields_or_paragraphs": mapping,
                },
            )
            write_json(
                output / sample["gold"],
                {
                    "document_id": doc_id,
                    "document_hash": sample["sha256"],
                    "synthetic": True,
                    "fields": answers(variant),
                },
            )
            render_pages(pdf, output / "qa", doc_id, scale=1400)
            documents.append(sample)
    write_json(
        output / "manifest.json",
        {"version": "filled-contracts-v1", "count": 4, "model_calls": 0, "documents": documents},
    )
    text = """# 값을 채운 근로계약서 테스트 자료

기존 Validation JSON 근로계약서 2종과 한영 표준 HWP 양식의 실제 항목 칸에 가상 값을 작성했습니다.
원본과 기존 37건 평가 자료는 변경하지 않았습니다. 이 문서는 실제 계약·서명용이 아닙니다.

| PDF | 값 | 형식 |
|---|---|---|
| 01_시급제_15000원_텍스트.pdf | 시급 15,000원 / 2026-10-01 ~ 2026-10-31 | 텍스트 원문 대조용 |
| 02_월급제_300만원_텍스트.pdf | 월급 3,000,000원 / 2026-10-01 ~ 2027-09-30 | 텍스트 원문 대조용 |
| 03_표준양식_시급15000원_스캔.pdf | 시급 15,000원 / 2026-10-01 ~ 2026-10-31 | 실제 양식 칸 작성·전체 이미지 PDF |
| 04_표준양식_월급300만원_스캔.pdf | 월급 3,000,000원 / 2026-10-01 ~ 2027-09-30 | 실제 양식 칸 작성·전체 이미지 PDF |

공통: 월-금 주 5일, 09:00-18:00, 휴게 12:00-13:00(60분), 소정근로시간 1일 8시간·1주 40시간, 지급일 매월 25일.
시간은 계약서에 명시한 소정근로시간이며 실제 출근 기록이 아닙니다. 시급제의 월 총액은 추정해 넣지 않았습니다.
가상 사업장·가상 근로자·가상 주소를 사용하고 서명은 생략했습니다.

`gold/`는 별도 기대값, `source-mapping/`은 원본 문단 또는 양식 입력 위치, `qa/`는 최종 PDF 렌더입니다.
정답 필드 ID나 평가 라벨은 PDF 본문에 넣지 않았습니다. `manifest.json`에 원본·PDF 해시를 기록했습니다.

화면에서 [내 PDF 업로드]로 PDF 하나만 선택하여 추출 테스트하세요.
별도의 새로운 PDF이므로 추출 버튼을 누르면 실제 API 비용이 발생할 수 있습니다. 이번 생성 작업 자체는 API를 호출하지 않았습니다.

재생성: `.venv/Scripts/python.exe -m agent.tests.support.make_filled_test_contracts`
"""
    (output / "README.md").write_text(text, encoding="utf-8")
    bundle = output.parent / "값입력_근로계약서_테스트세트.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for doc in documents:
            archive.write(output / doc["pdf"], doc["pdf"])
            archive.write(output / doc["gold"], doc["gold"])
            archive.write(output / doc["source_mapping"], doc["source_mapping"])
        for name in ("README.md", "manifest.json"):
            archive.write(output / name, name)
    with zipfile.ZipFile(bundle) as archive:
        assert archive.testzip() is None
    return {"documents": documents, "bundle": str(bundle), "model_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = create(args.output)
    for document in result["documents"]:
        print(document["pdf"], document["page_count"], "pages")
    print(result["bundle"])
