from datetime import timedelta

from tests.marketdata.conftest import MakeM1
from tradebot.marketdata import validate_candle


def test_clean_candle_has_no_issues(make_m1: MakeM1) -> None:
    assert validate_candle(make_m1(0)) == ()


def test_high_below_low_is_flagged(make_m1: MakeM1) -> None:
    candle = make_m1(0, open_quote="95", close_quote="95", high_quote="90", low_quote="100")
    issues = validate_candle(candle)
    assert any("below low" in issue for issue in issues)


def test_high_below_close_is_flagged(make_m1: MakeM1) -> None:
    candle = make_m1(0, open_quote="100", high_quote="100", low_quote="90", close_quote="120")
    issues = validate_candle(candle)
    assert any("below open or close" in issue for issue in issues)


def test_low_above_open_is_flagged(make_m1: MakeM1) -> None:
    candle = make_m1(0, open_quote="80", high_quote="110", low_quote="90", close_quote="105")
    issues = validate_candle(candle)
    assert any("above open or close" in issue for issue in issues)


def test_negative_volume_is_flagged(make_m1: MakeM1) -> None:
    issues = validate_candle(make_m1(0, volume_base="-1"))
    assert any("negative" in issue for issue in issues)


def test_close_time_not_matching_interval_is_flagged(make_m1: MakeM1) -> None:
    good = make_m1(0)
    bad = good.model_copy(update={"close_time": good.close_time + timedelta(minutes=1)})
    issues = validate_candle(bad)
    assert any("does not match" in issue for issue in issues)


def test_all_issues_are_reported_together(make_m1: MakeM1) -> None:
    candle = make_m1(
        0,
        open_quote="95",
        close_quote="95",
        high_quote="90",
        low_quote="100",
        volume_base="-1",
    )
    assert len(validate_candle(candle)) >= 3
