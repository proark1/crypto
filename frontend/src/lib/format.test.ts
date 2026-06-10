import { describe, expect, it } from "vitest";

import { signClass, trimAmount, truncateAmount } from "./format";

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
