const { danger, markdown, schedule, warn } = require("danger");

const changedFiles = unique([
  ...danger.git.created_files,
  ...danger.git.modified_files,
  ...danger.git.deleted_files,
]);
const editedFiles = unique([...danger.git.created_files, ...danger.git.modified_files]);
const pr = danger.github?.pr ?? {};
const prBody = pr.body ?? "";
const prBodyLower = prBody.toLowerCase();
const prAuthor = pr.user?.login ?? "";
const globCache = new Map();
const LARGE_FILE_IGNORE_PATTERNS = [
  "**/package-lock.json",
  "**/npm-shrinkwrap.json",
  "**/yarn.lock",
  "**/pnpm-lock.yaml",
  "**/Cargo.lock",
  "**/Pipfile.lock",
  "**/poetry.lock",
  "**/composer.lock",
  "**/Gemfile.lock",
  "dangerfile.js",
  ".github/**",
  "webui/dist/**",
  "webui/coverage/**",
  "**/node_modules/**",
  "**/vendor/**",
  "**/.cargo/**",
  "**/__pycache__/**",
  "**/*.pyc",
  "CHANGELOG.md",
];

const releasePleaseBranch =
  typeof pr.head?.ref === "string" &&
  pr.head.ref.startsWith("release-please--branches--");
const releasePleaseTitle = /^chore: release \d+\.\d+\.\d+ \[skip ci\]/i.test(
  pr.title ?? "",
);
const dependencyBot =
  prAuthor === "dependabot[bot]" ||
  prAuthor === "app/dependabot" ||
  prAuthor === "renovate[bot]";
const COMPANION_TEST_RULES = [
  [
    "frontend-api-tests-not-needed",
    "Frontend API or store contract changed; confirm test evidence.",
    `
      webui/src/api/**
      webui/src/stores/**
      webui/src/composables/**
    `,
    `
      webui/tests/**
      tests/test_python_web*.py
    `,
    "Add companion Vitest/API/store or backend contract tests, rely on a passing `codecov/patch/webui-contract` patch coverage check, or add `Danger: frontend-api-tests-not-needed` with the rationale.",
  ],
  [
    "webui-mutation-tests-not-needed",
    "Auth, CSRF, read-only, scheduler, or mutation UX files changed without guard tests.",
    `
      src/wudup/web.py
      src/wudup/web_auth.py
      src/wudup/web_diagnostics.py
      src/wudup/web_jobs.py
      src/wudup/web_pending.py
      src/wudup/web_plans.py
      src/wudup/web_retags.py
      src/wudup/web_scheduler.py
      src/wudup/web_self_update.py
      src/wudup/web_settings.py
      src/wudup/web_state.py
      webui/src/stores/auth.ts
      webui/src/stores/connection.ts
      webui/src/stores/settings.ts
      webui/src/stores/updates.ts
      webui/src/views/PendingView.vue
      webui/src/views/PoliciesView.vue
      webui/src/views/RetagsView.vue
      webui/src/views/SettingsView.vue
      webui/src/views/SnoozesView.vue
      webui/src/views/TagExclusionsView.vue
      webui/src/views/pending/**
      webui/src/views/settings/**
      webui/src/components/app/AppSelfUpdate*.vue
      webui/src/components/pending/**
      webui/src/components/retags/**
    `,
    `
      tests/test_python_web_apply_endpoint_guards.py
      tests/test_python_web_auth_*.py
      tests/test_python_web_diagnostics.py
      tests/test_python_web_jobs.py
      tests/test_python_web_pending_*.py
      tests/test_python_web_plan_*.py
      tests/test_python_web_retags.py
      tests/test_python_web_scheduler_*.py
      tests/test_python_web_self_update_*.py
      tests/test_python_web_state_*.py
      webui/tests/auth-store.test.ts
      webui/tests/connection-store.test.ts
      webui/tests/pending-view-*.test.ts
      webui/tests/RetagsView.test.ts
      webui/tests/settings-mutation-views.test.ts
      webui/tests/settings-store.test.ts
      webui/tests/updates-store.test.ts
    `,
    "Recent reviews caught missing read-only/mutation guards. Add focused auth/CSRF/read-only tests or add `Danger: webui-mutation-tests-not-needed` with the reason.",
  ],
  [
    "compose-rewrite-tests-not-needed",
    "Compose rewrite, digest, or tag logic changed without focused tests.",
    `
      src/wudup/compose_rewrite.py
      src/wudup/compose.py
      src/wudup/digest_provenance.py
      src/wudup/digest_verifier.py
      src/wudup/images.py
      src/wudup/plan_actions.py
      src/wudup/plan_*.py
      src/wudup/plan_digest_unpin.py
      src/wudup/plan_matching.py
      src/wudup/plans.py
      src/wudup/updater_digest*.py
      src/wudup/updater_lifecycle_digest.py
      src/wudup/updater_lifecycle_rewrite.py
      src/wudup/updater_models.py
      src/wudup/updater_runner_*.py
      src/wudup/updater_tag_exclusions.py
      src/wudup/wud_file.py
      webui/src/utils/digestProvenance.ts
    `,
    `
      tests/test_python_compose_*.py
      tests/test_python_digest_verifier.py
      tests/test_python_update_from_wud_*digest*.py
      tests/test_python_update_from_wud_*tag*.py
      tests/test_python_updater_digest*.py
      tests/test_python_wud_parsing.py
    `,
    "Digest/tag rewrites are rollback-sensitive. Add focused compose/updater coverage or add `Danger: compose-rewrite-tests-not-needed` with the reason.",
  ],
  [
    "demo-fixture-tests-not-needed",
    "Static demo API or fixture state changed without demo tests.",
    `
      webui/src/api/demo/**
      webui/scripts/seed_demo_state.py
      tests/test_python_webui_demo_state.py
    `,
    `
      webui/tests/demo-api.test.ts
      webui/tests/static-security.test.ts
      tests/test_python_webui_demo_state.py
    `,
    "Recent review feedback caught broad fixture replacements and duplicate cleanup handling. Add demo coverage or add `Danger: demo-fixture-tests-not-needed` with the reason.",
  ],
  [
    "responsive-tests-not-needed",
    "Responsive breakpoint code changed without responsive or smoke tests.",
    `
      webui/src/responsive.ts
      webui/src/assets/styles/responsive.css
      webui/src/assets/styles/foundation.css
    `,
    `
      webui/tests/responsive.test.ts
      webui/tests/smoke/**
    `,
    "Recent reviews caught JS/CSS breakpoint parity issues. Add responsive coverage, a smoke check, or add `Danger: responsive-tests-not-needed` with the reason.",
  ],
].map(companionTestRule);

if (releasePleaseBranch || releasePleaseTitle || dependencyBot) {
  markdown(
    "Danger maintainability review skipped for release automation or dependency bot PR.",
  );
} else {
  runCompanionTestRules();
  runReviewPromptRules();
  schedule(async () => {
    await runDiffContentRules();
    await runLargeFileRules();
  });
}

function runCompanionTestRules() {
  for (const rule of COMPANION_TEST_RULES) {
    warnWhenNoCompanionTests(rule);
  }
}

function runReviewPromptRules() {
  const dockerFiles = changedMatching([
    ".dockerignore",
    "Dockerfile",
    "entrypoint.sh",
    "docs/examples/docker-compose*.yml",
    "src/wudup/compose.py",
    "src/wudup/digest_verifier.py",
    "src/wudup/docker_cli.py",
    "src/wudup/doctor.py",
    "src/wudup/plan_actions.py",
    "src/wudup/self_update.py",
    "src/wudup/updates.py",
    "src/wudup/updater_cli.py",
    "src/wudup/updater_lifecycle*.py",
    "src/wudup/updater_runner_*.py",
    "src/wudup/web_health.py",
    "src/wudup/web_jobs.py",
    "src/wudup/web_scheduler.py",
    "src/wudup/web_self_update.py",
    "bin/docker-update-from-wud",
    "bin/updates",
    "tests/container-build.sh",
    "tests/e2e-docker-compose.sh",
    "tests/test-entrypoint.sh",
  ]);

  if (
    dockerFiles.length > 0 &&
    !acknowledged("docker-review-complete") &&
    !hasCheckedDockerValidation()
  ) {
    warn(
      [
        "Docker, Compose, or socket-adjacent files changed. Confirm Docker/Compose validation in the PR test plan, or add `Danger: docker-review-complete` with the reason.",
        "",
        `Changed: ${formatFiles(dockerFiles)}`,
      ].join("\n"),
    );
  }

  const publicConfigFiles = changedMatching([
    "AGENTS.md",
    ".github/CODEOWNERS",
    ".github/workflows/**",
    "dangerfile.js",
    "pyproject.toml",
    "template.env",
    "docs/examples/*.env*",
  ]);

  if (publicConfigFiles.length > 0 && !acknowledged("config-review-complete")) {
    warn(
      [
        "Review repo-facing configuration changes for path privacy, pinned actions, and documented validation.",
        "",
        `Changed: ${formatFiles(publicConfigFiles)}`,
        "",
        "Add `Danger: config-review-complete` if the review is complete and no further issue is needed.",
      ].join("\n"),
    );
  }
}

async function runDiffContentRules() {
  await warnOnNewRoutes();
  await warnOnDemoReplaceAll();
  await warnOnResponsiveSmaller();
  await warnOnVueLabelWrappingControl();
  await warnOnDockerSocketText();
}

async function warnOnNewRoutes() {
  if (!editedFiles.includes("src/wudup/web.py")) {
    return;
  }

  const added = await addedLinesForFile("src/wudup/web.py");
  const routeAdded = added.some((line) => {
    return (
      /\b(add_api_route|include_router|APIRouter)\b/.test(line) ||
      /["']\/api\/v1\//.test(line)
    );
  });

  if (!routeAdded || acknowledged("route-review-complete")) {
    return;
  }

  warn(
    [
      "New or changed WebUI route wiring detected. Confirm auth, CSRF/Origin, read-only mode, audit behavior, and matching backend/frontend tests.",
      "",
      "Add `Danger: route-review-complete` if this route review is complete.",
    ].join("\n"),
  );
}

async function warnOnDemoReplaceAll() {
  const demoFiles = changedMatching(["webui/src/api/demo/**"], editedFiles);
  for (const file of demoFiles) {
    const added = await addedLinesForFile(file);
    if (
      added.some((line) => /\.replaceAll\(/.test(line)) &&
      !acknowledged("demo-replacement-review-complete")
    ) {
      warn(
        [
          `\`${file}\` adds \`replaceAll\` in demo fixture code. Prefer generated unique tokens over replacing raw tag/version strings globally.`,
          "",
          "Add `Danger: demo-replacement-review-complete` if broad replacement is intentional and covered.",
        ].join("\n"),
      );
    }
  }
}

async function warnOnResponsiveSmaller() {
  if (
    !editedFiles.includes("webui/src/responsive.ts") ||
    acknowledged("responsive-breakpoint-review-complete")
  ) {
    return;
  }

  const added = await addedLinesForFile("webui/src/responsive.ts");
  if (added.some((line) => /\.smaller\(/.test(line))) {
    warn(
      [
        "`webui/src/responsive.ts` adds a VueUse `smaller()` breakpoint. CSS `max-width` custom media is inclusive, so use `smallerOrEqual()` or document the intentional boundary difference.",
        "",
        "Add `Danger: responsive-breakpoint-review-complete` if reviewed.",
      ].join("\n"),
    );
  }
}

async function warnOnVueLabelWrappingControl() {
  if (acknowledged("vue-label-association-review-complete")) {
    return;
  }

  const vueFiles = changedMatching(["webui/src/**/*.vue"], editedFiles);
  for (const file of vueFiles) {
    const added = await addedLinesForFile(file);
    const addedText = added.join("\n");
    if (/<label[\s>]/.test(addedText) && /<n-(switch|checkbox|radio|select|input)\b/.test(addedText)) {
      warn(
        [
          `\`${file}\` adds a label around a Naive UI form control. Confirm the control has reliable accessible naming via \`for\`/\`id\`, \`aria-labelledby\`, or \`aria-label\`.`,
          "",
          "Add `Danger: vue-label-association-review-complete` if reviewed.",
        ].join("\n"),
      );
    }
  }
}

async function warnOnDockerSocketText() {
  if (acknowledged("docker-socket-review-complete")) {
    return;
  }

  for (const file of editedFiles.filter((path) => !isIgnoredLargeFile(path))) {
    const added = await addedLinesForFile(file);
    const touchedSocket = added.some((line) => {
      return /DOCKER_HOST|docker\.sock|\/var\/run\/docker\.sock|socket-proxy|docker compose/i.test(
        line,
      );
    });
    if (touchedSocket) {
      warn(
        [
          `\`${file}\` adds Docker/socket-adjacent text. Confirm socket-proxy/raw-socket assumptions, read-only defaults, and Docker validation.`,
          "",
          "Add `Danger: docker-socket-review-complete` if reviewed.",
        ].join("\n"),
      );
    }
  }
}

async function runLargeFileRules() {
  if ((pr.additions ?? 0) <= 300 || acknowledged("large-file-review-complete")) {
    return;
  }

  for (const file of editedFiles.filter((path) => !isIgnoredLargeFile(path))) {
    const added = (await addedLinesForFile(file)).length;
    if (added > 300) {
      warn(
        [
          `\`${file}\` grew by ${added} added lines in this PR.`,
          "",
          "Review responsibility and navigation burden using `maintainability-policy.json`. Extract a cohesive responsibility or document a reviewed path-specific exception. Add `Danger: large-file-review-complete` to acknowledge this advisory; the marker cannot bypass maintainability enforcement. The checker remains draft-only until its post-refactor baseline is approved.",
        ].join("\n"),
      );
    }
  }
}

function warnWhenNoCompanionTests({ marker, title, changed, tests, detail }) {
  const files = changedMatching(changed, editedFiles);
  if (files.length === 0 || changedMatching(tests, editedFiles).length > 0) {
    return;
  }
  if (acknowledged(marker)) {
    return;
  }
  const expectedTests = tests.map((test) => `\`${test}\``).join(", ");

  warn(
    [
      title,
      "",
      `Changed: ${formatFiles(files)}`,
      `Expected companion tests: ${expectedTests}`,
      "",
      detail,
    ].join("\n"),
  );
}

function companionTestRule([marker, title, changed, tests, detail]) {
  return {
    marker,
    title,
    changed: pathPatterns(changed),
    tests: pathPatterns(tests),
    detail,
  };
}

function pathPatterns(patterns) {
  return patterns.trim().split(/\s+/);
}

async function addedLinesForFile(file) {
  const diff = await danger.git.structuredDiffForFile(file);
  const chunks = Array.isArray(diff?.chunks) ? diff.chunks : [];
  return chunks.flatMap((chunk) => {
    const changes = Array.isArray(chunk.changes) ? chunk.changes : [];
    return changes
      .filter((change) => change.type === "add" || change.type === "new")
      .map((change) => change.content ?? change.line ?? "")
      .filter((line) => typeof line === "string");
  });
}

function hasCheckedDockerValidation() {
  return /-\s+\[x\]\s+docker or compose validation/i.test(prBody);
}

function acknowledged(marker) {
  return prBodyLower.includes(`danger: ${marker}`.toLowerCase());
}

function changedMatching(patterns, files = changedFiles) {
  return files.filter((file) => matchesAny(file, patterns));
}

function matchesAny(file, patterns) {
  return patterns.some((pattern) => globToRegex(pattern).test(file));
}

function globToRegex(glob) {
  if (!globCache.has(glob)) {
    let pattern = "";
    for (let index = 0; index < glob.length; index += 1) {
      const char = glob[index];
      const next = glob[index + 1];
      if (char === "*" && next === "*" && glob[index + 2] === "/") {
        pattern += "(?:.*/)?";
        index += 2;
      } else if (char === "*" && next === "*") {
        pattern += ".*";
        index += 1;
      } else if (char === "*") {
        pattern += "[^/]*";
      } else {
        pattern += escapeRegex(char);
      }
    }
    globCache.set(glob, new RegExp(`^${pattern}$`));
  }
  return globCache.get(glob);
}

function escapeRegex(value) {
  return value.replace(/[|\\{}()[\]^$+?.]/g, String.raw`\$&`);
}

function isIgnoredLargeFile(file) {
  return matchesAny(file, LARGE_FILE_IGNORE_PATTERNS);
}

function formatFiles(files) {
  return files
    .slice(0, 8)
    .map((file) => `\`${file}\``)
    .join(", ")
    .concat(files.length > 8 ? `, and ${files.length - 8} more` : "");
}

function unique(values) {
  return [...new Set(values)];
}
