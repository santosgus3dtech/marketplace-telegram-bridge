"""Deterministic, reviewable reply suggestions for marketplace messages."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime


def normalize_message(value: str) -> str:
    """Normalize user text without retaining punctuation or accents."""

    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", ascii_text.lower())).strip()


@dataclass(frozen=True, slots=True)
class ReplyRule:
    """One explicit reply rule configured by an operator."""

    rule_id: str
    priority: int
    all_keywords: tuple[str, ...]
    response: str
    business_hours_only: bool = False


@dataclass(frozen=True, slots=True)
class ReplySuggestion:
    """A suggestion that still requires a human or policy decision to send."""

    rule_id: str
    response: str
    matched_keywords: tuple[str, ...]
    reason: str


def suggest_reply(
    message: str,
    rules: Iterable[ReplyRule],
    *,
    received_at: datetime,
    business_hour_start: int = 9,
    business_hour_end: int = 18,
) -> ReplySuggestion | None:
    """Return the highest-priority complete match, never a fuzzy guess."""

    normalized = normalize_message(message)
    tokens = set(normalized.split())
    in_business_hours = business_hour_start <= received_at.hour < business_hour_end
    candidates: list[tuple[ReplyRule, tuple[str, ...]]] = []
    for rule in rules:
        keywords = tuple(normalize_message(keyword) for keyword in rule.all_keywords)
        if rule.business_hours_only and not in_business_hours:
            continue
        if keywords and all(keyword in tokens or keyword in normalized for keyword in keywords):
            candidates.append((rule, keywords))
    if not candidates:
        return None
    rule, matched = sorted(candidates, key=lambda item: (-item[0].priority, item[0].rule_id))[0]
    return ReplySuggestion(
        rule_id=rule.rule_id,
        response=rule.response,
        matched_keywords=matched,
        reason=f"All {len(matched)} configured keywords matched; priority={rule.priority}",
    )
