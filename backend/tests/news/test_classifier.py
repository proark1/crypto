"""Keyword classification: each category, precedence, and the noise default."""

from datetime import UTC, datetime

import pytest

from tradebot.news import NewsEventType, NewsItem, classify

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def make_item(title: str) -> NewsItem:
    return NewsItem(
        external_id="1",
        source="test",
        title=title,
        currencies=("SOL",),
        published_at=NOW,
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Binance Will Delist SOL Trading Pairs", NewsEventType.DELISTING),
        ("Exchange announces trading termination for three tokens", NewsEventType.DELISTING),
        ("Protocol exploited for $40M, funds drained", NewsEventType.HACK),
        ("Bridge hacked: attacker moves stolen funds", NewsEventType.HACK),
        ("SEC sues exchange over unregistered securities", NewsEventType.REGULATORY),
        ("Regulator opens crackdown on staking products", NewsEventType.REGULATORY),
        ("Coinbase will list SOL perpetuals", NewsEventType.LISTING),
        ("Foundation partners with payments giant", NewsEventType.PARTNERSHIP),
        ("Analyst: SOL could reach new highs this cycle", NewsEventType.NOISE),
    ],
)
def test_each_category_matches_its_keywords(title: str, expected: NewsEventType) -> None:
    classified = classify(make_item(title))
    assert classified.event_type == expected
    if expected != NewsEventType.NOISE:
        assert classified.matched_keyword is not None


def test_most_severe_rule_wins_on_overlap() -> None:
    """A delisting of a hacked token is treated as the delisting it is."""
    classified = classify(make_item("Exchange to delist token after hack"))
    assert classified.event_type == NewsEventType.DELISTING


def test_negative_types_are_exactly_the_action_triggers() -> None:
    assert classify(make_item("Exchange will delist FOO")).is_negative
    assert classify(make_item("Protocol exploited overnight")).is_negative
    assert classify(make_item("SEC sues issuer")).is_negative
    assert not classify(make_item("Exchange will list FOO")).is_negative
    assert not classify(make_item("Project partners with bank")).is_negative
    assert not classify(make_item("Price moves sideways")).is_negative
