# AGENTS.md

## Scope

Rules for files under `.github/` plus the root-level CI and release config files `renovate.json`, `release-please-config.json`, `.release-please-manifest.json`, `dangerfile.js`, `sonar-project.properties`, and release-prep edits to `CHANGELOG.md`. Root `AGENTS.md` controls repo-wide safety, key invariants, and all other files; this file owns CI, security scanning, dependency bots, Danger, Sonar, GitHub templates, and release automation.

## Path Map

| Path | Purpose | Read first | Edit notes | Avoid unless required |
|---|---|---|---|---|
| `.github/workflows/ci.yml` | Cost-conscious CI for PRs to `main`, pushes to `main`, optional macOS/Docker checks, Docker E2E, and workflow linting. | `tests/run-all.sh`, `tests/container-build.sh`, `tests/e2e-docker-compose.sh`, workflow file. | Keep default CI Linux-only; keep macOS gated by `ci:macos` or manual dispatch; keep Docker build gated by `ci:docker`, manual dispatch, or image-impacting path changes; keep Docker E2E separate and gated by `ci:e2e`, manual dispatch, or image-impacting path changes. Ensure new Compose examples are covered by container-build config validation. | Scheduled workflows, broad matrices, caches, artifacts, or always-on macOS/Docker jobs unless explicitly requested. |
| `.github/dependabot.yml`, `renovate.json` | Dependency update automation. Dependabot owns pip, npm, and GitHub Actions; Renovate owns Dockerfile image tags. | Existing updater configs and `Dockerfile` when image updates are involved. | Keep managers split so Renovate handles only Dockerfile image tags; preserve stable-only Docker update behavior. | Enabling overlapping Docker managers or broad Renovate managers without explicitly disabling duplicates. |
| `.github/workflows/dependency-automerge*.yml` | Verified dependency bot classification and automatic merging. | Both workflows, `.github/workflows/security.yml`, `SECURITY.md`, and `tests/test_dependency_automerge_workflows.py`. | Preserve bot provenance, current-head workflow gates, required checks, and atomic merges; privileged workflows must not execute PR code. Run the focused unittest included in `tests/run-all.sh --python`. | Auto-approving exceptions or bypassing failed, pending, missing, or skipped core checks. |
| `.github/workflows/webui-demo-pages.yml` | Static GitHub Pages deployment for the public fixture-backed WebUI demo. | `webui/package.json`, `webui/vite.config.ts`, `docs/DEVELOPMENT.md`, workflow file. | Build only static assets with demo mode; keep Pages permissions narrow; never deploy FastAPI, fake Docker, SQLite, dev auth bypass, or real mutation backends. | Server-side demo hosting, secrets, custom domains, or Pages environment assumptions unless requested. |
| `.github/workflows/security.yml`, `.github/CODEOWNERS`, `.github/zizmor.yml` | Security scanning suite, sensitive-path ownership, and GitHub Actions audit policy. | Existing workflow/release workflow rows plus the security files. | Keep PR-blocking jobs high-signal; skip GHAS-backed scans while the repo is private; keep Scorecard advisory; preserve readable Action version tags unless policy changes. | Repo-setting assumptions that cannot be enforced from files alone. |
| `dangerfile.js`, `.github/workflows/danger.yml` | Danger JS maintainability review prompts for PRs. | Recent review comments or recurring post-review fixes, then `dangerfile.js` and the workflow file. | Keep rules warning-first, repo-specific, and tied to changed files or PR body acknowledgements; avoid `pull_request_target`; keep generated/vendor/lockfile paths out of large-file heuristics. | Broad style linting, flaky semantic guesses, or rules that duplicate normal test execution. |
| `sonar-project.properties` | SonarCloud project analysis settings and quality-gate scope. | `.github/instructions/sonarqube_mcp.instructions.md`, SonarQube MCP gate/issues for the affected PR, and touched code/tests. | Keep project key and organization aligned with MCP project discovery; keep CPD exclusions limited to test harness paths unless a source exclusion is explicitly justified. | Broad source, security, or coverage exclusions that hide production issues. |
| `.github/pull_request_template.md`, `.github/ISSUE_TEMPLATE/*.yml` | GitHub PR and issue intake templates. | Existing template file, README, and docs terms relevant to the changed question. | Keep prompts concise, repo-specific, actionable, and free of secrets or machine-specific paths. | Workflow changes unless the task targets CI behavior. |
| `.github/workflows/release-please.yml`, `release-please-config.json`, `.release-please-manifest.json` | Release Please automation that opens release PRs, bumps Python version files and changelog entries, and creates `vX.Y.Z` GitHub releases/tags. | Release Please config and manifest, `.github/workflows/release.yml`, `pyproject.toml`, `src/wudup/__init__.py`, `CHANGELOG.md`. | Keep tag names compatible with `vX.Y.Z`; use the configured Release Please token secret so release-created tags trigger publishing workflows. | Manual manifest edits after bootstrap unless repairing release automation state. |
| `.github/workflows/release.yml`, `.github/workflows/release-validation.yml`, `.github/scripts/publish-release-image.sh` | Release validation, staged image scanning, GHCR promotion, and GitHub Release publishing. | `SECURITY.md` release image policy, `Dockerfile`, `tests/test-publish-release-image.sh`, workflow files. | Keep stable `vX.Y.Z` tags; scan both amd64/arm64 images in both variants by immutable digest before promoting any production tags. | Extra registries, prerelease tags, or package publishing outside GHCR unless requested. |
| `CHANGELOG.md` | Release-time record of notable versioned changes. | `CHANGELOG.md`, recent commits since the previous tag, and changed user-facing docs. | Author versioned `## [vX.Y.Z] — YYYY-MM-DD` sections only during explicit release prep. | Ordinary feature, docs, or maintenance work outside release prep. |

## Commands

| Purpose | Command |
|---|---|
| GitHub Actions lint | `actionlint` |
| GitHub Actions security scan | `zizmor --config .github/zizmor.yml --min-severity high --min-confidence medium .github/workflows` |
| Renovate config JSON check | `python3 -m json.tool renovate.json` |
| Release Please config JSON check | `python3 -m json.tool release-please-config.json` and `python3 -m json.tool .release-please-manifest.json` |
| Dangerfile syntax check | `node --check dangerfile.js` |

## Validation Selection

- GitHub Actions workflow change: run `actionlint` when available; if not installed, inspect the touched workflow YAML and report that local actionlint was not available. For release workflow changes, also inspect tag, permission, and GHCR image-tag behavior.
- Dependency updater config change: parse touched YAML/JSON, run `git diff --check`, and run a local Renovate config validator when one is already installed. Keep Dockerfile image updates in Renovate and non-Docker ecosystems in Dependabot unless explicitly changing ownership.
- Security workflow change: run `actionlint` when available, `git diff --check`, `tests/run-all.sh`, and the local `zizmor` command when installed; verify CodeQL, Dependency Review, and SARIF uploads in the first GitHub run after the repository is public or GHAS-backed scanning is enabled.
- Danger maintainability rule change: run `node --check dangerfile.js`, `git diff --check`, and `actionlint` when the workflow changes. Use a real PR run to validate GitHub API/comment behavior because Danger depends on PR metadata.
- Sonar config change: run `git diff --check`, inspect exclusions for production-source impact, and verify the next SonarQube MCP quality gate after remote analysis updates.
- GitHub template change: validate issue-template YAML when practical, run `git diff --check`, and skip application tests unless executable examples or commands changed.
- Release Please config change: validate `release-please-config.json` and `.release-please-manifest.json` as JSON, run `actionlint` when workflow files change, and verify tag naming stays compatible with `.github/workflows/release.yml`.

## Release Prep

- During release prep, draft `CHANGELOG.md` from commits since the previous tag and group entries by user-visible impact (`Added`, `Changed`, `Fixed`, `Docs`, `Removed`, or `Internal` as appropriate).

## Maintenance

- When adding a workflow, GitHub template, dependency-bot config, or root-level CI/release config file, add or update its row here rather than in root `AGENTS.md`.
