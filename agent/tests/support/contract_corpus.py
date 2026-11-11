"""Reproducible local validation corpus. Original files are always read-only."""

import html
import io
import json
import random
import re
import zlib
from pathlib import Path

import olefile
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from agent.services.hashing import content_hash

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "output/contract-validation"
FONT = Path("C:/Windows/Fonts/malgun.ttf")
SEED = 20261104
FIELDS = {
    "pay_type": "임금 형태",
    "contract_rate": "임금 단가",
    "contract_start": "계약 시작일",
    "contract_end": "계약 종료일",
    "work_start": "출근 시각",
    "work_end": "퇴근 시각",
    "break_minutes": "휴게 시간",
    "scheduled_daily_minutes": "명시된 일 소정근로시간",
    "scheduled_weekly_minutes": "명시된 주 소정근로시간",
    "payday": "지급일",
}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def block_text(block):
    def cell_text(value):
        if isinstance(value, dict):
            if value.get("text"):
                return str(value["text"])
            return " ".join(cell_text(v) for v in value.values() if isinstance(v, (list, dict)))
        if isinstance(value, list):
            return " ".join(cell_text(v) for v in value)
        return ""

    return "\n".join(cell_text(c) for c in block.get("contents", []))


def ordered_blocks(path):
    return sorted(read_json(path)["document"]["sub_documents"], key=lambda b: b.get("sort_order") or 0)


def select_json_sources(root=ROOT):
    base = root / "계약_법률_문서_서식_데이터/3.개방데이터/1.데이터/Validation"
    sources = {p.stem: p for p in (base / "01.원천데이터").rglob("*.json")}
    groups = {"근로(고용)계약서": [], "아르바이트계약서": [], "기타고용계약서": []}
    for label in sorted((base / "02.라벨링데이터").rglob("*.json")):
        if label.stem not in sources or "고용계약" not in label.parent.name:
            continue
        doc = read_json(label)["document"]
        category = doc["metadata"]["file_info"]["document_category"]["detail_category"]
        if category not in groups:
            continue
        # Labels locate candidate wage clauses only; actual values still require source review.
        wage_clauses = [
            s["content"]["description"]
            for s in doc["sections"]
            if any("임금" in x for x in s["content"].get("content_labels", []))
        ]
        explicit = any(re.search(r"\d[\d,]*(?:\.\d+)?\s*(?:만|천|억)?\s*원", t) for t in wage_clauses)
        groups[category].append((sources[label.stem], label, explicit))
    rng, selected, seen = random.Random(SEED), [], set()
    for category, count in zip(groups, (12, 4, 4)):
        candidates = groups[category]
        rng.shuffle(candidates)
        candidates.sort(key=lambda x: not x[2])
        taken = 0
        for source, label, explicit in candidates:
            fingerprint = content_hash("\n".join(block_text(b) for b in ordered_blocks(source)).encode())
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            selected.append((category, source, label, explicit))
            taken += 1
            if taken == count:
                break
        if taken != count:
            raise ValueError(f"Insufficient distinct sources: {category}")
    return selected


def wrap_text(text, font="CorpusKorean", size=10, width=479):
    for line in text.split("\n"):
        current = ""
        for character in line:
            if current and pdfmetrics.stringWidth(current + character, font, size) > width:
                yield current
                current = ""
            current += character
        yield current


def render_json(source, destination):
    pdfmetrics.registerFont(TTFont("CorpusKorean", str(FONT)))
    canvas = Canvas(str(destination), pagesize=(595, 842), invariant=1)
    page, y, mapping = 0, 0, []

    def new_page():
        nonlocal page, y
        if page:
            canvas.showPage()
        page += 1
        canvas.setFont("CorpusKorean", 8)
        canvas.setFillColorRGB(0.25, 0.34, 0.3)
        canvas.drawString(48, 811, "검증 자료 · JSON 본문 재구성 · 원본 지면 배치와 다름")
        canvas.drawRightString(547, 25, str(page))
        canvas.setFillColorRGB(0, 0, 0)
        canvas.setFont("CorpusKorean", 10)
        y = 785

    new_page()
    for block in ordered_blocks(source):
        text = block_text(block)
        rendered = html.unescape(
            re.sub(r"<[^>]+>", "", re.sub(r"</td>", " | ", re.sub(r"</tr>", "\n", text)))
        )
        # Malgun Gothic has no U+2024 glyph. Record the visible punctuation substitution.
        substitutions = []
        if "\u2024" in rendered:
            rendered = rendered.replace("\u2024", "·")
            substitutions.append({"from": "U+2024", "to": "U+00B7", "reason": "font glyph unavailable"})
        lines = list(wrap_text(rendered))
        pages = []
        for line in lines:
            if y < 55:
                new_page()
            if page not in pages:
                pages.append(page)
            canvas.drawString(48, y, line)
            y -= 15
        y -= 7
        mapping.append(
            {
                "paragraph_id": block["id"],
                "sort_order": block.get("sort_order"),
                "text": text,
                "rendered_text": rendered,
                "typographic_substitutions": substitutions,
                "pages": pages,
            }
        )
    canvas.save()
    return {
        "source": str(source.relative_to(ROOT)),
        "source_hash": content_hash(source.read_bytes()),
        "page_count": page,
        "paragraphs": mapping,
    }


def hwp_images(path):
    images = []
    with olefile.OleFileIO(str(path)) as ole:
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise ValueError("Unsupported HWP header")
        for stream in sorted(ole.listdir()):
            if len(stream) != 2 or stream[0] != "BinData" or not stream[1].lower().endswith(".png"):
                continue
            data = ole.openstream(stream).read()
            if not data.startswith(b"\x89PNG"):
                data = zlib.decompress(data, -15)
            image = Image.open(io.BytesIO(data))
            image.load()
            images.append((stream[1], data, image.convert("RGB")))
    if len(images) != 2:
        raise ValueError("Expected two original page images")
    return images


def image_pdf(images, path):
    canvas = Canvas(str(path), invariant=1)
    for image in images:
        width, height = image.size
        canvas.setPageSize((595, 595 * height / width))
        canvas.drawImage(ImageReader(image), 0, 0, width=595, height=595 * height / width)
        canvas.showPage()
    canvas.save()


def filled_images(originals, variant):
    images = [im.copy() for im in originals]
    font = ImageFont.truetype(str(FONT), 18)
    small = ImageFont.truetype(str(FONT), 15)
    for image in images:
        ImageDraw.Draw(image).text(
            (75, 35), "가상 검증 자료 / SYNTHETIC TEST ONLY", font=font, fill="#174f42"
        )
    # Filled as a short bilingual amendment in the page margin: no ambiguous handwriting,
    # no obscuring multilingual form labels. Rasterization includes both form and amendment.
    lines = {
        "hourly": [
            "임금: 시급 15,000원 / Hourly wage: KRW 15,000/hour",
            "계약기간: 2026-10-01 ~ 2026-10-31 / Contract period",
            "근무: 09:00 ~ 18:00 / Work hours; 휴게 60분 / Break 60 minutes",
            "임금 지급일: 매월 25일 / Payday: 25th of each month",
        ],
        "monthly": [
            "임금: 월급 3,000,000원 / Monthly wage: KRW 3,000,000/month",
            "계약기간: 2026-10-01 ~ 2026-10-31 / Contract period",
            "근무: 09:00 ~ 18:00 / Work hours; 휴게 60분 / Break 60 minutes",
            "임금 지급일: 매월 25일 / Payday: 25th of each month",
        ],
        "missing": [
            "임금: 시급 (미기입) / Hourly wage: (not filled)",
            "계약 시작일: 2026-10-01 / Start date; 종료일: (미기입) / End date: (not filled)",
            "근무: 09:00 ~ 18:00 / Work hours; 휴게: (미기입) / Break: (not filled)",
            "임금 지급일: (미기입) / Payday: (not filled)",
        ],
    }[variant]
    draw = ImageDraw.Draw(images[0])
    # A labelled attachment occupies existing bottom whitespace, not the signed source.
    draw.rectangle((75, 1525, 1125, 1655), fill="white", outline="#174f42", width=2)
    draw.text(
        (85, 1530),
        "가상 작성 부속사항 / Filled test terms (applies to this sample)",
        font=small,
        fill="black",
    )
    for index, text in enumerate(lines):
        draw.text((85, 1554 + index * 23), text, font=small, fill="black")
    return images


def gold_field(state, value=None, unit=None, source="", page=None):
    return {"state": state, "value": value, "unit": unit, "source_quote": source, "page": page}


def prepare(output=DEFAULT_OUTPUT):
    output.mkdir(parents=True, exist_ok=True)
    for directory in ("pdfs", "gold", "mappings", "results", "usage", "review"):
        (output / directory).mkdir(exist_ok=True)
    documents, review = [], []
    for index, (category, source, label, priority) in enumerate(select_json_sources(), 1):
        doc_id = f"json-{index:02}"
        path = output / "pdfs" / f"{doc_id}.pdf"
        mapping = render_json(source, path)
        write_json(output / "mappings" / f"{doc_id}.json", mapping)
        documents.append(
            {
                "id": doc_id,
                "kind": "json",
                "category": category,
                "title": read_json(source)["document"].get("title") or source.stem,
                "pdf": f"pdfs/{doc_id}.pdf",
                "sha256": content_hash(path.read_bytes()),
                "pages": mapping["page_count"],
                "source": str(source.relative_to(ROOT)),
                "label_source": str(label.relative_to(ROOT)),
                "explicit_wage_priority": priority,
            }
        )
        review.append(
            f"\n===== {doc_id}: {source.name} / {category} =====\n"
            + "\n".join(f"[{p['paragraph_id']} / p{p['pages']}] {p['text']}" for p in mapping["paragraphs"])
        )
    (output / "review/selected-source-text.txt").write_text("\n".join(review), encoding="utf-8")
    english = None
    for index, source in enumerate(
        sorted((ROOT / "표준근로계약서(제조업,+건설업,+서비스업)").glob("*.hwp")), 1
    ):
        doc_id = f"form-{index:02}"
        pages = hwp_images(source)
        path = output / "pdfs" / f"{doc_id}.pdf"
        image_pdf([p[2] for p in pages], path)
        mapping = {
            "source": str(source.relative_to(ROOT)),
            "source_hash": content_hash(source.read_bytes()),
            "pages": [
                {
                    "page": i,
                    "stream": name,
                    "image_hash": content_hash(data),
                    "width": image.width,
                    "height": image.height,
                }
                for i, (name, data, image) in enumerate(pages, 1)
            ],
        }
        write_json(output / "mappings" / f"{doc_id}.json", mapping)
        documents.append(
            {
                "id": doc_id,
                "kind": "blank_form",
                "category": "표준 양식",
                "title": source.stem,
                "pdf": f"pdfs/{doc_id}.pdf",
                "pages": 2,
                "sha256": content_hash(path.read_bytes()),
                "source": str(source.relative_to(ROOT)),
            }
        )
        # Fixed boilerplate is not a filled employment fact; contract fields are blank.
        write_json(
            output / "gold" / f"{doc_id}.json",
            {
                "annotation_version": "human-source-v1",
                "reviewed_before_calls": True,
                "method": "Original embedded form pages inspected; placeholders/options are not completed values.",
                "fields": {field: gold_field("absent", source="Unfilled template") for field in FIELDS},
            },
        )
        if source.name.startswith("03"):
            english = (source, [p[2] for p in pages])
    if english is None:
        raise ValueError("Bilingual standard form not found")
    for variant in ("hourly", "monthly", "missing"):
        doc_id = f"filled-{variant}"
        path = output / "pdfs" / f"{doc_id}.pdf"
        image_pdf(filled_images(english[1], variant), path)
        fields = {
            "pay_type": gold_field(
                "present", "hourly" if variant != "monthly" else "monthly", "category", page=1
            ),
            "contract_rate": gold_field("absent")
            if variant == "missing"
            else gold_field(
                "present",
                "15000" if variant == "hourly" else "3000000",
                "KRW/hour" if variant == "hourly" else "KRW/month",
                page=1,
            ),
            "contract_start": gold_field("present", "2026-10-01", "date", page=1),
            "contract_end": gold_field("absent")
            if variant == "missing"
            else gold_field("present", "2026-10-31", "date", page=1),
            "work_start": gold_field("present", "09:00", "time", page=1),
            "work_end": gold_field("present", "18:00", "time", page=1),
            "break_minutes": gold_field("absent")
            if variant == "missing"
            else gold_field("present", "60", "minute", page=1),
            "payday": gold_field("absent")
            if variant == "missing"
            else gold_field("present", "25", "day_of_month", page=1),
        }
        for field in fields.values():
            field["source_quote"] = (
                "Synthetic amendment on page 1; generated from deterministic terms in source code."
            )
        fields["scheduled_daily_minutes"] = gold_field(
            "absent", source="Clock interval is not an explicit duration"
        )
        fields["scheduled_weekly_minutes"] = gold_field("absent")
        write_json(
            output / "gold" / f"{doc_id}.json",
            {
                "annotation_version": "human-source-v1",
                "reviewed_before_calls": True,
                "method": "Exact synthetic authoring values",
                "fields": fields,
            },
        )
        write_json(
            output / "mappings" / f"{doc_id}.json",
            {
                "source": str(english[0].relative_to(ROOT)),
                "variant": variant,
                "method": "Original two images plus clearly labelled synthetic amendment; all pages raster-only",
            },
        )
        documents.append(
            {
                "id": doc_id,
                "kind": "synthetic",
                "category": "가상 작성본",
                "title": {
                    "hourly": "시급 15,000원",
                    "monthly": "월급 3,000,000원",
                    "missing": "임금·기간 일부 누락",
                }[variant],
                "pdf": f"pdfs/{doc_id}.pdf",
                "pages": 2,
                "sha256": content_hash(path.read_bytes()),
                "source": str(english[0].relative_to(ROOT)),
            }
        )
    manifest = {"version": "corpus-v1", "seed": SEED, "count": len(documents), "documents": documents}
    write_json(output / "manifest.json", manifest)
    return manifest


if __name__ == "__main__":
    result = prepare()
    print(json.dumps({"count": result["count"], "output": str(DEFAULT_OUTPUT)}, ensure_ascii=False))
