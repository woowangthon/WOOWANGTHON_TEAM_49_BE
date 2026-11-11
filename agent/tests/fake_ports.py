"""Test doubles layered over the real file-backed local ports."""

import asyncio
import io
from copy import deepcopy

from pypdf import PdfReader

from agent.config import AgentSettings
from agent.ports.local import LocalFilePorts
from agent.schemas.public.errors import DocumentUnreadable, IntegrityError, OCRFailed
from agent.services import local_ocr


class FakePorts(LocalFilePorts):
    def __init__(self, **options):
        super().__init__(**options)
        self.ocr_results = {}
        self.ocr_settings = AgentSettings()

    async def read_ocr(self, context, source, ocr_result_id):
        content = await self.read_source(context, source)
        if ocr_result_id in self.ocr_results:
            return deepcopy(self.ocr_results[ocr_result_id])
        if not isinstance(ocr_result_id, str) or not ocr_result_id.startswith("test-ocr"):
            raise IntegrityError("Test OCR result is absent")
        try:
            reader = PdfReader(io.BytesIO(content.data), strict=True)
            if reader.is_encrypted:
                raise DocumentUnreadable("Encrypted PDFs are unsupported")
            count = len(reader.pages)
        except DocumentUnreadable:
            raise
        except Exception as exc:
            raise DocumentUnreadable("Test source PDF is unreadable") from exc
        if (
            not 1 <= count <= self.ocr_settings.max_pages
            or len(content.data) > self.ocr_settings.max_file_bytes
        ):
            raise DocumentUnreadable("Test PDF exceeds configured limits")
        pages = []
        for index in range(count):
            recognized = await asyncio.to_thread(
                local_ocr.recognize_pdf, content.data, self.ocr_settings, [index]
            )
            if (
                not isinstance(recognized, list)
                or len(recognized) != 1
                or not isinstance(recognized[0], dict)
                or not isinstance(recognized[0].get("text"), str)
            ):
                raise OCRFailed("Invalid test OCR adapter result")
            pages.extend(recognized)
        if not any(page["text"].strip() for page in pages):
            raise OCRFailed("Empty test OCR adapter result")
        return {
            "id": ocr_result_id,
            "document_id": source.document_id,
            "document_version": source.document_version,
            "content_hash": source.content_hash,
            "pages": [page["text"] for page in pages],
            "ocr_pages": pages,
            "page_count": count,
            "image_pages": [False] * count,
        }
