import { describe, expect, it } from "vitest";

import { signClass, trimAmount } from "./format";

describe("trimAmount", () => {
  it("removes trailing zeros without rounding", () => {
    expect(trimAmount("100.50000000")).toBe("100.5");
    expect(trimAmount("0.00001234")).toBe("0.00001234");
  });

  it("keeps integers untouched", () => {
    expect(trimAmount("10000")).toBe("10000");
  });

  it("collapses zero representations", () => {
    expect(trimAmount("0.000000")).toBe("0");
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
