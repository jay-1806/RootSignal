"""Strip obvious secrets and personal data from text before it goes into an LLM prompt."""

import re
from typing import Any

_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?i)\b(bearer|token|api[_-]?key|password|secret)\b[\s:=\"']+[^\s\"',;]+"),
        r"\1=<redacted>",
    ),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b[A-Fa-f0-9]{24,}\b"), "<hex>"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}"), "<token>"),
]


def redact(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_obj(value: Any) -> Any:
    """Redact every string inside nested dicts/lists."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value
