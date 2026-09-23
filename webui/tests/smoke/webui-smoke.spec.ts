import { expect, test, type Page, type Route } from "@playwright/test";

import { touchTargetSizePx } from "../../src/touchTargets";

type ApiCall = {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
};

type FixtureState = {
  authenticated: boolean;
  mutationsEnabled: boolean;
  calls: ApiCall[];
};

const appOrigin = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:5173";
const csrfToken = "csrf-smoke";
const stackSelection = {
  line_no: 1,
  selection_id: "selection-media-app",
};
const mobileNavLabels = [
  "Dashboard",
  "Pending",
  "Containers",
  "Retags",
  "History",
  "Policies",
  "Snoozes",
  "Exclusions",
  "Settings",
  "Doctor",
];

function authSession(state: FixtureState) {
  return {
    authenticated: state.authenticated,
    setup_required: false,
    auth_required: true,
    dev_auth_bypass: false,
    mutations_enabled: state.mutationsEnabled,
    username: state.authenticated ? "admin" : null,
  };
}

function wudApiStatus() {
  return {
    state: "ready",
    available: true,
    metadata_available: true,
    last_checked_at: "2026-01-02T00:00:00+00:00",
    detail: "1 update is available.",
  };
}

function pendingSourceInfo(overrides: Record<string, unknown> = {}) {
  return {
    configured: "file",
    active: "file",
    label: "Pending file",
    fresh: true,
    degraded: false,
    fallback_reason: "",
    detail: "",
    ...overrides,
  };
}

function pendingResponse() {
  const item = {
    line_no: 1,
    raw: "repo/app:1.0",
    image: "repo/app:1.0",
    key: "repo/app",
    repo: "repo/app",
    current_tag: "1.0",
    has_tag: true,
    allow_repo: false,
    digest: "",
    desired_tag: "1.1",
    wud_metadata: null,
    source: "file",
    source_id: "file:1",
  };
  const groupedItem = {
    ...item,
    selection_id: stackSelection.selection_id,
    resolved_image: "repo/app:1.0",
    target_image: "repo/app:1.1",
    compose_images: ["repo/app:1.0"],
    services: ["app"],
    runtime_state: "running",
    running_services: ["app"],
    stopped_services: [],
    action: "tag-update",
  };
  return {
    source_file: "/out/images.todo",
    source: pendingSourceInfo(),
    exists: true,
    count: 1,
    warnings: [],
    items: [item],
    grouping: {
      status: "ready",
      groups: [
        {
          name: "media",
          directory: "/docker/media",
          compose_file: "docker-compose.yml",
          project_directory: "/docker/media",
          project_name: "media",
          services_label: "app",
          services: ["app"],
          line_numbers: [1],
          items: [groupedItem],
        },
      ],
      unmatched: [],
      warnings: [],
    },
    wud_api: wudApiStatus(),
  };
}

function updateTargetsResponse() {
  return {
    status: "ready",
    count: 1,
    warnings: [],
    items: [
      {
        service_key: "media/app",
        stack: "media",
        service: "app",
        image: "repo/app:1.0",
        image_repo: "repo/app",
        current_tag: "1.0",
        directory: "/docker/media",
        compose_file: "docker-compose.yml",
        project_directory: "/docker/media",
      },
    ],
  };
}

function trackedContainersResponse() {
  return {
    status: "ready", count: 1, warnings: [], wud_status: wudApiStatus(),
    items: [{
      target_id: "target-bazarr", service_key: "media/bazarr", stack: "media", service: "bazarr",
      compose_path: "/stacks/media/compose.yml",
      image: `ghcr.io/example/${"long-repository-name-".repeat(6)}:v1.6.0-ls357`,
      current_tag: "v1.6.0-ls357", runtime_state: "running",
      tracking_regex: String.raw`^v1\.6\.0-ls357$`, tracking_health: "frozen",
      tracking_detail: "The filter matches only the installed version tag.",
      suggested_regex: String.raw`^v\d+\.\d+\.\d+-ls\d+$`,
      wud: null, wud_match_state: "untracked", wud_update_available: null,
      last_image_recorded_at: "", last_action_at: "", last_action_status: "",
      last_action_run_id: null, retag_available: true,
    }],
  };
}

function releaseNotesResponse() {
  return {
    source_file: "/out/images.todo",
    source: pendingSourceInfo(),
    count: 1,
    enabled: true,
    disabled_reason: "",
    notifications_enabled: true,
    notifications_disabled_reason: "",
    warnings: [],
    wud_api: wudApiStatus(),
    items: [
      {
        line_no: 1,
        status: "ready",
        provider: "github",
        image_repo: "repo/app",
        upstream_repo: "repo/app",
        release_tag: "v1.1",
        title: "v1.1",
        published_at: "2026-01-02T00:00:00Z",
        breaking: true,
        breaking_reasons: ["Release notes mention a migration."],
        body: "This release improves library scanning and fixes interrupted imports.\n\nMigration: review the updated configuration before restarting the service.",
        links: [
          {
            label: "GitHub release",
            url: "https://github.com/repo/app/releases/tag/v1.1",
            kind: "github_release",
          },
        ],
        refreshed_at: "2026-01-02T00:00:00Z",
        error: "",
      },
    ],
  };
}

function planResponse(overrides: Record<string, unknown> = {}) {
  return {
    plan_id: "plan-smoke",
    dry_run: true,
    can_apply: true,
    status: "ready",
    source_file: "/out/images.todo",
    source: pendingSourceInfo(),
    mode: "stop",
    max_wait: 120,
    digest_pin_updates: false,
    selected_line_numbers: [1],
    selected_selections: [stackSelection],
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
        actions: [
          {
            kind: "pull",
            description: "pull app",
            cwd: "/docker/media",
            args: ["docker", "compose", "pull", "app"],
          },
        ],
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
          },
        ],
      },
    ],
    skipped: [],
    issues: [],
    cleanup: {
      cleanup_id: "",
      can_remove_unmatched: false,
      items: [],
    },
    apply_preflight: {
      ok: true,
      failures: 0,
      warnings: 0,
      checks: [],
    },
    ...overrides,
  };
}

function jobResponse(status = "queued") {
  return {
    job_id: "job-smoke",
    status,
    run_id: status === "success" ? 7 : null,
    log_file: status === "success" ? "/out/logs/job-smoke.log" : "",
    started_at: "2026-05-28T12:00:00+00:00",
    finished_at: status === "success" ? "2026-05-28T12:00:01+00:00" : null,
    error: "",
    selected_line_numbers: [1],
  };
}

function runSummary() {
  return {
    id: 7,
    started_at: "2026-05-28T12:00:00+00:00",
    finished_at: "2026-05-28T12:00:01+00:00",
    status: "success",
    dry_run: false,
    mode: "stop",
    wud_file: "/out/images.todo",
    log_file: "/out/logs/job-smoke.log",
    metadata: { source: "webui" },
  };
}

function runDetail() {
  return {
    ...runSummary(),
    pending_updates: [
      {
        id: 70,
        run_id: 7,
        line_no: 1,
        raw: "repo/app:1.0 tag=1.1",
        image: "repo/app:1.0",
        target_digest: "",
        desired_tag: "1.1",
        service_key: "media/app",
        stack_name: "media",
        service_name: "app",
        status: "resolved",
        status_reason: "updated by smoke fixture",
        created_at: "2026-05-28T12:00:00+00:00",
        updated_at: "2026-05-28T12:00:01+00:00",
        metadata: {},
      },
    ],
    events: [
      {
        id: 71,
        run_id: 7,
        created_at: "2026-05-28T12:00:01+00:00",
        service_name: "app",
        stack_name: "media",
        image: "repo/app:1.0",
        target_image: "repo/app:1.1",
        old_image_id: "sha256:old",
        new_image_id: "sha256:new",
        old_digest: "sha256:old",
        new_digest: "sha256:new",
        status: "success",
        metadata: {},
      },
    ],
    verification: {
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
    },
  };
}

function runLog() {
  return {
    run_id: 7,
    log_file: "/out/logs/job-smoke.log",
    exists: true,
    content: "[2026-05-28T12:00:01+00:00] Done.\\n",
    truncated: false,
    max_bytes: 262144,
  };
}

async function installApiFixtures(page: Page, state: FixtureState) {
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== appOrigin) {
      await route.abort();
      return;
    }
    if (!url.pathname.startsWith("/api/v1/")) {
      await route.continue();
      return;
    }

    const bodyText = request.postData();
    const call: ApiCall = {
      method: request.method(),
      path: `${url.pathname}${url.search}`,
      headers: request.headers(),
      body: bodyText ? JSON.parse(bodyText) : null,
    };
    state.calls.push(call);

    await fulfillApi(route, state, url.pathname, request.method());
  });
}

async function fulfillApi(
  route: Route,
  state: FixtureState,
  path: string,
  method: string,
) {
  if (path === "/api/v1/auth/csrf") {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: {
        "set-cookie": `wud_csrf_token=${csrfToken}; Path=/; SameSite=Lax`,
      },
      body: JSON.stringify({ csrf_token: csrfToken }),
    });
    return;
  }
  if (path === "/api/v1/auth/session") {
    await json(route, authSession(state));
    return;
  }
  if (path === "/api/v1/auth/login" && method === "POST") {
    state.authenticated = true;
    await json(route, authSession(state));
    return;
  }
  if (path === "/api/v1/auth/logout" && method === "POST") {
    state.authenticated = false;
    await json(route, authSession(state));
    return;
  }
  if (path === "/api/v1/status") {
    await json(route, {
      ok: true,
      version: "test",
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
      mutations_enabled: state.mutationsEnabled,
      timezone: "UTC",
      auto_update_scheduler_enabled: state.mutationsEnabled,
      static_spa_available: true,
      wud_api: wudApiStatus(),
      warnings: [],
    });
    return;
  }
  if (path === "/api/v1/pending") {
    await json(route, pendingResponse());
    return;
  }
  if (path === "/api/v1/update-targets") {
    await json(route, updateTargetsResponse());
    return;
  }
  if (path === "/api/v1/tracked-containers") {
    await json(route, trackedContainersResponse());
    return;
  }
  if (path === "/api/v1/release-notes") {
    await json(route, releaseNotesResponse());
    return;
  }
  if (path === "/api/v1/release-notes/refresh" && method === "POST") {
    await json(route, releaseNotesResponse());
    return;
  }
  if (path === "/api/v1/service-policies") {
    await json(route, []);
    return;
  }
  if (path === "/api/v1/snoozes") {
    await json(route, []);
    return;
  }
  if (path === "/api/v1/tag-exclusions") {
    await json(route, []);
    return;
  }
  if (path === "/api/v1/runs") {
    await json(route, [runSummary()]);
    return;
  }
  if (path === "/api/v1/runs/7") {
    await json(route, runDetail());
    return;
  }
  if (path === "/api/v1/runs/7/log") {
    await json(route, runLog());
    return;
  }
  if (path === "/api/v1/plans" && method === "POST") {
    await json(route, planResponse({ can_apply: state.mutationsEnabled }));
    return;
  }
  if (path === "/api/v1/plans/apply" && method === "POST") {
    await json(route, jobResponse());
    return;
  }
  if (path === "/api/v1/jobs/job-smoke/stream") {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: [
        `event: log\ndata: ${JSON.stringify({
          job_id: "job-smoke",
          log_file: "/out/logs/job-smoke.log",
          exists: true,
          content: "[2026-05-28T12:00:00+00:00] [INFO] docker-update-from-wud-v2\n",
          truncated: false,
          max_bytes: 65536,
          error: "",
        })}\n\n`,
        `event: job\ndata: ${JSON.stringify(jobResponse("success"))}\n\n`,
      ].join(""),
    });
    return;
  }
  if (path === "/api/v1/jobs/job-smoke") {
    await json(route, jobResponse("success"));
    return;
  }

  await route.fulfill({
    status: 404,
    contentType: "application/json",
    body: JSON.stringify({ detail: `unhandled fixture route: ${method} ${path}` }),
  });
}

async function json(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function createState(overrides: Partial<FixtureState> = {}): FixtureState {
  return {
    authenticated: false,
    mutationsEnabled: false,
    calls: [],
    ...overrides,
  };
}

async function sensitiveStorageKeys(page: Page) {
  return page.evaluate(() => ({
    local: Object.keys(localStorage).filter((key) =>
      /(auth|csrf|password|session|token|wud)/i.test(key),
    ),
    session: Object.keys(sessionStorage).filter((key) =>
      /(auth|csrf|password|session|token|wud)/i.test(key),
    ),
  }));
}

async function sidebarForegroundStyles(page: Page, selector: string) {
  return page.locator(selector).evaluate((element) => {
    const normalizeColor = (value: string) => {
      const probe = document.createElement("span");
      probe.style.color = value;
      document.body.append(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      return color;
    };
    const rootStyles = getComputedStyle(document.documentElement);
    return {
      color: getComputedStyle(element).color,
      sidebarText: normalizeColor(
        rootStyles.getPropertyValue("--color-sidebar-text").trim(),
      ),
      surface: normalizeColor(
        rootStyles.getPropertyValue("--color-surface").trim(),
      ),
    };
  });
}

async function expectSidebarForegroundToken(page: Page, selector: string) {
  await expect
    .poll(async () => {
      const styles = await sidebarForegroundStyles(page, selector);
      return (
        styles.color === styles.sidebarText && styles.color !== styles.surface
      );
    })
    .toBe(true);
}

async function expectBrandMarkVisible(
  page: Page,
  selector: string,
  expectedSize: number,
) {
  const mark = page.locator(`${selector} img.app-brand-mark`);
  await expect(mark).toBeVisible();
  await expect(mark).toHaveAttribute("alt", "");
  await expect(mark).toHaveAttribute("aria-hidden", "true");
  await expect
    .poll(() =>
      mark.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        return {
          complete: element instanceof HTMLImageElement && element.complete,
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        };
      }),
    )
    .toEqual({ complete: true, width: expectedSize, height: expectedSize });
}

async function mobileShellLayout(page: Page) {
  return page.evaluate(() => {
    const bounds = (selector: string) => {
      const element = document.querySelector(selector);
      if (!(element instanceof HTMLElement)) {
        throw new Error(`Missing mobile shell element: ${selector}`);
      }
      const rect = element.getBoundingClientRect();
      return {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        height: rect.height,
      };
    };

    return {
      innerWidth: window.innerWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      sidebar: bounds(".sidebar"),
      navList: bounds(".nav-list"),
      mainPanel: bounds(".main-panel"),
      labels: Array.from(document.querySelectorAll<HTMLElement>(".nav-item span")).map(
        (element) => {
          const rect = element.getBoundingClientRect();
          return {
            text: element.textContent?.trim() ?? "",
            width: rect.width,
            height: rect.height,
          };
        },
      ),
    };
  });
}

async function expectMobileShellLayout(page: Page, viewportWidth: number) {
  const snapshot = await mobileShellLayout(page);
  expect(snapshot.innerWidth).toBe(viewportWidth);
  expect(snapshot.documentScrollWidth).toBeLessThanOrEqual(viewportWidth);
  expect(snapshot.labels.map((label) => label.text)).toEqual(mobileNavLabels);

  for (const label of snapshot.labels) {
    expect(label.width, `${label.text} label width`).toBeGreaterThan(0);
    expect(label.height, `${label.text} label height`).toBeGreaterThan(0);
  }

  for (const [name, rect] of Object.entries({
    sidebar: snapshot.sidebar,
    navList: snapshot.navList,
    mainPanel: snapshot.mainPanel,
  })) {
    expect(rect.left, `${name} left`).toBeGreaterThanOrEqual(0);
    expect(rect.right, `${name} right`).toBeLessThanOrEqual(viewportWidth + 1);
    expect(rect.width, `${name} width`).toBeGreaterThan(0);
  }

  expect(snapshot.navList.bottom).toBeLessThanOrEqual(snapshot.sidebar.bottom + 1);
  expect(snapshot.mainPanel.top).toBeGreaterThanOrEqual(snapshot.sidebar.bottom - 1);
}

async function expectMobileNavFocusTraversal(page: Page) {
  const navItems = page.locator(".nav-item");
  const navItemCount = await navItems.count();
  expect(navItemCount).toBe(mobileNavLabels.length);

  await page.locator(".brand").focus();
  await page.locator(".nav-list").evaluate((element) => {
    element.scrollLeft = 0;
  });

  const focusSteps = [];
  for (let index = 0; index < navItemCount; index += 1) {
    await page.keyboard.press("Tab");
    await expect(navItems.nth(index)).toBeFocused();
    const step = await navItems.nth(index).evaluate((element) => {
      const navList = element.closest(".nav-list");
      if (!(navList instanceof HTMLElement)) {
        throw new Error("Missing mobile nav list");
      }
      const itemRect = element.getBoundingClientRect();
      const navRect = navList.getBoundingClientRect();
      const styles = getComputedStyle(element);
      const outlineClearance = Math.max(
        0,
        (Number.parseFloat(styles.outlineWidth) || 0) +
          (Number.parseFloat(styles.outlineOffset) || 0),
      );
      return {
        label: element.textContent?.trim() ?? "",
        left: itemRect.left,
        right: itemRect.right,
        width: itemRect.width,
        height: itemRect.height,
        outlineClearance,
        navLeft: navRect.left,
        navRight: navRect.right,
        scrollLeft: navList.scrollLeft,
        scrollWidth: navList.scrollWidth,
        clientWidth: navList.clientWidth,
        viewportWidth: window.innerWidth,
      };
    });

    focusSteps.push(step);
    expect(step.label).toBe(mobileNavLabels[index]);
    expect(step.width, `${step.label} focused width`).toBeGreaterThan(0);
    expect(step.height, `${step.label} focused height`).toBeGreaterThan(0);
    expect(
      step.left - step.outlineClearance,
      `${step.label} focus outline left`,
    ).toBeGreaterThanOrEqual(step.navLeft - 1);
    expect(
      step.right + step.outlineClearance,
      `${step.label} focus outline right`,
    ).toBeLessThanOrEqual(step.navRight + 1);
    expect(
      step.left - step.outlineClearance,
      `${step.label} viewport focus outline left`,
    ).toBeGreaterThanOrEqual(0);
    expect(
      step.right + step.outlineClearance,
      `${step.label} viewport focus outline right`,
    ).toBeLessThanOrEqual(
      step.viewportWidth + 1,
    );
  }

  const firstStep = focusSteps[0];
  if (!firstStep) {
    throw new Error("Mobile nav focus traversal did not run");
  }
  expect(firstStep.scrollLeft).toBe(0);
  expect(firstStep.scrollWidth).toBeGreaterThan(firstStep.clientWidth);
  expect(Math.max(...focusSteps.map((step) => step.scrollLeft))).toBeGreaterThan(0);
}

test.beforeEach(async ({ context }) => {
  await context.clearCookies();
});

test("unauthenticated protected routes redirect to login", async ({ page }) => {
  const state = createState();
  await installApiFixtures(page, state);

  await page.goto("/#/pending");

  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("login from a protected route uses native credential fields and returns there", async ({
  page,
}) => {
  const state = createState();
  await installApiFixtures(page, state);

  await page.goto("/#/pending");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();

  const username = page.locator('input[name="username"]');
  const password = page.locator('input[name="password"]');
  await expect(username).toHaveAttribute("id", "login-username");
  await expect(username).toHaveAttribute("autocomplete", "username");
  await expect(password).toHaveAttribute("id", "login-password");
  await expect(password).toHaveAttribute("autocomplete", "current-password");
  await expect(page.locator('label[for="login-username"]')).toHaveText("Username");
  await expect(page.locator('label[for="login-password"]')).toHaveText("Password");

  await username.fill("admin");
  await password.fill("password");
  await page.getByRole("button", { name: /Sign in/ }).click();

  await expect(page.getByRole("heading", { name: "Pending updates", exact: true })).toBeVisible();
  expect(state.calls.filter((call) => call.path === "/api/v1/auth/session")).toHaveLength(1);
});

test("login requests csrf and does not store secrets in browser storage", async ({ page }) => {
  const state = createState();
  await installApiFixtures(page, state);

  await page.goto("/#/login");
  await page.getByRole("textbox").nth(0).fill("admin");
  await page.getByRole("textbox").nth(1).fill("password");
  await page.getByRole("button", { name: /Sign in/ }).click();

  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  expect(state.calls.map((call) => call.path)).toContain("/api/v1/auth/csrf");
  expect(state.calls.map((call) => call.path)).toContain("/api/v1/auth/login");
  await expect.poll(() => sensitiveStorageKeys(page)).toEqual({
    local: [],
    session: [],
  });
});

test("read-only pending flow can preflight a stack but cannot apply", async ({ page }) => {
  const state = createState({ authenticated: true, mutationsEnabled: false });
  await installApiFixtures(page, state);

  await page.goto("/#/pending");
  await page.getByRole("checkbox", { name: /Select stack media/ }).check();
  await page.getByRole("button", { name: /Review selected \(/ }).click();

  await expect(page.getByRole("dialog").getByText("Read-only mode is active").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Apply blocked" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Apply 1 update/ })).toBeDisabled();
  expect(state.calls.some((call) => call.path === "/api/v1/plans")).toBe(true);
  expect(state.calls.some((call) => call.path === "/api/v1/plans/apply")).toBe(false);
});

for (const kind of ["removal", "cleanup"] as const) {
  test(`pending ${kind} confirmation supports keyboard focus and cancel`, async ({ page }) => {
    const state = createState({ authenticated: true, mutationsEnabled: true });
    await installApiFixtures(page, state);
    const line = { line_no: 1, raw: "repo/old:latest", image: "repo/old:latest", desired_tag: "", digest: "" };
    if (kind === "removal") {
      await page.route("**/api/v1/pending/removal-plan", (route) => json(route, {
        removal_id: "removal-smoke",
        source_file: "/out/images.todo",
        can_remove: true,
        selected_line_numbers: [1],
        lines: [line],
      }));
    } else {
      await page.route("**/api/v1/plans", (route) => json(route, planResponse({
        cleanup: {
          cleanup_id: "cleanup-smoke",
          can_remove_unmatched: true,
          items: [{ ...line, reason: "unmatched", diagnostic: null }],
        },
      })));
    }
    await page.goto("/#/pending");
    await page.getByRole("checkbox", { name: /Select stack media/ }).check();
    if (kind === "removal") {
      await page.getByText("Queue details and actions", { exact: true }).click();
      await page.getByRole("button", { name: "Remove 1 selected entry", exact: true }).click();
    } else {
      await page.getByRole("button", { name: /Review selected \(/ }).click();
      await page.getByRole("dialog").getByRole("button", { name: "Remove 1 unmatched entry", exact: true }).click();
    }
    const dialog = page.getByRole("dialog", {
      name: kind === "removal" ? "Remove selected entries" : "Remove unmatched entries",
      exact: true,
    });
    const cancel = dialog.getByRole("button", { name: "Cancel", exact: true });
    const confirm = dialog.getByRole("button", { name: /Remove 1/ });
    await cancel.focus();
    await expect(cancel).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(confirm).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(cancel).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(dialog).toBeHidden();
    expect(state.calls.some((call) => ["/api/v1/pending/removal", "/api/v1/pending/cleanup"].includes(call.path))).toBe(false);
  });
}

test("mutation-enabled pending flow applies and links to run details", async ({
  page,
}) => {
  const state = createState({ authenticated: true, mutationsEnabled: true });
  await installApiFixtures(page, state);

  await page.goto("/#/pending");
  await page.locator('summary[aria-label="Details for media"]').click();
  await expect(
    page.getByRole("textbox", { name: "New tag for repo/app:1.0" }),
  ).toBeVisible();
  await page.getByRole("button", { name: /Review media plan/ }).click();

  await expect(page.getByRole("heading", { name: "Review media plan" })).toBeVisible();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /Apply 1 update/ })
    .click();

  const applyPanel = page.locator(".apply-job-panel");
  await expect(
    applyPanel.getByRole("heading", { name: "Apply complete" }),
  ).toBeVisible();
  await expect(applyPanel.locator(".apply-job-latest-log code")).toContainText(
    "docker-update-from-wud-v2",
  );
  await expect(applyPanel.locator(".apply-job-log-viewer")).toBeHidden();
  await expect(applyPanel.getByRole("button", { name: "Show output" })).toBeVisible();
  await applyPanel.getByRole("button", { name: "Show output" }).click();
  await expect(applyPanel.locator(".apply-job-log-viewer")).toBeVisible();
  await expect(applyPanel.locator(".apply-job-log-viewer")).toContainText(
    "docker-update-from-wud-v2",
  );
  await expect(applyPanel.getByRole("link", { name: "Details" })).toBeVisible();
  await expect(applyPanel.getByRole("link", { name: "Log" })).toBeVisible();
  expect(state.calls.some((call) => call.path === "/api/v1/plans/apply")).toBe(true);
  expect(
    state.calls.some((call) =>
      call.path.startsWith("/api/v1/jobs/job-smoke/stream?"),
    ),
  ).toBe(true);

  await applyPanel.getByRole("link", { name: "Details" }).click();
  await expect(page.getByRole("heading", { name: "#7" })).toBeVisible();
  await expect(page.getByText("Pending records")).toBeVisible();

  await page.getByRole("link", { name: "View log" }).click();
  await expect(page.getByRole("heading", { name: "#7 log" })).toBeVisible();
  await expect(page.getByText("Done.")).toBeVisible();
});

test("mobile shell keeps page width stable and preserves link targets", async ({
  page,
}) => {
  const state = createState({ authenticated: true, mutationsEnabled: false });
  await installApiFixtures(page, state);

  for (const viewportWidth of [320, 390]) {
    await page.setViewportSize({ width: viewportWidth, height: 844 });
    await page.goto("/#/");
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

    await expect(page.locator(".brand")).toHaveAttribute(
      "aria-label",
      "WUDup dashboard",
    );
    await expectBrandMarkVisible(page, ".brand", 28);
    await expectMobileShellLayout(page, viewportWidth);

    if (viewportWidth === 320) {
      await expectMobileNavFocusTraversal(page);
    }
  }

  const pendingLinkBox = await page
    .locator('a.text-link[href="#/pending"]')
    .boundingBox();
  const historyLinkBox = await page
    .locator('a.text-link[href="#/runs"]')
    .boundingBox();
  if (pendingLinkBox === null || historyLinkBox === null) {
    throw new Error("Expected mobile navigation text links to have bounding boxes");
  }
  expect(pendingLinkBox.height).toBeGreaterThanOrEqual(touchTargetSizePx);
  expect(historyLinkBox.height).toBeGreaterThanOrEqual(touchTargetSizePx);

  await page.goto("/#/pending");
  await expect(page.getByRole("checkbox", { name: /Select stack media/ })).toBeVisible();
  await page.getByRole("button", { name: /^Release notes for/ }).click();
  const releasePanel = page.getByRole("dialog", { name: "Release notes", exact: true });
  await expect(releasePanel.getByText("Possible breaking change")).toBeVisible();
  await releasePanel.getByRole("button", { name: "Close release notes" }).click();
  await expect
    .poll(() =>
      page.evaluate(() => ({
        innerWidth: window.innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
      })),
    )
    .toEqual({ innerWidth: 390, scrollWidth: 390 });
});

test("selection toolbar stays compact without hiding selection scope or actions", async ({ page }) => {
  const state = createState({ authenticated: true, mutationsEnabled: false });
  await installApiFixtures(page, state);

  for (const width of [320, 390, 844, 1280]) {
    await page.setViewportSize({ width, height: width === 844 ? 390 : 844 });
    await page.goto("/#/pending");
    const toolbar = page.getByRole("region", { name: "Review updates" });
    const review = toolbar.getByRole("button", { name: /Review selected \(/ });
    await expect(review).toBeDisabled();
    await page.getByRole("checkbox", { name: "Select stack media", exact: true }).check();
    await expect(review).toBeEnabled();
    const clear = toolbar.getByRole("button", { name: "Clear selection", exact: true });

    if (width <= 390) {
      const toolbarBox = await toolbar.boundingBox();
      const clearBox = await clear.boundingBox();
      const reviewBox = await review.boundingBox();
      expect(toolbarBox!.height).toBeLessThanOrEqual(125);
      expect(clearBox!.height).toBeGreaterThanOrEqual(touchTargetSizePx);
      expect(reviewBox!.height).toBeGreaterThanOrEqual(touchTargetSizePx);
      expect(clearBox!.y).toBe(reviewBox!.y);
      expect(clearBox!.x + clearBox!.width).toBeLessThan(reviewBox!.x);
      expect(reviewBox!.x + reviewBox!.width).toBeLessThanOrEqual(width);
    }

    await page.getByRole("textbox", { name: "Search pending updates" }).fill("no-match");
    await expect(toolbar).toContainText("1 selected update hidden by search; included in review.");
    await clear.focus();
    await page.keyboard.press("Enter");
    await expect(review).toBeDisabled();
    await expect(toolbar).not.toContainText("hidden by search");
    await page.getByText("Queue details and actions", { exact: true }).click();
    await expect(page.getByRole("button", { name: "Rescan WUD", exact: true })).toBeVisible();
    await page.getByText("Queue details and actions", { exact: true }).click();
    await page.getByRole("textbox", { name: "Search pending updates" }).fill("");
  }
  expect(state.calls.some((call) => call.path === "/api/v1/plans/apply")).toBe(false);
});

test("theme toggle follows system dark mode and cycles preferences", async ({
  page,
}) => {
  const state = createState({ authenticated: true, mutationsEnabled: false });
  await page.emulateMedia({ colorScheme: "dark" });
  await installApiFixtures(page, state);

  await page.goto("/#/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect
    .poll(() =>
      page.evaluate(() =>
        getComputedStyle(document.documentElement)
          .getPropertyValue("--color-body-bg")
          .trim(),
      ),
    )
    .toBe("#0f171a");
  await expectSidebarForegroundToken(page, ".nav-item.router-link-active");

  await page.getByRole("button", { name: /System theme/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expectSidebarForegroundToken(page, ".nav-item.router-link-active");
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("theme-preference")))
    .toBe("light");

  await page.getByRole("button", { name: /Light theme/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("theme-preference")))
    .toBe("dark");

  await page.getByRole("button", { name: /Dark theme/ }).click();
  await expect(page.getByRole("button", { name: /System theme/ })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("theme-preference")))
    .toBe("auto");
  await expect.poll(() => sensitiveStorageKeys(page)).toEqual({
    local: [],
    session: [],
  });
});

test("login mark renders brand image in light and dark themes", async ({
  page,
}) => {
  const state = createState();
  await page.emulateMedia({ colorScheme: "dark" });
  await installApiFixtures(page, state);

  await page.goto("/#/login");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expectBrandMarkVisible(page, ".auth-mark", 36);

  await page.evaluate(() => localStorage.setItem("theme-preference", "light"));
  await page.reload();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expectBrandMarkVisible(page, ".auth-mark", 36);
});

test("mutation-enabled pending flow creates jobs only after confirmation", async ({
  page,
}) => {
  const state = createState({ authenticated: true, mutationsEnabled: true });
  await installApiFixtures(page, state);

  await page.goto("/#/pending");
  await page.getByRole("checkbox", { name: /Select stack media/ }).check();
  await page.getByRole("button", { name: /Review selected \(/ }).click();
  await expect(page.getByRole("heading", { name: "Review media plan" })).toBeVisible();

  const dialog = page.getByRole("dialog").filter({
    hasText: "Review media plan",
  });
  await expect(dialog).toBeVisible();
  expect(state.calls.some((call) => call.path === "/api/v1/plans/apply")).toBe(false);

  await dialog.getByRole("button", { name: "Apply 1 update" }).click();

  const planCall = state.calls.find((call) => call.path === "/api/v1/plans");
  const applyCall = state.calls.find((call) => call.path === "/api/v1/plans/apply");
  expect(planCall?.headers["x-wud-csrf-token"]).toBe(csrfToken);
  expect(planCall?.body).toEqual({
    selections: [stackSelection],
    allow_tag_updates: true,
    tag_overrides: [],
    tag_stream_decisions: [],
    tag_stream_label_rewrite_approvals: [],
    digest_pin_label_rewrite_approvals: [],
  });
  expect(applyCall?.headers["x-wud-csrf-token"]).toBe(csrfToken);
  expect(applyCall?.body).toEqual({
    plan_id: "plan-smoke",
    selections: [stackSelection],
    allow_tag_updates: true,
    tag_overrides: [],
    tag_stream_decisions: [],
    tag_stream_label_rewrite_approvals: [],
    digest_pin_label_rewrite_approvals: [],
    confirmation: "apply",
  });
});

test("logout returns to login and leaves storage empty", async ({ page }) => {
  const state = createState({ authenticated: true });
  await installApiFixtures(page, state);

  await page.goto("/");
  await page.getByRole("button", { name: /Sign out/ }).click();

  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  expect(state.calls.map((call) => call.path)).toContain("/api/v1/auth/logout");
  await expect.poll(() => sensitiveStorageKeys(page)).toEqual({
    local: [],
    session: [],
  });
});

for (const width of [1280, 815, 390]) {
  test(`tracking examples stay contained and keyboard-accessible at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const state = createState({ authenticated: true });
    await installApiFixtures(page, state);
    await page.goto("/#/containers");
    const inspect = page.getByRole("button", { name: width > 768 ? "Inspect media/bazarr" : "Inspect bazarr", exact: true });
    await expect(inspect).toBeVisible();
    const noOverflow = () => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
    expect(await noOverflow()).toBe(true);
    await inspect.click();
    await expect(page.getByText("/stacks/media/compose.yml")).toBeVisible();
    if (width === 390) {
      const tapHeights = await page.locator(".tracked-links a, .tracked-links summary")
        .evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().height));
      expect(tapHeights.every((height) => height >= 44)).toBe(true);
      const previewWidth = await page.getByRole("button", { name: "Preview repair" })
        .evaluate((element) => element.getBoundingClientRect().width);
      expect(previewWidth).toBeGreaterThan(250);
    }
    const detail = page.locator(".tracked-pattern-details");
    await expect(detail).not.toHaveAttribute("open");
    await detail.locator("summary").focus();
    await page.keyboard.press("Enter");
    await expect(detail).toHaveAttribute("open");
    const examples = page.getByRole("table", { name: "Illustrative tag matches" });
    await expect(examples.getByRole("row").filter({ hasText: "v1.6.1-ls357" })).toContainText("Matches");
    await expect(examples.getByRole("row").filter({ hasText: "Without the suffix" })).toContainText("Excluded");
    await expect(page.getByRole("textbox", { name: "Tag to test against proposed filter" })).toHaveAttribute("placeholder", "e.g. v1.6.1-ls357");
    await expect(detail).toContainText("followed by");
    await page.keyboard.press("Enter");
    await expect(detail).not.toHaveAttribute("open");
    expect(await noOverflow()).toBe(true);
    await page.locator(".tracked-pattern-guide").screenshot({ path: testInfo.outputPath("tracking-examples.png") });
    await page.getByRole("textbox", { name: "Proposed WUD tag regex" }).fill(String.raw`^v1\.6\.\d+-ls357$`);
    await detail.locator("summary").click();
    await expect(examples.getByRole("row").filter({ hasText: "v2.0.0-ls357" })).toContainText("Excluded");
    await page.getByRole("textbox", { name: "Proposed WUD tag regex" }).fill(`^${"long-fixed-prefix-".repeat(6)}\\d+$`);
    expect(await noOverflow()).toBe(true);
    expect(state.calls.some((call) => call.path === "/api/v1/tracking-repairs/apply")).toBe(false);
  });
}

test("mobile tracking repair keeps focus and progress beside the service", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = createState({ authenticated: true, mutationsEnabled: true });
  await installApiFixtures(page, state);
  const plan = {
    plan_id: "tracking-plan", source_hash: "source", rendered_hash: "rendered",
    target_id: "target-bazarr", service_key: "media/bazarr", stack: "media",
    service: "bazarr", image: "repo/bazarr:v1.6.0-ls357",
    current_regex: String.raw`^v1\.6\.0-ls357$`,
    proposed_regex: String.raw`^v\d+\.\d+\.\d+-ls\d+$`,
    compose_diff: "wud.tag.include:\n- (set) old\n+ (set) new",
    will_recreate: true, can_apply: true, issues: [],
  };
  const trackingJob = (status: string) => ({
    ...jobResponse(status), job_id: "job-tracking",
    progress: [{ message: "Recreating bazarr" }],
  });
  let finishPoll!: () => void;
  const pollGate = new Promise<void>((resolve) => { finishPoll = resolve; });
  await page.route("**/api/v1/tracking-repairs", (route) => json(route, plan));
  await page.route("**/api/v1/tracking-repairs/apply", (route) => json(route, trackingJob("running")));
  await page.route("**/api/v1/apply-jobs/job-tracking", async (route) => {
    await pollGate;
    await json(route, trackingJob("success"));
  });

  await page.goto("/#/containers");
  await page.getByRole("button", { name: "Inspect bazarr" }).click();
  await page.getByRole("button", { name: "Preview repair" }).click();
  await page.getByRole("checkbox", { name: /I reviewed the diff/ }).check();
  await page.getByRole("button", { name: "Apply tracking repair" }).click();

  const status = page.getByLabel("Tracking repair status");
  try {
    await expect(status).toBeFocused();
    await expect(status).toContainText("still running");
    await expect(page.getByLabel("Tracking repair preview")).toHaveCount(0);
    await expect.poll(() => status.evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      return bounds.top >= 60 && bounds.bottom <= window.innerHeight;
    })).toBe(true);
  } finally {
    finishPoll();
  }
  await expect(status).toContainText("Tracking repaired for bazarr");
});

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test.describe(`release review at ${viewport.width}px`, () => {
    test.use({ viewport, hasTouch: viewport.width < 560 });
    test("keeps candidate context, keyboard focus, and plan selection", async ({ page }, testInfo) => {
      const state = createState({ authenticated: true, mutationsEnabled: false });
      await installApiFixtures(page, state);
      await page.goto("/#/pending");
      const trigger = page.locator(".stack-change-release").getByRole("button", { name: /^Release notes for/ });
      await expect(trigger).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("pending-release-actions.png") });
      await expect(page.locator(".stack-details")).not.toHaveAttribute("open");
      const panel = page.getByRole("dialog", { name: "Release notes", exact: true });
      if (viewport.width < 560) {
        await trigger.tap();
      } else {
        await trigger.focus();
        await page.keyboard.press("Enter");
      }
      await expect(panel).toBeVisible();
      await expect(panel.getByRole("button", { name: "Close release notes" })).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(panel.getByRole("link", { name: "GitHub release", exact: true })).toBeFocused();
      await panel.screenshot({ path: testInfo.outputPath("release-panel.png"), animations: "disabled" });
      await page.keyboard.press("Escape");
      await expect(panel).not.toBeVisible();
      await expect(trigger).toBeFocused();
      await page.getByRole("checkbox", { name: /Select stack media/ }).check();
      await page.getByRole("button", { name: /Review selected \(/ }).click();
      const plan = page.getByRole("dialog", { name: "Apply blocked", exact: true });
      await expect(plan).toBeVisible();
      const planTrigger = plan.getByRole("button", { name: /^Release notes for/ });
      await planTrigger.click();
      await expect(panel).toBeVisible();
      await expect(panel.getByRole("button", { name: "Close release notes" })).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(panel).not.toBeVisible();
      await expect(plan).toBeVisible();
      await expect(planTrigger).toBeFocused();
      await plan.getByRole("button", { name: "Close", exact: true }).click();
      await expect(page.getByRole("checkbox", { name: /Select stack media/ })).toBeChecked();
      expect(state.calls.some((call) => call.path === "/api/v1/plans/apply")).toBe(false);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    });
  });
}
