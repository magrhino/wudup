# Development

This page covers local development, CI behavior, and release automation. Runtime
deployment docs start in [DEPLOYMENT.md](DEPLOYMENT.md).

## Local Setup

Use Python 3.14 for the locked CI/development toolchain. Install its dependencies
in a virtual environment before running the full local suite:

```bash
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt -r requirements-dev.txt -r requirements-build.txt
python -m pip install --only-binary=:all: --no-deps --no-build-isolation -e '.[dev]'
```

WUDup still supports Python 3.10 and newer. To work on another supported Python
version, create the venv with that interpreter and use
`python -m pip install -e '.[dev]'`; this deliberately resolves dependencies for
that interpreter rather than consuming the Python 3.14 CI lock. Regenerate the
committed locks with Python 3.14, since environment markers can change the
dependency set on older interpreters.

Run the full validation entrypoint:

```bash
tests/run-all.sh
```

The suite runs Ruff, shell syntax checks, ShellCheck, Python syntax checks,
Python unit tests (which generate an XML coverage report via `pytest-cov`),
updater behavior tests, and WebUI type, unit, and build checks. The full
entrypoint runs its Python, shell, and WebUI sections in parallel locally,
matching CI's split validation jobs.

## Focused Checks

```bash
ruff check .
shellcheck install.sh bin/updates bin/docker-update-from-wud wud/*.sh
bash -n install.sh bin/updates bin/docker-update-from-wud wud/http.sh wud/release-notes-to-discord.sh wud/github-release-embed.sh wud/tag-manager.sh
sh -n wud/on-update.sh wud/append-updates.sh
python3 -m compileall -q src tests webui/scripts
python -m pytest tests/test_python_*.py
tests/run-all.sh --python
tests/run-all.sh --shell
tests/run-all.sh --webui
tests/test-docker-update-from-wud.sh
tests/test-github-release-embed.sh
tests/test-wud-append-updates.sh
tests/test-updates-wrapper.sh
tests/test-entrypoint.sh
tests/test-release-notes-to-discord.sh
tests/test-tag-manager.sh
```

Container checks require Docker:

```bash
docker compose -f docs/examples/docker-compose.example.yml config
docker compose --env-file docs/examples/webui.env.example -f docs/examples/docker-compose.webui.yml config
docker compose -f docs/examples/docker-compose.hardened.yml config
docker compose -f docs/examples/docker-compose.truenas.yml config
docker compose -f docs/examples/docker-compose.build.yml config
tests/container-build.sh
```

The deployment compose example uses the published GHCR image. The build compose
artifact keeps the repository-local image build path used by smoke tests.

## Maintainability Checker (Draft)

`scripts/check_maintainability.py` compares physical lines in committed Git blobs,
including a final line without a newline. Enforcement uses
`maintainability-policy.json` in the requested head commit, while the committed
base policy determines each original file's classification. If the base predates
the policy, the checker uses the comparison policy and explicitly reports that
fallback in `base_policy_source`. It reads objects
with Git and the Python standard library; it never imports or executes project
code, reads working source files, follows symlinks, or fetches missing objects.
Blob counting drains one Git batch response at a time in 64 KiB chunks, including
binary files and very long lines. It retains counts and tree metadata, not the
contents of the compared blobs. Missing or truncated objects and failed Git
processes produce an error rather than a partial passing report.
Each policy input (committed base/head or external report-only file) is limited
to 1 MiB before JSON parsing. Git object sizes are checked before loading policy
contents, and external reads stop after the limit plus one byte. Oversized policies
exit with status 2 and must be reduced before the checker can run.

The policy is **baseline pending**. Enforcement exits with status 2 until the
predecessor work under [#694](https://github.com/magrhino/wudup/issues/694) is
integrated into main or explicitly deferred and the initial ceilings are reviewed.
Validated pull requests do not establish that baseline. This draft adds no CI job
or required status. The earlier local LOC audit script was not available to promote;
the checker follows [#706](https://github.com/magrhino/wudup/issues/706) and synthetic
Git repository tests.

For current or historical inventory using the draft policy:

```bash
python3 scripts/check_maintainability.py --repo . --base BASE_COMMIT --head HEAD_COMMIT --report-only --policy-file maintainability-policy.json
```

`--policy-file` is available only with `--report-only`. Such a report always says
`REPORT ONLY / NOT ENFORCED`; its successful command exit is not a passing gate.
An inventory with identical base/head commits includes unchanged files. JSON output
reports resolved commits and, for every tracked path, the category, base/head line
counts, delta, Git rename origin, applicable ceiling, warnings, and violations.
Deleted files remain visible. Tests, generated outputs, locks, other non-source
files, symlinks, and submodules are reported separately without size enforcement.

After integration, review the production inventory from post-refactor main, set
`baseline.status` to `ready`, and record its full commit ID. Every remaining
oversized production file needs an exact-path entry in `ceilings`. Exceptional
declarative collections or fixture generators belong in `allowances`, also with an
explicit ceiling; they remain visible and subject to growth checks. Each record
has `ceiling`, `reason`, `deferred` (boolean), `follow_up` (linked issue when deferred,
otherwise null or a link), and `re_review` (the condition for another review).
Policy changes, including higher ceilings or category changes, require review.
Never add broad source exclusions to solve a failure.
Exact production paths and reviewed records take precedence over directory
categories, so an explicitly retained production owner cannot hide inside tests
or generated output. Ordinary tests remain report-only.

Git-detected renames with a reviewed ceiling or allowance require a record at the
destination's exact path. Move or copy the source record in the same PR, including
ceilings below the default blocking threshold, so later comparisons retain it.
The old path's ceiling is still shown for diagnosis until the record is transferred,
but that rename fails. A production rename into tests or an excluded category fails:
retain production classification or review an exact-path declarative allowance.
Unchanged and shrinking oversized files with recorded ceilings are allowed; new
or growing files above their applicable ceiling fail. Follow the policy's
anti-gaming guidance: preserve validation, safeguards, coverage, comments, and type
precision, and reduce responsibility and navigation burden instead of compressing
formatting or scattering a cohesive owner among fragments.

### Contributing: resolve a possible category move

Git may report a relocation as deletion plus addition when edits obscure its
similarity, or when code moves into an existing file. If an enforced file
disappears without a detected rename and any non-enforced path is added or
modified, the checker reports **Possible category move** on the deleted source.
The JSON `category_move_candidates` list identifies the changed non-enforced
paths. This is a review tripwire, not proof that code moved. It includes shrinking
files and tests, generated outputs, locks, and other excluded categories.
Deleting production code while independently updating tests can trigger it too.
Pure deletions with unchanged excluded files and test additions alone still pass.

Review the deleted responsibility and candidate paths together, then add an
exact-source-path entry under `transitions` in `maintainability-policy.json`:

```json
"transitions": {
  "src/wudup/old_owner.py": {
    "base_commit": "<full commit ID passed to --base>",
    "destinations": ["src/wudup/new_owner.py"],
    "reason": "The responsibility moved intact to its new owner; related test changes exercise that owner."
  }
}
```

Replace the placeholder with the full ID from `git rev-parse BASE_COMMIT`.
List every destination that received the responsibility. Each destination's content
must be added or modified in the head and remain production or declarative code under
the head policy. An excluded destination must have an explicit production path,
`ceilings` entry, or reviewed declarative `allowances` entry. If the source had a
reviewed ceiling or allowance, transfer that record to each destination's exact
path; any ceiling increase needs its own justification and review. An existing
destination with a larger ceiling also requires a policy-record update explaining
why that larger limit is appropriate for the transferred responsibility. Normal size
checks still apply, and persistent destination records protect subsequent PRs.
A transition entry itself grants no size exemption or new category exclusion.

For a genuine deletion, use `"destinations": []` and explain why the responsibility
was removed and how the changed non-enforced files are unrelated to a relocation.
Reviewers must verify that explanation against the diff. Each ambiguous source
needs its own entry; a PR-body acknowledgement is insufficient. The declaration
applies only to its exact comparison base commit. Refresh and review it if that
base changes; obsolete entries can be removed in subsequent work. A declaration
cannot waive a category violation on a Git-detected rename.

Commit the resolution before running the checker; working-tree policy edits are
ignored. The checker validates the declaration's scope and destinations, but it
cannot authenticate human approval or prove semantic equivalence. Policy changes
still require review through the repository's normal PR process.

### Enforcement command and activation

The future local/CI enforcement command is identical, using the actual PR base
and head commit IDs (not a synthetic merge commit):

```bash
python3 scripts/check_maintainability.py --repo . --base BASE_COMMIT --head HEAD_COMMIT
```

It exits 0 for a passing enforced comparison, 1 for violations, or 2 for a pending
baseline, invalid policy, or unavailable Git objects. Before wiring CI or requiring
its status, validate representative PRs against the reviewed baseline and resolve
false positives. Future CI must use a trusted checker and review policy changes;
it must not execute candidate project code. Danger remains advisory, and
`Danger: large-file-review-complete` cannot bypass this checker.

## WebUI Development

Install the frontend dependencies before running the Vue/Vite checks:

```bash
npm --prefix webui ci --ignore-scripts
npm --prefix webui run typecheck
npm --prefix webui run test
npm --prefix webui run build
```

Build the public static WebUI demo for GitHub Pages with:

```bash
npm --prefix webui run build:demo
```

The Pages demo is fixture-backed, read-only, and runs entirely in the browser.
It uses the same Vue routes, stores, components, and theme as the real WebUI,
but it never starts FastAPI, SQLite, fake Docker, or live Docker/Compose
operations. Fixture paths are sanitized to `demo/...`, plan preview stays
non-applyable, and static mutation endpoints reject with the demo read-only
message. See [Public WebUI Demo](wiki/demo.md) for the maintenance boundary.

Browser smoke tests use Playwright Chromium with mocked local API fixtures, and
demo testing includes the `assert:demo-dist` pre-flight step to harden static
artifact tests. Install the browser once before running them locally:

```bash
npm --prefix webui run playwright:install
npm --prefix webui run test:smoke
npm --prefix webui run test:smoke:demo
```

For the local interactive full-stack demo server, seed disposable state and run
both the FastAPI backend and Vite frontend through the checked-in wrapper:

```bash
make webui-demo
```

`make webui-demo` starts the backend on `127.0.0.1:7417`, the frontend on
`127.0.0.1:5173`, sets `WUD_WEB_DEV_NO_AUTH=true`, enables
`WUD_WEB_MUTATIONS_ENABLED=true` for this demo process, and allows the Vite
origin for CSRF/Origin checks. The demo uses `local-dev/` for disposable Docker
fixtures, WUD output, logs, and `WUD_DB_PATH`.

This local demo is intentionally different from the public GitHub Pages demo and
is a contributor development harness, not a public demo contract. It exercises
the real backend and updater code paths against fake Docker state.
The wrapper puts the checked-in fake Docker command first on `PATH` and points
it at `local-dev/fake-docker`, so interactive actions exercise the real WebUI
backend and updater code paths without using the host Docker daemon. The
wrapper also serves a read-only sample WUD API on `127.0.0.1:7418` so the
Containers page can match the seeded Compose services to WUD observations.
Set `WUD_WEB_DEV_WUD_PORT` to use a different local port, or set
`WUD_API_BASE_URL` to use an external WUD API instead.

You can open `http://127.0.0.1:5173/#/pending`, select stack updates, preview the dry-run
plan, apply it, and then inspect the new run detail and log records. The seeded
`jarvis/task-runner` update exercises the update-stream choice between
`2.34.4-distroless` and `2.34.4`. The policies, snoozes, and tag exclusions
pages also seed editable SQLite demo
records, and the pending view includes a cached example security finding. Use
`make webui-demo-state` when you only need to refresh the disposable fixtures
without starting the servers.

Useful WebUI development variables:

| Variable | Purpose |
|---|---|
| `VITE_WUD_DEMO_MODE` | Build-time switch for the static GitHub Pages WebUI demo. |
| `VITE_WUD_PAGES_BASE` | Static demo asset base path; default demo mode value is `/wudup/`. |
| `WUD_DB_PATH` | SQLite database path for WebUI setup state, sessions, run history, audit records, and managed tag exclusions. |
| `WUD_TIMEZONE` | IANA timezone name used for WebUI auto-update policy schedules. Defaults to `UTC`. |
| `WUD_WEB_MUTATIONS_ENABLED` | Set to `true` only when testing browser-initiated plan/apply flows; default is read-only. |
| `WUD_WEB_DEV_BACKEND_PORT` | Backend port used by `webui/scripts/dev-server.mjs` and the Vite proxy; default `7417`. |
| `WUD_WEB_DEV_WUD_PORT` | Loopback port for the local read-only sample WUD API; default `7418`. |
| `WUD_WEB_DEV_FRONTEND_PORT` | Vite frontend port used by the dev-server wrapper; default `5173`. |
| `VITE_WUD_API_PREFIX` | Optional live API prefix or URL for custom reverse proxies; defaults to `<app base>/api/v1`. A preloaded `window.WUD_API_PREFIX` value overrides it at runtime. |
| `VITE_WUD_BACKEND_URL` | Backend URL exported by the dev-server wrapper for frontend experiments; the Vite proxy forwards the same-origin API prefix. |
| `WUD_WEB_HOST` / `WUD_WEB_PORT` | Host and port used when running `wudup web` manually. |
| `WUD_WEB_STATIC_DIR` | Optional built SPA directory override for manual backend testing. |
| `WUD_WEB_DEV_NO_AUTH` | Development-only auth bypass used by tests and the local demo wrapper. |
| `WUD_WEB_ALLOWED_ORIGINS` | Extra allowed origins for login, logout, setup, and mutation CSRF/Origin checks. |
| `WUD_WEB_PUBLIC_ORIGIN` | Browser-visible origin used for setup links, LAN or reverse-proxy exposure, allowed-host derivation, and secure-cookie auto-detection. |
| `WUD_WEB_ALLOWED_HOSTS` | Optional extra HTTP `Host` names accepted in addition to loopback, the configured public origin, and the bind host. |
| `WUD_WEB_TRUSTED_PROXIES` | Proxy IP/CIDR/hostname entries whose forwarded headers are trusted; hostnames resolve once at WebUI startup. |
| `WUD_WEB_SECURE_COOKIES` | Cookie Secure mode: `auto`, `true`, or `false`; keep `auto` outside local HTTP tests. |
| `WUD_API_BASE_URL` | Internal WUD API URL for best-effort WebUI metadata discovery; `make webui-demo` defaults to its loopback sample API, while manual runs default to `http://wud:3000`. Runtime discovery retries after transient outages. |
| `WUD_API_STARTUP_WAIT_SECONDS` | Seconds to retry the initial WUD API health probe during WebUI startup; defaults to `0`. This startup wait is separate from automatic runtime retries. |
| `WUD_SECURITY_SCANNING_ENABLED` | Enables the opt-in Trivy candidate advisory prototype for local testing, including installed-digest comparison when WUD metadata has `local_digest`. Refresh jobs require a Trivy executable in the backend process. |

For manual backend-only testing with a built SPA:

```bash
npm --prefix webui run build
wudup web --host 127.0.0.1 --port 7417 --static-dir webui/dist
```

## CI

CI and Docker installs use hashed Python locks and disable third-party install
scripts where supported. Run `make lock` with the installed development tools
to regenerate the runtime, build-backend, and development locks together.
See [SonarQube triage and gate policy](SONAR_TRIAGE.md) for the per-finding
decisions, required source-build exceptions, and merge enforcement.

CI runs on pull requests targeting `main` and pushes to `main`. The default path
is intentionally Linux-only to keep private repository Actions usage predictable.
The `python-tests` and `webui-checks` jobs generate coverage reports and upload
them to Codecov. Pull requests with `[skip ci]` in the title skip CI jobs except
the Docker build smoke test on Release Please PRs, and
direct `docs:` or `chore:` commits to `main` skip CI and Release Please jobs.
Merged Release Please PRs can still run the release automation needed to tag the
release.

Optional checks are available when broader coverage is useful:

- Add the `ci:macos` pull request label, or manually dispatch CI with
  `run_macos=true`, to run the macOS test job.
- Add the `ci:docker` pull request label, manually dispatch CI with
  `run_docker=true`, or change image-impacting files to run the Docker build
  smoke test. This includes `src/wudup/**`, `pyproject.toml`, `requirements.txt`,
  Dockerfile/entrypoint changes, and the container test scripts. Release Please
  PRs always run this check. CI and release validation both call
  `tests/container-build.sh`, which includes the WebUI startup smoke test.
- Manually dispatch CI with `run_webui_smoke=true`, or change files under
  `webui/`, to run the Playwright Chromium WebUI smoke tests.
- Add the `ci:e2e` pull request label, or manually dispatch CI with
  `run_e2e=true`, to run the Docker E2E test job (`docker-e2e`).
- Manually dispatch CI with `run_webui_demo=true` to run the WebUI Demo test job
  (`webui-demo`).
- Workflow linting runs automatically when files under `.github/workflows/`
  change, and can also be run from manual CI dispatch.

## Dependency Updates

Dependabot's existing weekly schedules, grouping, and cooldowns are unchanged.
Verified minor and patch updates are eligible for automatic squash merge;
major updates require manual review. Renovate continues to mark eligible
non-major Docker updates with its own `automerge` label.

The merge workflow waits for all repository-required checks and verifies the
latest `ci`, `security`, and `CodeQL Advanced` runs for the current PR head. All
three workflows must succeed, including any selected Docker or browser tests.
Core tests, dependency review, workflow auditing, and CodeQL analyses cannot be
skipped. GitHub must also report the PR as cleanly mergeable, even when the merge
token can bypass repository rules. Optional jobs can retain their normal skip
conditions. No extra human approval is needed for a routine clean update.

Dependency Review checks runtime, development, and unknown dependency scopes for
high/critical vulnerabilities and the routine licenses listed in
[`SECURITY.md`](../SECURITY.md#dependency-license-rules). Missing license metadata
also stops the check. The workflow summary and `dependency-review-<PR>-<SHA>` JSON
artifact (retained for 90 days) are the review evidence; failures need a
dependency fix or an evidence-backed maintainer
decision, not a broad allowlist or fabricated VEX record. Existing copyleft
dependencies, such as MPL-licensed build tooling, still require review when
updated; a previously merged version is not a blanket license exception.

Keep the repository's required checks and CodeQL high-or-higher merge protection
enabled, including the app-bound SonarCloud Code Analysis gate documented in
[SonarQube triage](SONAR_TRIAGE.md#gate-and-enforcement). Keep all three Python
locks in the existing root Dependabot update entry so shared pins update together;
CI installs them in one hashed, wheel-only transaction and rejects conflicts.
Dependency Review requires a public repository with dependency graph
enabled (or separately configured licensed private-repository support); the
current public-only workflow intentionally prevents unattended private-repository
merges by requiring its core job to pass, not skip.

The auto-merge workflow waits up to its 15-minute job limit for trailing required
checks. To retry after a timeout or a late external check, run:

```bash
gh workflow run dependency-automerge.yml --ref main -f pr_number=123
```

The retry uses the same gates and does not bypass them. Candidate records are
kept for seven days; if one has expired, rerun the candidate workflow for the
current PR or trigger a PR label event to regenerate it. Security exceptions
and VEX assessments remain maintainer work as described in `SECURITY.md`; this
automation neither creates VEX claims nor consumes them to suppress alerts.

## Releases

Release Please is the normal release path. When a Release Please PR is merged,
it creates a draft GitHub Release and tag, then dispatches the release
publisher with that tag.

For manual backfill, including an already-created draft release, or retry,
dispatch the release workflow with an existing stable tag:

```bash
gh workflow run release.yml --ref main -f release_tag=v1.2.3
```

The release publisher runs parallel blocking validation jobs, builds and
publishes Docker images for Linux amd64 and arm64 to `ghcr.io/magrhino/wudup`,
scans both the default and `-trivy` variants by immutable platform digest,
validates the multi-arch manifests, and then creates or publishes the GitHub
Release. All four scans must pass the [release image policy](../SECURITY.md#release-image-policy)
before any production image tag is promoted. HIGH/CRITICAL findings (including
unfixed vulnerabilities), end-of-life operating systems, and scan errors block
the release. The pinned scanner checks OS and language packages in the final
images; the former four-package upgrade check is no longer the release gate.
The public GitHub Release is published only after the GHCR image
tags are available. The release gate includes Python, shell, WebUI, WebUI smoke,
container build, and Docker Compose E2E validation. Image tags are published as
`vX.Y.Z`, `X.Y.Z`, `X.Y`, and `latest`. Direct pushes of stable `vX.Y.Z` tags
also run the same publisher as a fallback.
