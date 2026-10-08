"""Auxiliary helpers: regex, normalization, validation."""
from __future__ import annotations

import re
import unicodedata


_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def only_digits(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", value)


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def clean_text(value: str) -> str:
    """Normalize whitespace produced by OCR/PDF extractors."""
    if not value:
        return ""
    value = value.replace("\r", "\n")
    value = _WHITESPACE_RE.sub(" ", value)
    value = _MULTI_NEWLINE_RE.sub("\n\n", value)
    return value.strip()


def format_cnpj(value: str) -> str:
    digits = only_digits(value)
    if len(digits) != 14:
        return value.strip()
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


def is_valid_cnpj(value: str) -> bool:
    digits = only_digits(value)
    if len(digits) != 14 or digits == digits[0] * 14:
        return False

    def _check_digit(slice_: str, weights: list[int]) -> int:
        total = sum(int(d) * w for d, w in zip(slice_, weights))
        remainder = total % 11
        return 0 if remainder < 2 else 11 - remainder

    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6] + w1
    d1 = _check_digit(digits[:12], w1)
    d2 = _check_digit(digits[:12] + str(d1), w2)
    return digits[-2:] == f"{d1}{d2}"
