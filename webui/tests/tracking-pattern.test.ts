import { describe, expect, it } from "vitest";

import { explainTrackingPattern } from "../src/views/trackingPattern";

describe("tracking pattern explanation", () => {
  it("explains and tests dotted versions without claiming registry availability", () => {
    const guide = explainTrackingPattern("^v\\d+(?:\\.\\d+)+$");

    expect(guide?.description).toContain("new major version");
    expect(guide?.matches("v1.36.2")).toBe(true);
    expect(guide?.matches("v1.37")).toBe(true);
    expect(guide?.matches("v2.0")).toBe(true);
    expect(guide?.matches("latest")).toBe(false);
    expect(guide?.matches("v1.37-rc1")).toBe(false);
    expect(guide?.matches("v1.37\n")).toBe(null);
  });

  it("tests a fixed suffix without treating it as a free-form regex", () => {
    const guide = explainTrackingPattern("^\\d+\\.\\d+\\.\\d+-ls\\d+$");

    expect(guide?.description).toContain("“-ls”");
    expect(guide?.matches("9.13.0-ls415")).toBe(true);
    expect(guide?.matches("9.13.0")).toBe(false);
  });

  it.each(["latest", "latest-trivy", "stable", "16"])(
    "explains exact tag %s without implying that it follows renamed tags",
    (tag) => {
      const guide = explainTrackingPattern(`^${tag}$`);

      expect(guide?.description).toContain("Matches only the tag");
      expect(guide?.description).toContain("WUD digest watching");
      expect(guide?.matches(tag)).toBe(true);
      expect(guide?.matches(`${tag}-next`)).toBe(false);
    },
  );

  it("warns that an all-numeric wildcard spans other channel tags", () => {
    const guide = explainTrackingPattern(String.raw`^\d+$`);

    expect(guide?.description).toContain("other all-numeric tags");
    expect(guide?.matches("16")).toBe(true);
    expect(guide?.matches("17")).toBe(true);
  });

  it("never evaluates unsupported or overlapping expressions", () => {
    expect(explainTrackingPattern("^(v\\d+)+$")).toBeNull();
    expect(explainTrackingPattern("^v\\d+\\d+\\.\\d+$")).toBeNull();
    expect(explainTrackingPattern("v\\d+")).toBeNull();
    expect(explainTrackingPattern("^v\\d+$".repeat(50))).toBeNull();
  });
});
