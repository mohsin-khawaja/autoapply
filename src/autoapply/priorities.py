"""Queue preferences; ranking never makes an excluded role eligible."""

from __future__ import annotations

import re

_GTM_SALES = re.compile(r"\b(?:gtm|go[ -]to[ -]market|sales|pre[ -]?sales) engineer", re.I)
_AI_FDE = re.compile(
    r"\b(?:fde|forward[ -]deployed|ai(?:/ml)? engineer|ml engineer|"
    r"machine learning|applied ai|applied ml|ai/ml)\b",
    re.I,
)


def role_priority(title: str) -> int:
    if _GTM_SALES.search(title):
        return 2
    return 1 if _AI_FDE.search(title) else 0
