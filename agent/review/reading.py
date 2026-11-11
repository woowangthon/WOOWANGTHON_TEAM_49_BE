"""PDF/image readers with source coordinates, shared by extraction and the viewer."""

import csv
import io
import re
import warnings
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader

from agent.review.telemetry import local_span
from agent.services.local_ocr import executable, run
from agent.services.pdf_lock import PDF_LOCK
from agent.services.pdf_text_reading import read_pdf_text_page


def image_open(data):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as original:
            if original.format not in ("JPEG", "PNG"):
                raise ValueError("PDF·JPG·PNG 파일을 선택해 주세요.")
            if original.width * original.height > 40_000_000:
                raise ValueError("이미지는 4천만 화소 이하로 올려 주세요.")
            oriented = ImageOps.exif_transpose(original)
            return oriented.convert("RGB")


def probe(data, settings):
    if not data or len(data) > settings.max_file_bytes:
        raise ValueError("파일당 20MB 이하로 올려 주세요.")
    if data.startswith(b"%PDF-"):
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= settings.max_pages:
            raise ValueError("암호가 없고 20페이지 이하인 PDF가 필요합니다.")
        return {"media_type": "application/pdf", "extension": ".pdf", "pages": len(reader.pages)}
    with image_open(data):
        pass
    png = data.startswith(b"\x89PNG\r\n\x1a\n")
    return {
        "media_type": "image/png" if png else "image/jpeg",
        "extension": ".png" if png else ".jpg",
        "pages": 1,
    }


def render_image(data, media_type, page=1, max_side=1600):
    if media_type != "application/pdf":
        if page != 1:
            raise ValueError("이미지에는 첫 페이지만 있습니다.")
        image = image_open(data)
        image.thumbnail((max_side, max_side))
        return image
    import pypdfium2 as pdfium

    with PDF_LOCK, pdfium.PdfDocument(data) as document:
        if not 1 <= page <= len(document):
            raise ValueError("페이지 범위를 확인해 주세요.")
        with closing(document[page - 1]) as source:
            width, height = source.get_size()
            with closing(source.render(scale=max_side / max(width, height))) as bitmap:
                return bitmap.to_pil().convert("RGB")


def png_bytes(data, media_type, page=1, max_side=1600, region=None):
    with render_image(data, media_type, page, max_side) as image:
        if region:
            x0, y0, x1, y1 = region
            cropped = image.crop(
                (int(x0 * image.width), int(y0 * image.height), int(x1 * image.width), int(y1 * image.height))
            )
        else:
            cropped = image
        try:
            output = io.BytesIO()
            cropped.save(output, "PNG")
            return output.getvalue()
        finally:
            if cropped is not image:
                cropped.close()


def _native_blocks(data, page_number):
    import pypdfium2 as pdfium

    # PDFium's position converter handles rotation and cropboxes exactly as the viewer renders them.
    with PDF_LOCK, pdfium.PdfDocument(data) as document, closing(document[page_number - 1]) as page:
        width, height = page.get_size()
        with (
            closing(page.render(scale=1600 / max(width, height))) as bitmap,
            closing(page.get_textpage()) as text,
        ):
            converter = bitmap.get_posconv(page)
            words, chars, rectangles = [], [], []

            def flush():
                if chars and rectangles:
                    words.append(
                        {
                            "text": "".join(chars),
                            "bbox": [
                                min(x[0] for x in rectangles),
                                min(x[1] for x in rectangles),
                                max(x[2] for x in rectangles),
                                max(x[3] for x in rectangles),
                            ],
                            "confidence": None,
                        }
                    )
                chars.clear()
                rectangles.clear()

            for i in range(text.count_chars()):
                char = text.get_text_range(i, 1)
                if not char or char.isspace():
                    flush()
                    continue
                left, bottom, right, top = text.get_charbox(i)
                points = [converter.to_bitmap(x, y) for x in (left, right) for y in (bottom, top)]
                rectangle = [
                    min(p[0] for p in points) / bitmap.width,
                    min(p[1] for p in points) / bitmap.height,
                    max(p[0] for p in points) / bitmap.width,
                    max(p[1] for p in points) / bitmap.height,
                ]
                chars.append(char)
                rectangles.append([max(0, min(1, value)) for value in rectangle])
            flush()
            return words


def _ocr_blocks(image, settings, psm=6):
    command = executable(settings)
    directory = settings.ocr_data_path or Path(command).resolve().parent / "tessdata"
    directory = directory if directory.is_dir() else None
    options = ["--tessdata-dir", "."] if directory else []
    languages = run([command, *options, "--list-langs"], 15, cwd=directory)
    if not {"kor", "eng"} <= set(languages.split()):
        raise ValueError("OCR 한국어·영어 언어 데이터를 확인해 주세요.")
    output = io.BytesIO()
    image.save(output, "PNG")
    tsv = run(
        [command, "stdin", "stdout", *options, "-l", "kor+eng", "--psm", str(psm), "tsv"],
        settings.ocr_page_timeout,
        cwd=directory,
        data=output.getvalue(),
    )
    words = []
    for item in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if item.get("level") != "5" or not (item.get("text") or "").strip():
            continue
        x, y, w, h = (int(item[key]) for key in ("left", "top", "width", "height"))
        words.append(
            {
                "text": item["text"].strip(),
                "bbox": [x / image.width, y / image.height, (x + w) / image.width, (y + h) / image.height],
                "confidence": float(item["conf"]),
            }
        )
    return words


def _lines(words, page):
    rows = []
    for word in sorted(words, key=lambda w: ((w["bbox"][1] + w["bbox"][3]) / 2, w["bbox"][0])):
        bounds = word["bbox"]
        center, height = (bounds[1] + bounds[3]) / 2, bounds[3] - bounds[1]
        row = next(
            (r for r in reversed(rows) if abs(r["center"] - center) <= max(0.003, height * 0.45)), None
        )
        if row is None:
            row = {"center": center, "words": []}
            rows.append(row)
        row["words"].append(word)
    result = []
    for index, row in enumerate(rows):
        tokens = sorted(row["words"], key=lambda w: w["bbox"][0])
        boxes = [w["bbox"] for w in tokens]
        result.append(
            {
                "id": f"p{page}-l{index + 1}",
                "page": page,
                "text": " ".join(w["text"] for w in tokens),
                "bbox": [
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ],
                "words": tokens,
            }
        )
    return result


def read_document(data, media_type, settings):
    reader = PdfReader(io.BytesIO(data), strict=True) if media_type == "application/pdf" else None
    pages = []
    calls = []
    for number in range(1, len(reader.pages) + 1 if reader else 2):
        native = None
        if reader:
            with local_span(calls, "pdf.inspect_text", page=number):
                native = read_pdf_text_page(reader.pages[number - 1])
        method = "pdf_text" if native else "python_ocr"
        try:
            if native:
                with local_span(calls, "pdf.read_native", page=number):
                    words = _native_blocks(data, number)
            else:
                with local_span(calls, "page.render" if reader else "image.prepare", page=number):
                    image = render_image(data, media_type, number, 3000)
                with image, local_span(calls, "ocr.tesseract", page=number):
                    words = _ocr_blocks(image, settings)
            blocks = _lines(words, number)
            text = "\n".join(block["text"] for block in blocks)
            pages.append(
                {
                    "page": number,
                    "method": method,
                    "text": text,
                    "blocks": blocks,
                    "status": "complete" if text.strip() else "unreadable",
                    "error": None,
                }
            )
        except Exception:
            # A bad page must not discard readable pages. Native text can still be reviewed without boxes.
            pages.append(
                {
                    "page": number,
                    "method": method,
                    "text": (native or {}).get("text", ""),
                    "blocks": [],
                    "status": "partial" if native else "unreadable",
                    "error": "이 페이지를 완전히 읽지 못했습니다. 원문 확인 또는 재시도가 필요합니다.",
                }
            )
    return {
        "reader_calls": calls,
        "pages": pages,
        "status": "complete"
        if all(p["status"] == "complete" for p in pages)
        else "partial"
        if any(p["text"].strip() for p in pages)
        else "unreadable",
    }


def compact(text):
    return re.sub(r"\s+", "", text or "")


def locate(reading, page, quote, raw):
    source = next((p for p in reading.get("pages", []) if p["page"] == page), None)
    if not source or not raw or not quote:
        return []
    raw_key, quote_key = compact(raw), compact(quote)
    if not raw_key or quote_key.count(raw_key) != 1:
        return []
    matches = []
    blocks = source["blocks"]
    for i in range(len(blocks)):
        for count in range(1, 4):
            group = blocks[i : i + count]
            if len(group) != count:
                break
            text = compact(" ".join(b["text"] for b in group))
            if quote_key not in text or raw_key not in quote_key:
                continue
            tokens = [word for b in group for word in b["words"]]
            joined, spans = "", []
            for token in tokens:
                start = len(joined)
                joined += compact(token["text"])
                spans.append((start, len(joined), token["bbox"]))
            quote_start = joined.find(quote_key)
            start = joined.find(raw_key, quote_start, quote_start + len(quote_key))
            if start < 0:
                continue
            boxes = [box for a, b, box in spans if a < start + len(raw_key) and b > start]
            if boxes:
                matches.append(boxes)
            break
    unique = {str(boxes): boxes for boxes in matches}
    # Repeated amounts with ambiguous quotations are never silently anchored to the first occurrence.
    return next(iter(unique.values())) if len(unique) == 1 else []
