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

  it.each([
    [String.raw`^v\d+\.\d+\.\d+-ls\d+$`, "v1.6.0-ls357", "v1.6.1-ls357", "v1.6.0"],
    [String.raw`^\d+\.\d+\.\d+\.\d+-ls\d+$`, "6.3.0.10514-ls313", "6.3.0.10515-ls313", "6.3.0.10514"],
    [String.raw`^\d+\.\d+\.\d+ubu\d+-ls\d+$`, "10.11.11ubu2604-ls43", "10.11.11ubu2605-ls43", "10.11.11"],
    [String.raw`^\d+-\d+-\d+-r\d+$`, "2026-07-24-r1", "2026-07-24-r2", "latest"],
    [String.raw`^v\d+$`, "v5", "v6", "latest"],
  ])("derives and evaluates examples for %s", (pattern, tag, included, excluded) => {
    const guide = explainTrackingPattern(pattern, tag)!;
    expect(guide.examples).toContainEqual(expect.objectContaining({ tag: included, matches: true }));
    expect(guide.examples).toContainEqual(expect.objectContaining({ tag: excluded, matches: false }));
    for (const example of guide.examples) expect(example.matches).toBe(guide.matches(example.tag));
  });

  it("shows Alpine patch matches and major exclusions with a pinned-major summary", () => {
    const guide = explainTrackingPattern(String.raw`^2(?:\.\d+)+-alpine$`, "2.7-alpine")!;
    expect(guide.summary).toContain("at 2");
    expect(guide.examples).toContainEqual(expect.objectContaining({ tag: "2.7.1-alpine", matches: true }));
    expect(guide.examples).toContainEqual(expect.objectContaining({ tag: "3.0-alpine", matches: false }));
    expect(guide.matches("2.7.12-alpine")).toBe(true);
  });

  it("rechecks examples after an operator narrows a custom pattern", () => {
    const guide = explainTrackingPattern(String.raw`^v1\.6\.\d+-ls357$`, "v1.6.0-ls357")!;
    expect(guide.summary).not.toContain("new major");
    expect(guide.examples).toContainEqual(expect.objectContaining({ tag: "v1.6.1-ls357", matches: true }));
    expect(guide.examples).toContainEqual(expect.objectContaining({ tag: "v2.0.0-ls357", matches: false }));
    expect(guide.examples).toContainEqual(expect.objectContaining({ tag: "v1.6.0-ls358", matches: false }));
  });

  it("bounds examples to valid Docker tags and handles large numeric parts exactly", () => {
    expect(explainTrackingPattern("^latest$", "invalid/tag")?.examples).toEqual([]);
    const tag = "v999999999999999999999999.0";
    const guide = explainTrackingPattern(String.raw`^v\d+(?:\.\d+)+$`, tag)!;
    expect(guide.examples.some((example) => example.tag === "v1000000000000000000000000.0")).toBe(true);
    expect(guide.examples.every((example) => guide.matches(example.tag) !== null)).toBe(true);
  });
});
