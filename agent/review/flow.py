"""Observed document-processing branches; frontend rendering is separate."""

from agent.review.telemetry import timestamp

NODE_IDS = frozenset(['start', 'format', 'quality', 'image', 'native', 'render_ocr', 'ocr', 'classify', 'manual_type', 'type_gate', 'type_wait', 'extract', 'cache', 'validate', 'recheck', 'render_verify', 'verify', 'review', 'validate_again', 'locate'])


def visit(job, node_id, *sources, status="completed"):
    """Called only when control actually reaches a node/branch, never from the UI."""
    assert node_id in NODE_IDS, node_id
    job.setdefault("route", []).append(
        dict(node=node_id, sources=list(sources), status=status, at=timestamp())
    )


def reading_route(job, reading):
    """Reader spans carry page-local ordering, including failed OCR calls."""
    ends = []
    by_page = {}
    for event in reading.get("reader_calls", []):
        page = event.get("context", {}).get("page")
        before = by_page.get(page, "format")
        name = {
            "pdf.inspect_text": "quality",
            "pdf.read_native": "native",
            "image.prepare": "image",
            "page.render": "render_ocr",
            "ocr.tesseract": "ocr",
        }.get(event["name"])
        if name:
            visit(job, name, before, status=event["status"])
            event["flow_node"] = name
            by_page[page] = name
    ends.extend(dict.fromkeys(by_page.values()))
    return ends
