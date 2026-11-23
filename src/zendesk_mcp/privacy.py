"""Redaction for content shown to a wide audience (customer quotes, ticket text)."""

from __future__ import annotations

import re
from typing import Iterable

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL = re.compile(r"https?://\S+|www\.\S+")
NUMBERISH = re.compile(r"(?<![\w-])\+?\d[\d\s().-]{6,}\d(?![\w-])")


def _numbers(m: re.Match) -> str:
    # Only redact runs with 9+ digits (phones, card/account numbers); keeps dates like 2026-10-14.
    return "[number]" if sum(ch.isdigit() for ch in m.group(0)) >= 9 else m.group(0)


class Redactor:
    """Removes emails, links, long numbers and the names of known people."""

    def __init__(self, names: Iterable[str] = ()):
        tokens: set[str] = set()
        for n in names:
            n = (n or "").strip()
            if len(n) < 3 or "@" in n:
                continue
            tokens.add(n)
            tokens.update(part for part in re.split(r"\s+", n) if len(part) >= 3 and part[0].isupper())
        # longest first so full names are replaced before their parts
        self._names = re.compile(
            r"\b(" + "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)) + r")\b"
        ) if tokens else None

    def __call__(self, text: str | None) -> str | None:
        if not text:
            return text
        text = EMAIL.sub("[email]", text)
        text = URL.sub("[link]", text)
        text = NUMBERISH.sub(_numbers, text)
        if self._names:
            text = self._names.sub("[name]", text)
        return text


def no_redact(text: str | None) -> str | None:
    return text
