import { describe, expect, it } from "vitest";

import {
  MIN_TRADES_TO_JUDGE,
  VERDICT_LEGEND,
  expectancyTone,
  interpretRun,
  profitFactorTone,
} from "./interpret";

describe("interpretRun", () => {
  it("calls a thin sample inconclusive before anything else", () => {
    const reading = interpretRun({
      trade_count: MIN_TRADES_TO_JUDGE - 1,
      scenario_count: 400,
      expectancy_r: "2.0", // looks great — but must not be trusted
      profit_factor: "9.9",
    });
    expect(reading.tone).toBe("warn");
    expect(reading.headline).toContain("Too few trades");
    expect(reading.nextSteps.length).toBeGreaterThan(0);
  });

  it("calls a losing run losing, in plain words", () => {
    const reading = interpretRun({
      trade_count: 40,
      scenario_count: 400,
      expectancy_r: "-0.1363",
      profit_factor: "0.6048",
    });
    expect(reading.tone).toBe("bad");
    expect(reading.headline).toContain("lost money");
    expect(reading.explanation).toContain("-0.1363");
    expect(reading.nextSteps.join(" ")).toContain("sweep");
  });

  it("a positive expectancy with a sub-1 profit factor is still losing", () => {
    const reading = interpretRun({
      trade_count: 40,
      scenario_count: 400,
      expectancy_r: "0.01",
      profit_factor: "0.98",
    });
    expect(reading.tone).toBe("bad");
  });

  it("calls a winning run winning, with verification next steps", () => {
    const reading = interpretRun({
      trade_count: 40,
      scenario_count: 400,
      expectancy_r: "0.31",
      profit_factor: "1.8",
    });
    expect(reading.tone).toBe("good");
    expect(reading.headline).toContain("made money");
    expect(reading.nextSteps.join(" ")).toContain("paper");
  });
});

describe("metric tones", () => {
  it("colors expectancy by its sign", () => {
    expect(expectancyTone("0.31")).toBe("good");
    expect(expectancyTone("-0.14")).toBe("bad");
    expect(expectancyTone(undefined)).toBe("neutral");
  });

  it("colors profit factor around break-even at 1.0", () => {
    expect(profitFactorTone("1.8")).toBe("good");
    expect(profitFactorTone("0.6")).toBe("bad");
    expect(profitFactorTone(null)).toBe("neutral");
  });
});

describe("verdict legend", () => {
  it("covers every verdict the backend emits", () => {
    for (const verdict of [
      "excellent",
      "good",
      "neutral",
      "bad",
      "very_bad",
      "correct_hold",
      "wrong_hold",
      "missed_opportunity",
    ]) {
      expect(VERDICT_LEGEND[verdict]).toBeDefined();
    }
  });
});
