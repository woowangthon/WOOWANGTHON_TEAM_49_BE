"""Viewer highlight boxes for extracted values (frontend `source.boundingBox`).

The frontend draws one rectangle per field as ``[x, y, width, height]`` relative
to the rendered page (top-left origin, 0..1, ``RELATIVE_0_1``). Readings store
word boxes as ``[x1, y1, x2, y2]``; this module locates the quoted value and
converts the union of its word boxes. An unlocatable or ambiguous value yields
``None`` so the viewer shows the page and quote without guessing a position.
"""

from agent.review.reading import locate

PADDING = 0.002


def union_box(boxes):
    """Union of ``[x1, y1, x2, y2]`` boxes as a clamped ``[x, y, width, height]``."""
    valid = [
        box
        for box in boxes or []
        if isinstance(box, (list, tuple))
        and len(box) == 4
        and all(isinstance(value, (int, float)) for value in box)
    ]
    if not valid:
        return None
    x1 = max(0.0, min(box[0] for box in valid) - PADDING)
    y1 = max(0.0, min(box[1] for box in valid) - PADDING)
    x2 = min(1.0, max(box[2] for box in valid) + PADDING)
    y2 = min(1.0, max(box[3] for box in valid) + PADDING)
    x, y = round(x1, 6), round(y1, 6)
    # Rounding must never push the box past the page edge; the viewer rejects x+width>1.
    width = min(round(x2 - x1, 6), round(1.0 - x, 6))
    height = min(round(y2 - y1, 6), round(1.0 - y, 6))
    if width <= 0 or height <= 0:
        return None
    return [x, y, width, height]


def bounding_box(recognized, page, quote, raw=None):
    """Locate ``raw`` inside ``quote`` on ``page``; fall back to the whole quote."""
    layout = recognized.get("layout_pages") if isinstance(recognized, dict) else None
    if not isinstance(layout, list) or not isinstance(page, int) or not quote:
        return None
    reading = {"pages": layout}
    for needle in (raw, quote):
        if not needle:
            continue
        try:
            boxes = locate(reading, page, quote, str(needle))
        except (KeyError, TypeError, ValueError):
            return None
        box = union_box(boxes)
        if box is not None:
            return box
    return None
