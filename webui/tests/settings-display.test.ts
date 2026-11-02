import { describe, expect, it } from "vitest";

import {
  coreUpdateTourStatusLabel,
  coreUpdateTourStepLabel,
  displayValue,
  entryCountLabel,
  managedOptions,
  secretRows,
  SETTINGS_NAV_GROUPS,
  settingRows,
  sourceLabel,
  sourceTagType,
  RELEASE_NOTIFICATION_MODE_LABELS,
  RETAG_DIGEST_PINS_LABELS,
  THEME_PREFERENCE_LABELS,
} from "../src/views/settings/settingsDisplay";
import { settingsResponse } from "./helpers/fixtures";

describe("settings display helpers", () => {
  it("maps settings entries to disclosure rows", () => {
    const configured = settingsResponse().updater[0];
    const requestScoped = settingsResponse().webui.find(
      (entry) => entry.source === "request",
    )!;

    expect(displayValue("")).toBe("unset");
    expect(entryCountLabel([configured])).toBe("1 value");
    expect(entryCountLabel([configured, requestScoped])).toBe("2 values");
    expect(sourceLabel(configured)).toBe("Configured");
    expect(sourceTagType(configured)).toBe("success");
    expect(sourceLabel(requestScoped)).toBe("Request scoped");
    expect(sourceTagType(requestScoped)).toBe("info");

    expect(settingRows([configured])[0]).toMatchObject({
      key: "DOCKER_BASE",
      detail: "Default: /srv/docker",
      valueKind: "code",
      tagLabel: "Configured",
      tagType: "success",
    });
  });

  it("redacts secret row values", () => {
    expect(secretRows(settingsResponse().secrets)).toContainEqual(
      expect.objectContaining({
        key: "GITHUB_TOKEN",
        value: "Raw value hidden",
        valueClass: "settings-redacted-value",
        tagLabel: "Configured",
        tagType: "success",
      }),
    );
  });

  it("labels managed options and core tour state", () => {
    const themeEntry = settingsResponse().managed.find(
      (entry) => entry.key === "theme_preference",
    );

    expect(managedOptions(themeEntry, THEME_PREFERENCE_LABELS)).toEqual([
      { label: "System theme", value: "system" },
      { label: "Light theme", value: "light" },
      { label: "Dark theme", value: "dark" },
    ]);
    const retagDigestPinsEntry = settingsResponse().managed.find(
      (entry) => entry.key === "retag_digest_pins",
    );
    expect(managedOptions(retagDigestPinsEntry, RETAG_DIGEST_PINS_LABELS)).toEqual([
      { label: "Use selected tags", value: "false" },
      { label: "Pin resolved digests", value: "true" },
    ]);
    const releaseNotificationModeEntry = settingsResponse().managed.find(
      (entry) => entry.key === "release_notifications_mode",
    );
    expect(releaseNotificationModeEntry?.value).toBe("digest");
    expect(
      managedOptions(releaseNotificationModeEntry, RELEASE_NOTIFICATION_MODE_LABELS),
    ).toEqual([
      { label: "Summary", value: "digest" },
      { label: "Per container", value: "per_container" },
    ]);
    expect(coreUpdateTourStatusLabel("completed")).toBe("Completed");
    expect(coreUpdateTourStepLabel("runs_history")).toBe("History");
  });

  it("keeps the settings nav grouped by workflow", () => {
    const expectedLinkIdsByGroup: Record<string, string[]> = {
      operate: [
        "settings-actions",
        "settings-preferences",
        "settings-notifications",
        "settings-integrations",
      ],
      configuration: [
        "settings-runtime",
        "settings-paths",
        "settings-behavior",
        "settings-webui",
        "settings-secrets",
      ],
      support: ["settings-diagnostics", "settings-docs"],
    };
    const seenLinkIds = new Set<string>();

    expect(SETTINGS_NAV_GROUPS.map((group) => group.id)).toEqual([
      "operate",
      "configuration",
      "support",
    ]);
    SETTINGS_NAV_GROUPS.forEach((group) => {
      expect(group.links.map((link) => link.id)).toEqual(
        expectedLinkIdsByGroup[group.id],
      );
      group.links.forEach((link) => {
        expect(seenLinkIds.has(link.id)).toBe(false);
        seenLinkIds.add(link.id);
      });
    });
  });
});
