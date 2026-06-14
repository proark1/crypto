import { describe, expect, it } from "vitest";

import {
  compareDecimalStrings,
  formatFractionPercent,
  formatMoney,
  humanizeParamName,
  rankMoneyDescending,
  signClass,
  trimAmount,
  truncateAmount,
} from "./format";

describe("formatMoney", () => {
  it("groups the integer part with thousands separators", () => {
    expect(formatMoney("10000.00")).toBe("10,000");
    expect(formatMoney("1234567.89")).toBe("1,234,567.89");
  });

  it("keeps the sign and trims trailing precision", () => {
    expect(formatMoney("-199.00")).toBe("-199");
    expect(formatMoney("10049.50")).toBe("10,049.5");
  });

  it("never does float math — sub-cent values keep a significant digit", () => {
    expect(formatMoney("0.0001234")).toBe("0.0001");
  });
});

describe("trimAmount", () => {
  it("removes trailing zeros without rounding", () => {
    expect(trimAmount("100.50000000")).toBe("100.5");
    expect(trimAmount("0.00001234")).toBe("0.00001234");
  });

  it("keeps integers untouched", () => {
    expect(trimAmount("10000")).toBe("10000");
  });

  it("collapses zero representations, including negative zero", () => {
    expect(trimAmount("0.000000")).toBe("0");
    expect(trimAmount("-0.000000")).toBe("0");
  });
});

describe("signClass", () => {
  it("marks losses red and gains green", () => {
    expect(signClass("-12.5")).toContain("red");
    expect(signClass("12.5")).toContain("emerald");
  });

  it("keeps zero and unknown neutral", () => {
    expect(signClass("0.00")).toContain("zinc");
    expect(signClass(null)).toContain("zinc");
  });
});

describe("formatFractionPercent", () => {
  it("renders a fraction as a signed percentage", () => {
    expect(formatFractionPercent("0.0123")).toBe("+1.23%");
    expect(formatFractionPercent("-0.05")).toBe("-5.00%");
    expect(formatFractionPercent("0")).toBe("0.00%");
  });

  it("shows a dash for unknown or unparsable values", () => {
    expect(formatFractionPercent(null)).toBe("—");
    expect(formatFractionPercent("not-a-number")).toBe("—");
  });

  it("does not put a sign on a value that rounds to zero", () => {
    expect(formatFractionPercent("0.00003")).toBe("0.00%");
    expect(formatFractionPercent("-0.00003")).toBe("0.00%");
  });
});

describe("compareDecimalStrings", () => {
  it("orders by exact value, not float precision", () => {
    expect(compareDecimalStrings("10049.00", "10048.99")).toBe(1);
    expect(compareDecimalStrings("-12.5", "0.00")).toBe(-1);
    expect(compareDecimalStrings("100.00", "100")).toBe(0);
    // Differ only far beyond double precision: float coercion would tie.
    expect(compareDecimalStrings("1.0000000000000001", "1.0000000000000002")).toBe(-1);
  });

  it("treats negative zero as equal to zero", () => {
    expect(compareDecimalStrings("-0.00", "0.00")).toBe(0);
    expect(compareDecimalStrings("-0", "0")).toBe(0);
    expect(compareDecimalStrings("0", "-0.000")).toBe(0);
    // A real negative still sorts below zero.
    expect(compareDecimalStrings("-0.01", "0.00")).toBe(-1);
  });
});

describe("rankMoneyDescending", () => {
  it("ranks highest-first, shares ranks on ties, and skips nulls", () => {
    expect(rankMoneyDescending(["10000.00", "10050.00", "9990.00"])).toEqual([2, 1, 3]);
    // Dense ranking: tied firsts share rank 1, the next distinct value is 2.
    expect(rankMoneyDescending(["5.00", "5.00", "1.00"])).toEqual([1, 1, 2]);
    expect(rankMoneyDescending(["5.00", null, "1.00"])).toEqual([1, null, 2]);
  });
});

describe("humanizeParamName", () => {
  it("spaces out snake_case and keeps indicator acronyms loud", () => {
    expect(humanizeParamName("fast_ema_period")).toBe("fast EMA period");
    expect(humanizeParamName("rsi_period")).toBe("RSI period");
    expect(humanizeParamName("atr_multiplier")).toBe("ATR multiplier");
    expect(humanizeParamName("lookback")).toBe("lookback");
  });
});

describe("truncateAmount", () => {
  it("cuts headline metrics to two decimals without rounding", () => {
    expect(truncateAmount("9989.997850243007523490000000")).toBe("9989.99");
    expect(truncateAmount("-10.002149756992476510000000")).toBe("-10");
  });

  it("keeps the first significant digit of a tiny PnL", () => {
    expect(truncateAmount("-0.000149756992476510000000")).toBe("-0.0001");
    expect(truncateAmount("0.00500000")).toBe("0.005");
  });

  it("leaves integers and exact zero alone", () => {
    expect(truncateAmount("10000")).toBe("10000");
    expect(truncateAmount("0.000000")).toBe("0");
  });
});
