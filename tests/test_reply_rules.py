"""Tests for deterministic reply suggestions."""

from datetime import UTC, datetime

from app.services.reply_rules import ReplyRule, normalize_message, suggest_reply

RULES = (
    ReplyRule("availability", 50, ("available",), "Yes, the demo item is available."),
    ReplyRule(
        "pickup",
        80,
        ("pickup", "today"),
        "Demo pickup slots are available after 17:00.",
        True,
    ),
)


def test_message_normalization_is_accent_and_punctuation_safe() -> None:
    assert normalize_message("Olá! Está DISPONÍVEL?") == "ola esta disponivel"


def test_highest_priority_complete_match_wins() -> None:
    result = suggest_reply(
        "Is this available for pickup today?",
        RULES,
        received_at=datetime(2026, 10, 6, 14, tzinfo=UTC),
    )
    assert result is not None
    assert result.rule_id == "pickup"
    assert result.matched_keywords == ("pickup", "today")


def test_business_hours_rule_is_not_suggested_after_hours() -> None:
    result = suggest_reply(
        "Can I arrange pickup today?",
        RULES,
        received_at=datetime(2026, 10, 6, 22, tzinfo=UTC),
    )
    assert result is None


def test_unknown_question_never_gets_a_fuzzy_response() -> None:
    assert (
        suggest_reply(
            "Can you customize this?",
            RULES,
            received_at=datetime(2026, 10, 6, 14, tzinfo=UTC),
        )
        is None
    )
