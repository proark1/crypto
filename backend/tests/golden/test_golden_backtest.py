"""The golden backtest: fixed dataset + fixed config -> byte-identical output.

Any diff here means trading behavior changed. That is either intentional —
regenerate via ``python -m tests.golden.generate_golden_backtest`` and
explain the diff in the PR — or it is a regression. Never regenerate just to
make CI pass (CLAUDE.md, testing requirements).
"""

import json
from pathlib import Path

from tests.golden.harness import run_golden_backtest

GOLDEN_PATH = Path(__file__).parent / "golden_backtest.json"


async def test_backtest_output_is_byte_identical_to_golden_fixture() -> None:
    actual = await run_golden_backtest()
    expected = GOLDEN_PATH.read_text()
    assert actual == expected, (
        "golden backtest output changed — if intentional, regenerate the fixture "
        "and explain the behavior change in the PR description"
    )


async def test_golden_run_actually_trades() -> None:
    """Guard the guard: an empty golden run would verify nothing."""
    payload = json.loads(await run_golden_backtest())
    assert len(payload["fills"]) >= 4
    assert payload["report"]["round_trips"] >= 2
