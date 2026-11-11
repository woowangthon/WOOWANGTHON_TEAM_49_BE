"""All PDFium callers in this process share one reentrant lock."""

from threading import RLock

PDF_LOCK = RLock()
