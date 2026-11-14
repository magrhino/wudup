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

// Matches the panel root in any of its states, including compound selectors
// such as `.apply-job-panel.apply-job-panel-active`.
const panelClass = /\.apply-job-panel(?:-active|-success)?(?![\w-])/;

type CssRule = { selector: string; body: string };

function styleRules(source: string): CssRule[] {
  const style = source.slice(source.indexOf(">", source.indexOf("<style")) + 1);
  // Innermost `selector { body }` pairs, so rules nested in @media are included.
  return [...style.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((match) => ({
    selector: match[1].trim(),
    body: match[2],
  }));
}

function targetsPanelRoot(selector: string): boolean {
  return selector.split(",").some((part) => {
    const lastCompound = part.trim().split(/[\s>+~]+/).pop() ?? "";
    return panelClass.test(lastCompound);
  });
}

describe("PendingApplyJobPanel layout", () => {
  it("keeps the panel in normal page flow so the live log stays reachable", () => {
    // A panel taken out of normal flow (sticky, fixed, or absolute) that is
    // taller than the viewport hides the live log at its bottom, because page
    // scrolling cannot bring it into view.
    const panelRules = styleRules(readFileSync(panelPath, "utf8")).filter(
      (rule) => targetsPanelRoot(rule.selector),
    );

    expect(panelRules.map((rule) => rule.selector)).toEqual(
      expect.arrayContaining([".apply-job-panel", ".apply-job-panel-active"]),
    );
    for (const rule of panelRules) {
      const positions = [...rule.body.matchAll(/(?:^|;|\s)position\s*:\s*([\w-]+)/g)]
        .map((match) => match[1]);
      for (const position of positions) {
        expect(
          ["static", "relative"],
          `${rule.selector} sets position: ${position}`,
        ).toContain(position);
      }
    }
  });
});
