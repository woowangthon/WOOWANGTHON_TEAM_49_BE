"""Conservative, page-local use of visible digital PDF text.

Passing these checks means the layer is usable as an extraction source, not that
the document or its facts are verified. Image-bearing pages deliberately fall
back to OCR, including scans with a plausible-looking hidden text layer.
"""

import re
import unicodedata
from collections import Counter


def _usable_text(text):
    characters = [character for character in text if not character.isspace()]
    if len(characters) < 8 or sum(character.isalnum() for character in characters) < 6:
        return False
    if re.search(r"\(cid:\d+\)", text, re.I):
        return False
    if any(
        character == "\ufffd" or unicodedata.category(character) in {"Cc", "Cf", "Cn", "Co", "Cs"}
        for character in characters
    ):
        return False
    return sum(character.isalnum() for character in characters) / len(characters) >= 0.3


def read_pdf_text_page(page):
    """Return layout text only when the visible text layer passes conservative checks.

    Extraction errors, suspicious visibility, unsupported rotation, broken font
    mapping, or character loss during layout extraction all select OCR instead.
    No numeric spelling is repaired or inferred here.
    """
    try:
        if len(page.images):
            return None
        resources = page.get("/Resources", {}).get_object()
        graphic_states = resources.get("/ExtGState", {})
        box = page.cropbox
        state = {"mode": 0, "alpha": 1.0, "shade": 0.0, "clipped": False}
        stack, failures, path_boxes, backgrounds, text_boxes = [], [], [], [], []

        def point(x, y, matrix):
            return (
                float(x * matrix[0] + y * matrix[2] + matrix[4]),
                float(x * matrix[1] + y * matrix[3] + matrix[5]),
            )

        def operand(operator, operands, cm, tm):
            nonlocal state
            if operator == b"q":
                stack.append(dict(state))
            elif operator == b"Q":
                if stack:
                    state = stack.pop()
                else:
                    failures.append("unbalanced_graphics")
            elif operator == b"Tr":
                state["mode"] = int(operands[0])
            elif operator == b"gs":
                graphic_state = graphic_states.get(operands[0])
                if graphic_state is None:
                    failures.append("unknown_graphics_state")
                else:
                    graphic_state = graphic_state.get_object()
                    state["alpha"] = min(
                        float(graphic_state.get("/ca", state["alpha"])),
                        float(graphic_state.get("/CA", state["alpha"])),
                    )
                    if graphic_state.get("/SMask", "/None") != "/None":
                        failures.append("text_mask")
            elif operator == b"g":
                state["shade"] = float(operands[0])
            elif operator == b"rg":
                state["shade"] = sum(
                    float(value) * weight for value, weight in zip(operands, (0.21, 0.72, 0.07))
                )
            elif operator == b"k":
                c, m, y, k = map(float, operands)
                state["shade"] = (1 - k) * (0.21 * (1 - c) + 0.72 * (1 - m) + 0.07 * (1 - y))
            elif operator == b"re":
                x, y, width, height = map(float, operands)
                corners = [point(x + dx, y + dy, cm) for dx in (0, width) for dy in (0, height)]
                path_boxes.append(
                    (
                        min(p[0] for p in corners),
                        min(p[1] for p in corners),
                        max(p[0] for p in corners),
                        max(p[1] for p in corners),
                    )
                )
            elif operator in (b"f", b"f*", b"F", b"B", b"B*"):
                if state["alpha"] >= 0.98:
                    if any(
                        bounds[0] < text_bounds[2]
                        and bounds[2] > text_bounds[0]
                        and bounds[1] < text_bounds[3]
                        and bounds[3] > text_bounds[1]
                        for bounds in path_boxes
                        for text_bounds in text_boxes
                    ):
                        failures.append("text_covered_by_rectangle")
                    backgrounds.extend((bounds, state["shade"]) for bounds in path_boxes)
                path_boxes.clear()
            elif operator in (b"m", b"l", b"c", b"n", b"S", b"s"):
                path_boxes.clear()
            elif operator in (b"W", b"W*"):
                state["clipped"] = True
            elif operator in (b"Tj", b"TJ", b"'", b'"'):
                if state["mode"] != 0 or state["alpha"] < 0.98 or state["clipped"]:
                    failures.append("hidden_or_styled_text")
                # Compare against the last opaque rectangle behind the text,
                # or the white page. This includes dark-on-dark hidden text
                # while retaining white headings over a dark table cell.
                x, y = point(tm[4], tm[5], cm)
                background = next(
                    (
                        shade
                        for bounds, shade in reversed(backgrounds)
                        if bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]
                    ),
                    1.0,
                )
                if abs(state["shade"] - background) < 0.25:
                    failures.append("low_contrast_text")

        def text_fragment(text, cm, tm, font, size):
            if not text.strip():
                return
            if font is None or size < 1:
                failures.append("unknown_font")
            # Layout mode can omit rotated fragments; do not silently lose them.
            if any(abs(float(value)) > 0.001 for value in (cm[1], cm[2], tm[1], tm[2])):
                failures.append("rotated_text")
            x = float(tm[4] * cm[0] + tm[5] * cm[2] + cm[4])
            y = float(tm[4] * cm[1] + tm[5] * cm[3] + cm[5])
            if not float(box.left) - size <= x <= float(box.right) + size or not (
                float(box.bottom) - size <= y <= float(box.top) + size
            ):
                failures.append("off_page_text")
            # A conservative font-size envelope is enough to reject later
            # rectangle overpainting. It is not a glyph renderer or a claim
            # that every form of occlusion can be detected.
            lines = text.rstrip().splitlines()
            width = max(len(line) for line in lines) * size
            corners = [
                point(*point(dx, dy, tm), cm)
                for dx in (0, width)
                for dy in (-size * (len(lines) - 0.75), size)
            ]
            text_boxes.append(
                (
                    min(p[0] for p in corners),
                    min(p[1] for p in corners),
                    max(p[0] for p in corners),
                    max(p[1] for p in corners),
                )
            )

        plain = page.extract_text(visitor_operand_before=operand, visitor_text=text_fragment)
        if failures or stack or not _usable_text(plain):
            return None
        layout = page.extract_text(extraction_mode="layout", layout_mode_space_vertically=False)
        if not _usable_text(layout):
            return None
        # Layout may reorder columns, but it must preserve characters and every
        # complete numeric token, including punctuation, signs, dates and units.
        if Counter(re.sub(r"\s+", "", plain)) != Counter(re.sub(r"\s+", "", layout)):
            return None
        number = r"(?<!\w)[+\-−]?\d[\d,.:/\-]*"
        if Counter(re.findall(number, plain)) != Counter(re.findall(number, layout)):
            return None
        text = "\n".join(line.rstrip() for line in layout.splitlines()).strip()
        return {"text": text, "lines": [{"text": line} for line in text.splitlines() if line.strip()]}
    except Exception:
        # A damaged or unsupported layer does not make the rendered page unusable.
        return None
