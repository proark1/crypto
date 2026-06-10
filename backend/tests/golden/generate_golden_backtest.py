"""Regenerate the golden backtest fixture — only for *intentional* changes.

    cd backend && uv run python -m tests.golden.generate_golden_backtest

CLAUDE.md: never regenerate this file just to make CI pass. If the golden
test fails, either the behavior change is intentional (regenerate, and
explain the diff in the PR description) or you introduced a regression.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.golden.harness import run_golden_backtest

OUTPUT_PATH = Path(__file__).parent / "golden_backtest.json"


def main() -> None:
    """Run the golden configuration and overwrite the committed fixture."""
    OUTPUT_PATH.write_text(asyncio.run(run_golden_backtest()))
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
