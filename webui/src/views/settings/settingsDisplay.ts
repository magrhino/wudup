import type {
  CoreUpdateTourStatus,
  CoreUpdateTourStep,
  ManagedSettingEntry,
  SecretSettingStatus,
  SettingsEntry,
} from "../../api/client";
import type { SettingsDisclosureRow } from "../../components/SettingsDisclosureSection.types";
import {
  RELEASE_NOTIFICATION_DELIVERY_MODE_ON_DEMAND,
  RELEASE_NOTIFICATION_DELIVERY_MODE_ON_DETECTION,
  type ReleaseNotificationDeliveryMode,
} from "../../releaseNotifications";

export const PATH_ENTRY_NAMES = new Set([
  "DOCKER_BASE",
  "HOST_DOCKER_BASE",
  "WUD_OUT_FILE",
  "WUD_LOG_DIR",
  "WUD_DB_PATH",
]);

export const BEHAVIOR_ENTRY_NAMES = new Set([
  "WUD_UPDATE_MODE",
  "WUD_MAX_WAIT",
  "WUD_LOCK_TIMEOUT",
  "WUD_TIMEZONE",
  "WUD_COMPOSE_IGNORE_PATHS",
  "WUD_DIGEST_PIN_UPDATES",
]);

type SettingsNavLink = {
  readonly id: string;
  readonly label: string;
};

type SettingsNavGroup = SettingsNavLink & {
  readonly links: readonly SettingsNavLink[];
};

function navLink(id: string, label: string): SettingsNavLink {
  return { id, label };
}

function navGroup(
  id: string,
  label: string,
  links: readonly SettingsNavLink[],
): SettingsNavGroup {
  return { id, label, links };
}

export const SETTINGS_NAV_GROUPS: readonly SettingsNavGroup[] = [
  navGroup("operate", "Operate", [
    navLink("settings-actions", "Actions"),
    navLink("settings-preferences", "Preferences"),
    navLink("settings-notifications", "Notifications"),
  ]),
  navGroup("configuration", "Configuration", [
    navLink("settings-runtime", "Overview"),
    navLink("settings-paths", "Paths"),
    navLink("settings-behavior", "Behavior"),
    navLink("settings-webui", "WebUI safety"),
    navLink("settings-secrets", "Secrets"),
  ]),
  navGroup("support", "Support", [
    navLink("settings-diagnostics", "Diagnostics"),
    navLink("settings-docs", "Docs"),
  ]),
];

export const THEME_PREFERENCE_LABELS: Record<string, string> = {
  system: "System theme",
  light: "Light theme",
  dark: "Dark theme",
};

export const ONBOARDING_CHECKLIST_LABELS: Record<string, string> = {
  visible: "Visible",
  dismissed: "Dismissed",
};

export const SIDEBAR_BADGES_LABELS: Record<string, string> = {
  true: "Show counts",
  false: "Hide counts",
};

export const DIGEST_PIN_UPDATES_LABELS: Record<string, string> = {
  false: "Disabled",
  true: "Enabled",
};

export const RETAG_DIGEST_PINS_LABELS: Record<string, string> = {
  false: "Use selected tags",
  true: "Pin resolved digests",
};

export const RELEASE_NOTIFICATION_DELIVERY_MODE_LABELS: Record<
  ReleaseNotificationDeliveryMode,
  string
> = {
  [RELEASE_NOTIFICATION_DELIVERY_MODE_ON_DEMAND]: "On demand",
  [RELEASE_NOTIFICATION_DELIVERY_MODE_ON_DETECTION]: "On detection",
};

export const RELEASE_NOTIFICATION_MODE_LABELS: Record<string, string> = {
  digest: "Summary",
  per_container: "Per container",
};

export const RELEASE_NOTIFICATION_RESEND_POLICY_LABELS: Record<string, string> = {
  remote_change: "Remote changes",
  cooldown: "Cooldown",
};

export const RELEASE_NOTIFICATION_VERBOSITY_LABELS: Record<string, string> = {
  summary: "Summary",
  full: "Full release notes",
};

const CORE_UPDATE_TOUR_STATUS_LABELS: Record<CoreUpdateTourStatus, string> = {
  not_started: "Not started",
  in_progress: "In progress",
  completed: "Completed",
  dismissed: "Dismissed",
};

const CORE_UPDATE_TOUR_STEP_LABELS: Record<CoreUpdateTourStep, string> = {
  dashboard: "Dashboard",
  pending_select: "Pending selection",
  pending_preflight: "Preflight",
  pending_apply: "Apply guidance",
  runs_history: "History",
};

export function displayValue(value: string): string {
  return value || "unset";
}

export function entryCountLabel(entries: SettingsEntry[]): string {
  return `${entries.length} value${entries.length === 1 ? "" : "s"}`;
}

export function sourceLabel(entry: SettingsEntry): string {
  if (entry.source === "request") {
    return "Request scoped";
  }
  if (entry.source === "derived") {
    return "Runtime derived";
  }
  return entry.configured ? "Configured" : "Default";
}

export function sourceTagType(
  entry: SettingsEntry,
): "default" | "info" | "success" | "warning" {
  if (entry.source === "request") {
    return "info";
  }
  if (entry.source === "derived") {
    return "info";
  }
  return entry.configured ? "success" : "default";
}

export function secretLabel(secret: SecretSettingStatus): string {
  return secret.configured ? "Configured" : "Not configured";
}

export function managedSourceLabel(entry: ManagedSettingEntry | undefined): string {
  return entry?.source === "configured" ? "Configured" : "Default";
}

export function settingRows(entries: SettingsEntry[]): SettingsDisclosureRow[] {
  return entries.map((entry) => ({
    key: entry.name,
    name: entry.name,
    detail: `Default: ${displayValue(entry.default_value)}`,
    value: displayValue(entry.value),
    valueKind: "code",
    tagLabel: sourceLabel(entry),
    tagType: sourceTagType(entry),
  }));
}

export function secretRows(
  secrets: SecretSettingStatus[],
): SettingsDisclosureRow[] {
  return secrets.map((secret) => ({
    key: secret.name,
    name: secret.name,
    detail: "Value never rendered",
    value: "Raw value hidden",
    valueKind: "text",
    valueClass: "settings-redacted-value",
    tagLabel: secretLabel(secret),
    tagType: secret.configured ? "success" : "default",
  }));
}

export function managedOptions(
  entry: ManagedSettingEntry | undefined,
  labels: Record<string, string>,
): Array<{ label: string; value: string }> {
  return (entry?.allowed_values ?? []).map((value) => ({
    label: labels[value] ?? value,
    value,
  }));
}

export function coreUpdateTourStatusLabel(
  status: CoreUpdateTourStatus = "not_started",
): string {
  return CORE_UPDATE_TOUR_STATUS_LABELS[status] ?? status;
}

export function coreUpdateTourStepLabel(
  step: CoreUpdateTourStep = "dashboard",
): string {
  return CORE_UPDATE_TOUR_STEP_LABELS[step] ?? step;
}
