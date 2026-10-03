import type {
  ApplyJobLogResponse,
  ApplyJobResponse,
  ApplyPreflightResponse,
  AuthSessionResponse,
  CoreUpdateTourResponse,
  DoctorResponse,
  OnboardingChecklistResponse,
  OnboardingDismissResponse,
  PendingGroupedItem,
  PendingGrouping,
  PendingItem,
  PendingResponse,
  PendingRescanResponse,
  PendingSnoozedCandidate,
  PendingSourceInfo,
  PlanResponse,
  ReleaseNoteInfo,
  ReleaseNotificationResponse,
  ReleaseNotesResponse,
  RetagPlanResponse,
  RetagPreviewJobResponse,
  RetagTargetItem,
  RetagTargetsResponse,
  RollbackPlanResponse,
  RunVerificationSummary,
  RunSummary,
  SecurityScanInfo,
  ServicePolicyRecord,
  SelfUpdateApplyResponse,
  SelfUpdatePlanResponse,
  SelfUpdatePrepareResponse,
  SelfUpdateResponse,
  SettingsResponse,
  SnoozeRecord,
  StatusResponse,
  StateOperationResponse,
  TagExclusionRuleRecord,
  UpdateTargetItem,
  UpdateTargetsResponse,
  WudApiConfigurationDiagnostics,
  WudApiObservationDiagnostics,
  WudApiStatus,
  WudContainerMetadata,
} from "../../src/api/client";
import {
  DEFAULT_RELEASE_NOTIFICATION_DELIVERY_MODE,
  RELEASE_NOTIFICATION_DELIVERY_MODE_VALUES,
} from "../../src/releaseNotifications";

type SettingsEntryFixture = SettingsResponse["updater"][number];
type ManagedSettingFixture = SettingsResponse["managed"][number];
type ApplyPreflightCheckFixture = ApplyPreflightResponse["checks"][number];

function settingsEntry(
  name: string,
  value: string,
  overrides: Partial<SettingsEntryFixture> = {},
): SettingsEntryFixture {
  return {
    name,
    value,
    default_value: value,
    configured: false,
    source: "default",
    ...overrides,
  };
}

function configuredSettingsEntry(
  name: string,
  value: string,
  overrides: Partial<SettingsEntryFixture> = {},
): SettingsEntryFixture {
  return settingsEntry(name, value, {
    configured: true,
    source: "configured",
    ...overrides,
  });
}

function managedSettingEntry(
  key: string,
  value: string,
  allowedValues: string[] = [],
  overrides: Partial<ManagedSettingFixture> = {},
): ManagedSettingFixture {
  return {
    key,
    value,
    default_value: value,
    source: "default",
    editable: true,
    allowed_values: allowedValues,
    restart_required: false,
    disabled_reason: "",
    configured: false,
    sensitive: false,
    ...overrides,
  };
}

function applyPreflightCheck(
  code: string,
  label: string,
  sourceCheckCodes: string[],
  overrides: Partial<ApplyPreflightCheckFixture> = {},
): ApplyPreflightCheckFixture {
  return {
    status: "PASS",
    code,
    label,
    detail: "",
    source_check_codes: sourceCheckCodes,
    ...overrides,
  };
}

export function wudApiStatus(
  overrides: Partial<WudApiStatus> = {},
): WudApiStatus {
  return {
    state: "ready",
    available: true,
    metadata_available: true,
    last_checked_at: "2026-01-02T00:00:00+00:00",
    detail: "1 update is available.",
    ...overrides,
  };
}

export function wudApiConfigurationDiagnostics(
  overrides: Partial<WudApiConfigurationDiagnostics> = {},
): WudApiConfigurationDiagnostics {
  const readyStatus = {
    state: "ready" as const,
    available: true,
    last_checked_at: "2026-01-02T00:00:00+00:00",
    detail: "WUD API configuration available",
  };
  return {
    health: {
      ...readyStatus,
      detail: "WUD API is reachable",
    },
    app: {
      status: readyStatus,
      name: "wud",
      version: "5.0.0",
    },
    log: {
      status: readyStatus,
      level: "debug",
    },
    store: {
      status: readyStatus,
      path: ".store",
      file: "wud.json",
      configuration: {
        path: ".store",
        file: "wud.json",
      },
    },
    watchers_status: readyStatus,
    watchers: [
      {
        id: "docker.local",
        type: "docker",
        name: "local",
        cron: "0 * * * *",
        watch_by_default: true,
        configuration: {
          cron: "0 * * * *",
          watchbydefault: true,
        },
      },
    ],
    registries_status: readyStatus,
    registries: [
      {
        id: "hub.private",
        type: "hub",
        name: "private",
        configuration: {
          auth: "<redacted>",
        },
      },
    ],
    ...overrides,
  };
}

export function wudApiObservationDiagnostics(
  overrides: Partial<WudApiObservationDiagnostics> = {},
): WudApiObservationDiagnostics {
  return {
    counts: {
      available: 1,
      degraded: 1,
      retained: 0,
      recovered: 0,
      unresolved: 1,
      unsupported_ignored: 0,
    },
    items: [
      {
        outcome: "unresolved",
        reason_code: "reported_error",
        container_id: "docker.local.app",
        name: "app",
        image: "example/app:1.0",
        watcher: "local",
        registry: "hub",
        update_available: null,
        usable_result: false,
        retryable: true,
        error: "registry request failed",
      },
    ],
    ...overrides,
  };
}

export function wudContainerMetadata(
  overrides: Partial<WudContainerMetadata> = {},
): WudContainerMetadata {
  return {
    id: "docker.local.app",
    name: "app",
    display_name: "App",
    status: "running",
    watcher: "local",
    local_tag: "1.0",
    local_digest: "sha256:old",
    remote_tag: "1.1",
    remote_digest: "sha256:new",
    update_kind: "tag",
    semver_diff: "minor",
    link: "https://github.com/acme/app/releases/tag/v1.1",
    error: "",
    ...overrides,
  };
}

export function pendingSourceInfo(
  overrides: Partial<PendingSourceInfo> = {},
): PendingSourceInfo {
  return {
    configured: "api",
    active: "api",
    label: "WUD API",
    fresh: true,
    degraded: false,
    fallback_reason: "",
    detail: "",
    ...overrides,
  };
}

export function authSession(
  overrides: Partial<AuthSessionResponse> = {},
): AuthSessionResponse {
  return {
    authenticated: true,
    setup_required: false,
    auth_required: true,
    dev_auth_bypass: false,
    mutations_enabled: false,
    username: "admin",
    ...overrides,
  };
}

export function statusResponse(
  overrides: Partial<StatusResponse> = {},
): StatusResponse {
  return {
    ok: true,
    version: "0.24.2",
    build_version: "",
    wud_file: "/out/images.todo",
    wud_file_exists: true,
    pending_count: 1,
    pending_source: pendingSourceInfo(),
    source_hash: "pending-source-hash",
    db_path: "/out/wud.sqlite",
    db_ready: true,
    auth_required: true,
    dev_auth_bypass: false,
    setup_required: false,
    mutations_enabled: false,
    timezone: "UTC",
    auto_update_scheduler_enabled: false,
    static_spa_available: true,
    wud_api: wudApiStatus(),
    warnings: [],
    ...overrides,
  };
}

export function settingsResponse(
  overrides: Partial<SettingsResponse> = {},
): SettingsResponse {
  return {
    updater: [
      configuredSettingsEntry("DOCKER_BASE", "/srv/docker"),
      configuredSettingsEntry("HOST_DOCKER_BASE", "/srv/docker", {
        default_value: "",
      }),
      configuredSettingsEntry("WUD_OUT_FILE", "/out/images.todo", {
        default_value: "/srv/docker/wud/out/images.todo",
      }),
      configuredSettingsEntry("WUD_LOG_DIR", "/logs", {
        default_value: "./logs",
      }),
      configuredSettingsEntry("WUD_DB_PATH", "/logs/wudup.sqlite"),
      settingsEntry("WUD_UPDATE_MODE", "stop"),
      settingsEntry("WUD_MAX_WAIT", "180"),
      settingsEntry("WUD_LOCK_TIMEOUT", "30"),
      settingsEntry("WUD_TIMEZONE", "UTC"),
    ],
    webui: [
      settingsEntry("WUD_WEB_AUTH_REQUIRED", "true", { source: "derived" }),
      settingsEntry("WUD_WEB_DEV_NO_AUTH", "false"),
      configuredSettingsEntry(
        "WUD_WEB_PUBLIC_ORIGIN",
        "https://wud.example.test",
        { default_value: "" },
      ),
      settingsEntry(
        "WUD_WEB_ALLOWED_HOSTS",
        "127.0.0.1, localhost, wud.example.test",
        { source: "derived" },
      ),
      settingsEntry("WUD_WEB_SECURE_COOKIES_EFFECTIVE", "true", {
        source: "request",
      }),
      settingsEntry("WUD_WEB_MUTATIONS_ENABLED", "false"),
      settingsEntry("WUD_WEB_RESTART_CONTAINER", "wudup", {
        default_value: "",
        source: "derived",
      }),
    ],
    secrets: [
      { name: "WUD_WEB_TOKEN", configured: false },
      { name: "GITHUB_TOKEN", configured: true },
      { name: "DISCORD_WEBHOOK", configured: false },
    ],
    managed: [
      managedSettingEntry("theme_preference", "system", [
        "system",
        "light",
        "dark",
      ]),
      managedSettingEntry("onboarding_checklist", "visible", [
        "visible",
        "dismissed",
      ]),
      managedSettingEntry("digest_pin_updates", "false", ["false", "true"]),
      managedSettingEntry("retag_digest_pins", "false", ["false", "true"]),
      managedSettingEntry("release_notes_enabled", "false", ["false", "true"]),
      managedSettingEntry(
        "release_notifications_delivery_mode",
        DEFAULT_RELEASE_NOTIFICATION_DELIVERY_MODE,
        Array.from(RELEASE_NOTIFICATION_DELIVERY_MODE_VALUES),
      ),
      managedSettingEntry("release_notifications_mode", "digest", [
        "digest",
        "per_container",
      ]),
      managedSettingEntry(
        "release_notifications_resend_policy",
        "remote_change",
        ["remote_change", "cooldown"],
      ),
      managedSettingEntry("release_notifications_cooldown_seconds", "86400"),
      managedSettingEntry("release_notifications_discord_webhook", "", [], {
        configured: false,
        sensitive: true,
      }),
      managedSettingEntry("release_notifications_verbosity", "summary", [
        "summary",
        "full",
      ]),
    ],
    ...overrides,
  };
}

export function selfUpdateResponse(
  overrides: Partial<SelfUpdateResponse> = {},
): SelfUpdateResponse {
  return {
    status: "available",
    strategy: "pull_image",
    current_tag: "v0.24.2",
    latest_tag: "v0.25.0",
    current_image: "ghcr.io/magrhino/wudup:latest",
    target_image: "ghcr.io/magrhino/wudup:latest",
    restart_container: "wudup",
    release_notes: [
      {
        tag: "v0.25.0",
        title: "v0.25.0",
        published_at: "2026-06-01T00:00:00Z",
        url: "https://github.com/magrhino/wudup/releases/tag/v0.25.0",
        body: "Adds self-update review and image pull support.",
        body_truncated: false,
        breaking: false,
        breaking_reasons: [],
      },
    ],
    release_notes_truncated: false,
    release_notes_cap: 10,
    can_update: true,
    disabled_reason: "",
    external_recreate_required: true,
    warnings: [],
    ...overrides,
  };
}

export function selfUpdateApplyResponse(
  overrides: Partial<SelfUpdateApplyResponse> = {},
): SelfUpdateApplyResponse {
  return {
    status: "prepared_only",
    audit_run_id: 77,
    current_tag: "v0.24.2",
    latest_tag: "v0.25.0",
    target_image: "ghcr.io/magrhino/wudup:latest",
    container: "wudup",
    running_image_id: "sha256:running-old",
    prepared_image_id: "sha256:prepared-new",
    external_recreate_required: true,
    ...overrides,
  };
}

export function selfUpdatePlanResponse(
  overrides: Partial<SelfUpdatePlanResponse> = {},
): SelfUpdatePlanResponse {
  return {
    strategy: "prepare_tag_update",
    plan: planResponse({
      plan_id: "self-update-plan-test",
      stacks: [
        {
          name: "media",
          directory: "/docker/media",
          compose_file: "docker-compose.yml",
          project_directory: "/docker/media",
          services_label: "wudup",
          services: ["wudup"],
          pull_services: ["wudup"],
          stop_services: ["wudup"],
          force_recreate: true,
          up_no_deps: true,
          tag_updates: [
            {
              old_image: "ghcr.io/magrhino/wudup:v0.24.2",
              desired_tag: "v0.25.0",
              new_image: "ghcr.io/magrhino/wudup:v0.25.0",
              services: ["wudup"],
            },
          ],
          actions: [],
          lines: [
            {
              line_no: 1,
              raw: "ghcr.io/magrhino/wudup:v0.24.2 tag=v0.25.0",
              image: "ghcr.io/magrhino/wudup:v0.24.2",
              resolved_image: "ghcr.io/magrhino/wudup:v0.24.2",
              compose_image: "ghcr.io/magrhino/wudup:v0.24.2",
              target_image: "ghcr.io/magrhino/wudup:v0.25.0",
              service: "wudup",
              digest: "",
              desired_tag: "v0.25.0",
              action: "tag-update",
            },
          ],
        },
      ],
    }),
    current_tag: "v0.24.2",
    latest_tag: "v0.25.0",
    current_image: "ghcr.io/magrhino/wudup:v0.24.2",
    target_image: "ghcr.io/magrhino/wudup:v0.25.0",
    restart_container: "wudup",
    external_recreate_required: true,
    warning:
      "This updates the Compose image tag and pulls the image. Recreate the WUDup container from outside the WebUI to run it.",
    ...overrides,
  };
}

export function selfUpdatePrepareResponse(
  overrides: Partial<SelfUpdatePrepareResponse> = {},
): SelfUpdatePrepareResponse {
  return {
    status: "tag_prepared",
    audit_run_id: 78,
    current_tag: "v0.24.2",
    latest_tag: "v0.25.0",
    target_image: "ghcr.io/magrhino/wudup:v0.25.0",
    container: "wudup",
    external_recreate_required: true,
    ...overrides,
  };
}

export function doctorResponse(
  overrides: Partial<DoctorResponse> = {},
): DoctorResponse {
  return {
    ok: false,
    failures: 1,
    warnings: 1,
    checks: [
      {
        status: "FAIL",
        code: "docker-daemon-info",
        category: "docker",
        name: "Docker daemon info",
        detail: "exit 17: permission denied",
        target: "",
        suggestions: [
          {
            label: "Wire Docker access",
            description: "Mount the Docker socket or configure DOCKER_HOST.",
            snippet: "DOCKER_HOST=unix:///var/run/docker.sock",
          },
        ],
      },
      {
        status: "WARN",
        code: "webui-public-origin",
        category: "webui",
        name: "WebUI public origin",
        detail: "derived from request as http://testserver",
        target: "",
        suggestions: [],
      },
      {
        status: "PASS",
        code: "webui-database",
        category: "webui",
        name: "WebUI database",
        detail: "/logs/wudup.sqlite",
        target: "",
        suggestions: [],
      },
    ],
    ...overrides,
  };
}

export function onboardingChecklistResponse(
  overrides: Partial<OnboardingChecklistResponse> = {},
): OnboardingChecklistResponse {
  return {
    dismissed: false,
    dismissed_at: "",
    all_passed: false,
    visible: true,
    items: [
      {
        key: "admin-setup",
        title: "Admin setup",
        status: "PASS",
        detail: "The first admin account exists.",
        check_codes: ["webui-authentication"],
        suggestions: [],
        docs: [
          {
            label: "First login",
            url: "https://github.com/magrhino/wudup/blob/main/docs/wiki/webui-container.md#first-login",
          },
        ],
      },
      {
        key: "docker-access",
        title: "Docker daemon access",
        status: "FAIL",
        detail: "Docker daemon info: permission denied",
        check_codes: ["docker-daemon-info"],
        suggestions: [
          {
            label: "Wire Docker access",
            description: "Mount the Docker socket or configure DOCKER_HOST.",
            snippet: "DOCKER_HOST=unix:///var/run/docker.sock",
          },
        ],
        docs: [],
      },
      {
        key: "mutation-mode",
        title: "Browser mutation mode",
        status: "PASS",
        detail: "Browser apply controls are disabled server-side.",
        check_codes: ["webui-mutation-gate"],
        suggestions: [],
        docs: [],
      },
    ],
    ...overrides,
  };
}

export function onboardingDismissResponse(
  overrides: Partial<OnboardingDismissResponse> = {},
): OnboardingDismissResponse {
  return {
    dismissed: true,
    dismissed_at: "2026-05-31T00:00:00+00:00",
    ...overrides,
  };
}

export function coreUpdateTourResponse(
  overrides: Partial<CoreUpdateTourResponse> = {},
): CoreUpdateTourResponse {
  return {
    status: "not_started",
    step: "dashboard",
    updated_at: "",
    ...overrides,
  };
}

export function pendingItem(overrides: Partial<PendingItem> = {}): PendingItem {
  return {
    line_no: 1,
    raw: "repo/app:1.0 sha256=abc",
    image: "repo/app:1.0",
    key: "repo/app",
    repo: "repo/app",
    current_tag: "1.0",
    has_tag: true,
    allow_repo: false,
    digest: "sha256:abc",
    desired_tag: "1.1",
    wud_metadata: null,
    source: "file",
    source_id: "file:1",
    metadata_status: "fresh",
    ...overrides,
  };
}

export function pendingGroupedItem(
  overrides: Partial<PendingGroupedItem> = {},
): PendingGroupedItem {
  const item = pendingItem(overrides);
  const services = overrides.services ?? ["app"];
  const runtimeState = overrides.runtime_state ?? "running";
  return {
    ...item,
    selection_id: `selection-${item.line_no}`,
    resolved_image: item.image,
    target_image: item.desired_tag ? `${item.repo}:${item.desired_tag}` : item.image,
    compose_images: [item.image],
    services,
    action: item.desired_tag ? "tag-update" : "recreate_service",
    diagnostic: null,
    runtime_state: runtimeState,
    running_services:
      overrides.running_services ?? (runtimeState === "running" ? services : []),
    stopped_services:
      overrides.stopped_services ??
      (runtimeState === "not-running" ? services : []),
    ...overrides,
  };
}

export function pendingSnoozedCandidate(
  overrides: Partial<PendingSnoozedCandidate> = {},
): PendingSnoozedCandidate {
  return {
    key: "demo-hidden-media-worker",
    service_key: "media/worker",
    stack: "media",
    service: "worker",
    image: "repo/worker:1.0",
    target_image: "repo/worker:1.1",
    current_tag: "1.0",
    desired_tag: "1.1",
    digest: "",
    source_id: "docker.local.worker",
    wud_metadata: wudContainerMetadata({
      id: "docker.local.worker",
      name: "worker",
      display_name: "Worker",
      local_tag: "1.0",
      remote_tag: "1.1",
    }),
    snooze_kind: "time",
    reason: "maintenance window",
    snoozed_until: "2099-01-01T00:00:00+00:00",
    wait_for_service_key: "",
    ...overrides,
  };
}

export function securityScanInfo(
  overrides: Partial<SecurityScanInfo> = {},
): SecurityScanInfo {
  const severityCounts = {
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
    unknown: 0,
    ...overrides.severity_counts,
  };
  return {
    line_no: 1,
    state: "not_scanned",
    verdict: "unknown",
    scanner: "trivy",
    scanner_version: "",
    scanner_schema: "",
    scanned_at: "",
    db_revision: "",
    db_updated_at: "",
    advisory_counts: { ...severityCounts },
    advisory_counts_known: true,
    fixable_counts: {
      critical: 0,
      high: 0,
      medium: 0,
      low: 0,
      unknown: 0,
    },
    unfixed_count: 0,
    findings: [],
    subject: {
      requested_ref: "repo/app:2.0",
      reported_digest: "sha256:candidate",
      index_digest: "sha256:candidate",
      manifest_digest: "sha256:candidate-child",
      immutable_ref: "repo/app@sha256:candidate-child",
      platform: "linux/amd64",
    },
    comparison: {
      status: "unknown",
      current_subject: {
        requested_ref: "",
        reported_digest: "",
        index_digest: "",
        manifest_digest: "",
        immutable_ref: "",
        platform: "",
      },
      fixed_findings: [],
      remaining_findings: [],
      introduced_findings: [],
      message: "",
    },
    warnings: [],
    error_code: "",
    error_message: "",
    ...overrides,
    severity_counts: severityCounts,
  };
}

export function pendingGrouping(
  items = [pendingGroupedItem()],
): PendingGrouping {
  return {
    status: "ready",
    groups:
      items.length > 0
        ? [
            {
              name: "media",
              directory: "/docker/media",
              compose_file: "docker-compose.yml",
              project_directory: "/docker/media",
              project_name: "media",
              services_label: "app",
              services: ["app"],
              line_numbers: items.map((item) => item.line_no),
              items,
            },
          ]
        : [],
    unmatched: [],
    warnings: [],
  };
}

export function pendingResponse(items = [pendingItem()]): PendingResponse {
  const groupedItems = items.map((item) => pendingGroupedItem(item));
  return {
    source_file: "WUD API",
    source: pendingSourceInfo(),
    source_hash: "pending-source-hash",
    exists: true,
    count: items.length,
    items,
    grouping: pendingGrouping(groupedItems),
    snoozed_candidates: [],
    wud_api: wudApiStatus(),
    warnings: [],
  };
}

export function pendingRescanResponse(
  overrides: Partial<PendingRescanResponse> = {},
): PendingRescanResponse {
  return {
    status: "success",
    audit_run_id: 24,
    scope: "all",
    requested_count: 0,
    watched_count: 0,
    skipped: [],
    wud_api: wudApiStatus(),
    ...overrides,
  };
}

export function updateTarget(
  overrides: Partial<UpdateTargetItem> = {},
): UpdateTargetItem {
  return {
    service_key: "media/app",
    stack: "media",
    service: "app",
    image: "repo/app:1.0",
    image_repo: "repo/app",
    current_tag: "1.0",
    directory: "/docker/media",
    compose_file: "docker-compose.yml",
    project_directory: "/docker/media",
    ...overrides,
  };
}

export function updateTargetsResponse(
  items = [updateTarget()],
): UpdateTargetsResponse {
  return {
    status: "ready",
    count: items.length,
    items,
    warnings: [],
  };
}

export function retagTarget(
  overrides: Partial<RetagTargetItem> = {},
): RetagTargetItem {
  const { target_id: targetIdOverride, ...itemOverrides } = overrides;
  const serviceKeyOverride = itemOverrides.service_key;
  const [serviceKeyStack, serviceKeyService] =
    serviceKeyOverride?.split("/", 2) ?? [];
  const stack = itemOverrides.stack ?? serviceKeyStack ?? "media";
  const service = itemOverrides.service ?? serviceKeyService ?? "app";
  const serviceKey = serviceKeyOverride ?? `${stack}/${service}`;
  const directory = itemOverrides.directory ?? "/docker/media";
  const composeFile = itemOverrides.compose_file ?? "docker-compose.yml";
  const projectDirectory = itemOverrides.project_directory ?? "/docker/media";
  return {
    target_id:
      targetIdOverride ??
      [
        "fixture-target",
        directory,
        composeFile,
        projectDirectory,
        stack,
        service,
      ].join("|"),
    image: "repo/app:latest",
    image_repo: "repo/app",
    current_tag: "latest",
    tracking_tag: "latest",
    tracking_tag_source: "label",
    proposed_tag: "1.1",
    final_image: "repo/app@sha256:abc123",
    candidate_source: "provenance",
    candidate_warning: "",
    candidate_link_label: "",
    candidate_link_url: "",
    runtime_state: "running",
    retag_available: true,
    retag_reason: "eligible",
    choices: ["keep-current", "switch-to-concrete"],
    label_key: "wud.tag.include",
    label_value: "latest",
    digest_provenance: {
      source_image: "repo/app:latest",
      resolved_tag: "1.1",
      watch_tag: "latest",
      target_digest: "sha256:abc123",
      final_image: "repo/app@sha256:abc123",
      provenance_source: "test",
      provenance_confidence: "high",
    },
    ...itemOverrides,
    service_key: serviceKey,
    stack,
    service,
    directory,
    compose_file: composeFile,
    project_directory: projectDirectory,
  };
}

export function retagTargetsResponse(
  items = [retagTarget()],
  overrides: Partial<Omit<RetagTargetsResponse, "items">> = {},
): RetagTargetsResponse {
  return {
    status: "ready",
    count: items.length,
    items,
    warnings: [],
    ...overrides,
  };
}

export function retagPlanResponse(
  overrides: Partial<RetagPlanResponse> = {},
): RetagPlanResponse {
  return {
    plan_id: "retag-plan-test",
    status: "ready",
    can_apply: true,
    external_recreate_required: false,
    selected_count: 1,
    keep_current_count: 1,
    stacks: [
      {
        stack: "media",
        directory: "/docker/media",
        compose_file: "docker-compose.yml",
        project_directory: "/docker/media",
        services: ["app"],
        tag_updates: [],
        digest_pin_updates: [
          {
            target_id: "media/app",
            service_key: "media/app",
            stack: "media",
            service: "app",
            source_image: "repo/app:latest",
            resolved_tag: "1.1",
            planned_digest: "sha256:abc123",
            final_image: "repo/app@sha256:abc123",
            watch_tag: "latest",
            marker: "wudup.resolved-tag=1.1",
            label_key: "wud.tag.include",
            label_value: String.raw`^1\.1$$`,
            label_rewrites: [
              {
                service: "app",
                label_key: "wud.tag.include",
                current_label_value: "^latest$$",
                planned_tag: "1.1",
                proposed_label_value: String.raw`^1\.1$$`,
                proposed_label_regex: String.raw`^1\.1$`,
                approved: false,
                reason: "exact-regex-normalized",
              },
            ],
            digest_provenance: {
              source_image: "repo/app:latest",
              resolved_tag: "1.1",
              watch_tag: "latest",
              target_digest: "sha256:abc123",
              final_image: "repo/app@sha256:abc123",
              provenance_source: "test",
              provenance_confidence: "high",
            },
          },
        ],
      },
    ],
    issues: [],
    warnings: [],
    ...overrides,
  };
}

export function retagPreviewJobResponse(
  overrides: Partial<RetagPreviewJobResponse> = {},
): RetagPreviewJobResponse {
  const plan = retagPlanResponse();
  return {
    preview_job_id: "retag-preview-test",
    status: "success",
    plan,
    warnings: plan.warnings,
    error: "",
    progress: [
      {
        job_id: "retag-preview-test",
        phase: "preview",
        status: "success",
        message: "Retag preview is ready.",
        created_at: "2026-01-02T00:00:00Z",
        stack: "",
        services: [],
        line_numbers: [],
      },
    ],
    ...overrides,
  };
}

export function releaseNoteInfo(
  overrides: Partial<ReleaseNoteInfo> = {},
): ReleaseNoteInfo {
  return {
    line_no: 1,
    status: "ready",
    provider: "github",
    image_repo: "acme/app",
    upstream_repo: "acme/app",
    release_tag: "v2.0.0",
    title: "v2.0.0",
    published_at: "2026-01-02T00:00:00Z",
    breaking: false,
    breaking_reasons: [],
    links: [
      {
        label: "GitHub release",
        url: "https://github.com/acme/app/releases/tag/v2.0.0",
        kind: "github_release",
      },
    ],
    refreshed_at: "2026-01-02T00:00:00Z",
    error: "",
    body: "Full release notes body.",
    security: {
      outcome: "ordinary",
      severity: "none",
      reason_code: "no_security_signal",
      reason: "No security urgency signal was found in the release notes.",
      advisory_ids: [],
      lookup_truncated: false,
    },
    notification_key: "notification-key-1",
    notification_status: "new",
    notification_last_sent_at: "",
    notification_send_count: 0,
    notification_skipped_reason: "",
    ...overrides,
  };
}

export function releaseNotesResponse(
  items = [releaseNoteInfo()],
): ReleaseNotesResponse {
  return {
    source_file: "/out/images.todo",
    source: pendingSourceInfo(),
    count: items.length,
    items,
    enabled: true,
    disabled_reason: "",
    notifications_enabled: true,
    notifications_disabled_reason: "",
    wud_api: wudApiStatus(),
    warnings: [],
  };
}

export function releaseNotificationResponse(
  overrides: Partial<ReleaseNotificationResponse> = {},
): ReleaseNotificationResponse {
  return {
    enabled: true,
    mode: "digest",
    resend_policy: "remote_change",
    destination: {
      type: "discord",
      configured: true,
      source: "DISCORD_WEBHOOK",
    },
    source: pendingSourceInfo(),
    source_file: "/out/images.todo",
    count: 1,
    sendable_count: 1,
    skipped_count: 0,
    batch_count: 1,
    messages: [
      "🧾 WUDup batch — 1 updates found\n\n⚠️ Needs review\n• demo/app `2.0.0` → `v2.0.0` — major version bump\n\nOpen WUDup for full notes, digests, and apply plan.",
    ],
    items: [
      {
        line_no: 1,
        image: "acme/app:2.0.0",
        service_key: "demo/app",
        title: "v2.0.0",
        description: "acme/app",
        status: "ready",
        release_tag: "v2.0.0",
        image_repo: "acme/app",
        upstream_repo: "acme/app",
        current_version: "2.0.0",
        target_version: "v2.0.0",
        category: "needs_review",
        reason_code: "major_bump",
        reason_label: "major version bump",
        security: {
          outcome: "ordinary",
          severity: "none",
          reason_code: "no_security_signal",
          reason: "No security urgency signal was found in the release notes.",
          advisory_ids: [],
          lookup_truncated: false,
        },
        links: [],
        triggers: [
          {
            id: "discord.releases",
            type: "discord",
            name: "releases",
          },
        ],
        notification_key: "notification-key-1",
        notification_status: "new",
        notification_last_sent_at: "",
        notification_send_count: 0,
        skipped_reason: "",
      },
    ],
    wud_api: wudApiStatus(),
    warnings: [],
    sent: false,
    audit_run_id: 0,
    error: "",
    ...overrides,
  };
}

export function applyPreflightResponse(
  overrides: Partial<ApplyPreflightResponse> = {},
): ApplyPreflightResponse {
  return {
    ok: true,
    failures: 0,
    warnings: 0,
    checks: [
      applyPreflightCheck("docker-reachable", "Docker reachable", [
        "docker-socket",
        "docker-daemon-version",
        "docker-daemon-info",
        "docker-container-listing",
      ]),
      applyPreflightCheck("compose-renders", "Compose renders", [
        "compose-discovery",
      ]),
      applyPreflightCheck("wud-file-writable", "WUD file writable", [
        "wud-out-file-directory",
        "wud-out-file",
      ]),
      applyPreflightCheck("database-ready", "Database ready", ["webui-database"]),
      applyPreflightCheck("logs-writable", "Logs writable", ["wud-log-dir"]),
      applyPreflightCheck("mutations-enabled", "Mutations enabled", [
        "webui-mutation-gate",
      ]),
      applyPreflightCheck(
        "wud-metadata-current",
        "Selected update metadata",
        ["wud-api-observations"],
      ),
      applyPreflightCheck("bind-mounts-safe", "Bind mounts safe", [
        "bind-mount-path-invalid",
      ]),
      applyPreflightCheck(
        "selected-services-matched",
        "Selected services matched",
        ["selected-services"],
      ),
    ],
    ...overrides,
  };
}

export function planResponse(overrides: Partial<PlanResponse> = {}): PlanResponse {
  return {
    plan_id: "plan-test",
    dry_run: true,
    can_apply: true,
    status: "ready",
    source_file: "/out/images.todo",
    source: pendingSourceInfo(),
    mode: "stop",
    max_wait: 120,
    digest_pin_updates: false,
    selected_line_numbers: [1],
    selected_selections: [],
    summary: {
      target_count: 1,
      matched_target_count: 1,
      stack_count: 1,
      service_count: 1,
      skipped_count: 0,
      issue_count: 0,
    },
    targets: [],
    stacks: [
      {
        name: "media",
        directory: "/docker/media",
        compose_file: "docker-compose.yml",
        project_directory: "/docker/media",
        services_label: "app",
        services: ["app"],
        pull_services: ["app"],
        stop_services: ["app"],
        force_recreate: false,
        up_no_deps: true,
        tag_updates: [],
        digest_pin_updates: [],
        digest_unpin_updates: [],
        actions: [{ kind: "pull", description: "pull app", cwd: "/docker/media", args: ["docker", "compose", "pull", "app"] }],
        lines: [
          {
            line_no: 1,
            raw: "repo/app:1.0",
            image: "repo/app:1.0",
            resolved_image: "repo/app:1.0",
            compose_image: "repo/app:1.0",
            target_image: "repo/app:1.1",
            service: "app",
            digest: "",
            desired_tag: "1.1",
            action: "tag-update",
            metadata_status: "fresh",
          },
        ],
      },
    ],
    skipped: [],
    issues: [],
    cleanup: {
      items: [],
    },
    apply_preflight: applyPreflightResponse(),
    ...overrides,
  };
}

export function applyJobResponse(
  overrides: Partial<ApplyJobResponse> = {},
): ApplyJobResponse {
  return {
    job_id: "job-test",
    status: "queued",
    run_id: null,
    log_file: "",
    started_at: null,
    finished_at: null,
    error: "",
    selected_line_numbers: [1],
    progress: [],
    ...overrides,
  };
}

export function applyJobLogResponse(
  overrides: Partial<ApplyJobLogResponse> = {},
): ApplyJobLogResponse {
  return {
    job_id: "job-test",
    log_file: "/out/logs/job-test.log",
    exists: true,
    content: "[2026-05-28T12:00:00+00:00] [INFO] docker-update-from-wud-v2\n",
    truncated: false,
    max_bytes: 65_536,
    error: "",
    ...overrides,
  };
}

export function runSummary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 1,
    started_at: "2026-05-28T12:00:00+00:00",
    finished_at: null,
    status: "success",
    dry_run: true,
    mode: "stop",
    wud_file: "/out/images.todo",
    log_file: "",
    metadata: {},
    events: [],
    ...overrides,
  };
}

export function runVerification(
  overrides: Partial<RunVerificationSummary> = {},
): RunVerificationSummary {
  return {
    status: "verified",
    total_count: 1,
    verified_count: 1,
    needs_review_count: 0,
    items: [
      {
        line_no: 1,
        service_key: "media/app",
        stack_name: "media",
        service_name: "app",
        image: "repo/app:1.0",
        target_image: "repo/app:1.1",
        image_status: "new_image_running",
        container_status: "recreated",
        health_status: "passed",
        wud_status: "removed",
        follow_up_needed: false,
        summary: "new image running, container recreated, health passed, WUD line removed.",
      },
    ],
    ...overrides,
  };
}

export function rollbackPlan(
  overrides: Partial<RollbackPlanResponse> = {},
): RollbackPlanResponse {
  return {
    run_id: 1,
    status: "ready",
    detail: "The listed services have verified local rollback targets.",
    ready_count: 1,
    blocked_count: 0,
    not_needed_count: 0,
    items: [
      {
        event_id: 1,
        service_key: "media/app",
        stack_name: "media",
        service_name: "app",
        status: "ready",
        reason: "Current and previous image state was verified from Docker and Compose.",
        recorded_previous_image: "repo/app:1.0",
        recorded_target_image: "repo/app:1.1",
        rollback_image: `repo/app@sha256:${"a".repeat(64)}`,
        previous_image_id: "sha256:old-image",
        previous_digest: `sha256:${"a".repeat(64)}`,
        current_compose_image: "repo/app:1.1",
        current_container_image_ids: ["sha256:new-image"],
      },
    ],
    ...overrides,
  };
}

export function servicePolicy(
  overrides: Partial<ServicePolicyRecord> = {},
): ServicePolicyRecord {
  return {
    service_key: "media/app",
    update_mode: "stop",
    auto_update: true,
    snooze_default_seconds: null,
    auto_update_time: "09:30",
    auto_update_days: ["mon", "wed"],
    created_at: "2026-05-28T12:00:00+00:00",
    updated_at: "2026-05-28T12:00:00+00:00",
    metadata: {},
    ...overrides,
  };
}

export function snooze(overrides: Partial<SnoozeRecord> = {}): SnoozeRecord {
  return {
    id: 1,
    service_key: "media/app",
    snoozed_until: "2026-05-29T12:00:00+00:00",
    reason: "maintenance",
    created_at: "2026-05-28T12:00:00+00:00",
    active: true,
    kind: "time",
    wait_for_service_key: "",
    metadata: {},
    ...overrides,
  };
}

export function tagExclusion(
  overrides: Partial<TagExclusionRuleRecord> = {},
): TagExclusionRuleRecord {
  return {
    id: 1,
    scope: "image_repo",
    image_repo: "repo/app",
    service_key: "",
    match_type: "exact",
    tag: "2.0",
    regex_fragment: "",
    status: "active",
    created_at: "2026-05-28T12:00:00+00:00",
    updated_at: "2026-05-28T12:00:00+00:00",
    metadata: {},
    ...overrides,
  };
}

export function stateOperationResponse(
  overrides: Partial<StateOperationResponse> = {},
): StateOperationResponse {
  return {
    operation: "upsert_service_policy",
    status: "success",
    audit_run_id: 10,
    resource_type: "service_policy",
    resource_id: "media/app",
    resource: servicePolicy(),
    ...overrides,
  };
}
