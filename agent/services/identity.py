"""Comparison keys only: never replace the original document spelling."""

import re
import unicodedata


def identity_key(value, *, company=False):
    text = unicodedata.normalize("NFKC", value or "").casefold()
    if company:
        text = re.sub(r"\(\s*가상\s*\)", "", text)
        text = re.sub(r"주\s*식\s*회\s*사|\(\s*주\s*\)", "", text)
    return re.sub(r"\s+", "", text)


def ambiguous_money(raw):
    text = re.sub(r"[\s₩￦원]", "", raw or "")
    return bool(re.fullmatch(r"-?\d{4,}\.\d{3}", text) or re.fullmatch(r"-?\d[\d,.]*\.\d{3}[\d,.]*", text))
