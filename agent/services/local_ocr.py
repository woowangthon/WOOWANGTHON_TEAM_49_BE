"""Local-only PDF rasterization and Korean Tesseract OCR. No model/network calls."""

import csv
import io
import os
import shutil
import subprocess
import threading
from pathlib import Path

from agent.services.pdf_lock import PDF_LOCK

from agent.schemas.public.errors import OCRFailed, OCRUnavailable

ROOT = Path(__file__).resolve().parents[2]
OCR_LOCK = threading.Lock()  # PDFium is not thread-safe across documents either.


def executable(settings):
    if settings.ocr_command and not (
        Path(settings.ocr_command).is_file() or shutil.which(settings.ocr_command)
    ):
        raise OCRUnavailable("AGENT_OCR_COMMAND에 설정한 OCR 실행 파일을 찾을 수 없습니다.")
    candidates = [
        settings.ocr_command,
        str(ROOT / "tools/ocr/tesseract/tesseract.exe"),
        shutil.which("tesseract"),
        "C:/Program Files/Tesseract-OCR/tesseract.exe",
    ]
    for value in candidates:
        if value and (Path(value).is_file() or shutil.which(value)):
            return str(Path(value).resolve()) if Path(value).is_file() else shutil.which(value)
    raise OCRUnavailable("Tesseract와 한국어·영어 언어 데이터 설치가 필요합니다.")


def run(command, timeout, *, cwd=None, data=None):
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            input=data,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise OCRFailed("OCR 페이지 처리 시간이 초과되었습니다.") from exc
    except OSError as exc:
        raise OCRUnavailable("로컬 OCR 실행 파일을 실행할 수 없습니다.") from exc
    if result.returncode:
        raise OCRFailed("로컬 OCR 처리에 실패했습니다. 실행 파일과 언어 데이터를 확인해 주세요.")
    return result.stdout.decode("utf-8-sig", errors="replace")


def tsv_lines(tsv):
    """Reassemble visual rows, including side-by-side earnings/deduction columns."""
    words = []
    try:
        for item in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
            if item.get("level") != "5" or not (item.get("text") or "").strip():
                continue
            words.append(
                {
                    "text": item["text"].strip(),
                    "x": int(item["left"]),
                    "y": int(item["top"]),
                    "height": int(item["height"]),
                    "confidence": float(item["conf"]),
                }
            )
    except (ValueError, TypeError, KeyError) as exc:
        raise OCRFailed("OCR 인식 결과 형식을 읽을 수 없습니다.") from exc
    rows = []
    for word in sorted(words, key=lambda w: (w["y"] + w["height"] / 2, w["x"])):
        center = word["y"] + word["height"] / 2
        row = next(
            (r for r in reversed(rows) if abs(r["center"] - center) <= max(5, word["height"] * 0.45)), None
        )
        if row is None:
            row = {"center": center, "words": []}
            rows.append(row)
        row["words"].append(word)
    return [
        {
            "text": " ".join(w["text"] for w in sorted(row["words"], key=lambda w: w["x"])),
            "confidence": min(w["confidence"] for w in row["words"]),
        }
        for row in rows
    ]


def recognize_pdf(data, settings, page_numbers=None):
    with OCR_LOCK:
        try:
            with PDF_LOCK:
                return _recognize_pdf(data, settings, page_numbers)
        except OCRFailed, OCRUnavailable:
            raise
        except Exception as exc:
            raise OCRFailed(
                "PDF 이미지를 OCR로 처리하지 못했습니다. 문서 파일과 페이지 상태를 확인해 주세요."
            ) from exc


def _recognize_pdf(data, settings, page_numbers):
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise OCRUnavailable("PDF를 OCR 이미지로 변환하는 pypdfium2 설치가 필요합니다.") from exc

    command = executable(settings)
    if settings.ocr_data_path is not None and not settings.ocr_data_path.is_dir():
        raise OCRUnavailable("AGENT_OCR_DATA_PATH에 설정한 언어 데이터 폴더를 찾을 수 없습니다.")
    data_path = settings.ocr_data_path or Path(command).resolve().parent / "tessdata"
    # Relative tessdata paths avoid the Windows build's non-ASCII argv limitation.
    cwd = data_path if data_path.is_dir() else None
    command = str(Path(command).resolve()) if Path(command).is_file() else command
    options = ["--tessdata-dir", "."] if cwd else []
    languages = run([command, *options, "--list-langs"], 15, cwd=cwd).splitlines()
    if not {"kor", "eng"}.issubset({line.strip() for line in languages}):
        raise OCRUnavailable("OCR 한국어(kor)·영어(eng) 언어 데이터가 필요합니다.")
    document = pdfium.PdfDocument(data)
    pages = []
    try:
        for index in page_numbers if page_numbers is not None else range(len(document)):
            page = document[index]
            bitmap = None
            try:
                width, height = page.get_size()
                scale = min(settings.ocr_dpi / 72, (25_000_000 / max(width * height, 1)) ** 0.5)
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                try:
                    image_buffer = io.BytesIO()
                    image.save(image_buffer, format="PNG")
                finally:
                    image.close()
                output = run(
                    [command, "stdin", "stdout", *options, "-l", "kor+eng", "--psm", "6", "tsv"],
                    settings.ocr_page_timeout,
                    cwd=cwd,
                    data=image_buffer.getvalue(),
                )
                lines = tsv_lines(output)
                pages.append({"text": "\n".join(line["text"] for line in lines), "lines": lines})
            finally:
                if bitmap is not None:
                    bitmap.close()
                page.close()
    finally:
        document.close()
    return pages
