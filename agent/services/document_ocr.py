"""Read digital PDF text first, with local OCR fallback for each other page."""

import asyncio
import inspect
import io

from pypdf import PdfReader

from agent.schemas.public.errors import DocumentUnreadable, OCRFailed
from agent.services.pdf_text_reading import read_pdf_text_page


async def recognize_document(data, settings, control=None):
    async def check():
        if control:
            result = control()
            if inspect.isawaitable(result):
                await result

    if len(data) > settings.max_file_bytes or not data.startswith(b"%PDF-"):
        raise DocumentUnreadable("PDF 형식과 크기를 확인해 주세요.")
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise DocumentUnreadable("암호화되지 않은 PDF가 필요합니다.")
        page_count = len(reader.pages)
        if not 1 <= page_count <= settings.max_pages:
            raise DocumentUnreadable("PDF 페이지 수 제한을 초과했습니다.")
    except DocumentUnreadable:
        raise
    except Exception as exc:
        raise DocumentUnreadable("PDF 페이지를 읽을 수 없습니다.") from exc

    from agent.services.local_ocr import recognize_pdf

    ocr_pages, page_methods = [], []
    for index in range(page_count):
        await check()
        native = await asyncio.to_thread(read_pdf_text_page, reader.pages[index])
        if native is not None:
            ocr_pages.append(native)
            page_methods.append("pdf_text")
            await check()
            continue
        recognized = await asyncio.to_thread(recognize_pdf, data, settings, [index])
        if (
            not isinstance(recognized, list)
            or len(recognized) != 1
            or not isinstance(recognized[0], dict)
            or not isinstance(recognized[0].get("text"), str)
        ):
            raise OCRFailed("OCR 페이지 결과를 읽을 수 없습니다. 문서와 OCR 설정을 확인해 주세요.")
        ocr_pages.extend(recognized)
        page_methods.append("python_ocr")
        await check()
    pages = [page["text"] for page in ocr_pages]
    if not any(page.strip() for page in pages):
        raise OCRFailed("문자를 인식하지 못했습니다. 해상도와 문서 방향을 확인해 주세요.")
    return {
        "pages": pages,
        "page_count": page_count,
        "image_pages": [False] * page_count,
        "ocr_pages": ocr_pages,
        "page_methods": page_methods,
        "source_method": page_methods[0] if len(set(page_methods)) == 1 else "mixed",
    }
