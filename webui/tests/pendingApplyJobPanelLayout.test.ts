import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const webuiRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const panelPath = join(
  webuiRoot,
  "src",
  "components",
  "pending",
  "PendingApplyJobPanel.vue",
);

function activePanelRules(source: string): string[] {
  const style = source.slice(source.indexOf("<style"));
  return [...style.matchAll(/\.apply-job-panel-active\s*\{([^}]*)\}/g)].map(
    (match) => match[1],
  );
}

describe("PendingApplyJobPanel layout", () => {
  it("keeps the running panel in normal page flow so the live log stays reachable", () => {
    // A pinned panel taller than the viewport hides the live log at its
    // bottom until the job ends, because page scrolling cannot move it.
    const rules = activePanelRules(readFileSync(panelPath, "utf8"));

    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) {
      expect(rule).not.toMatch(/position\s*:\s*(sticky|fixed)/);
    }
  });
});
