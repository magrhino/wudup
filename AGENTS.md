# AGENTS.md

## Scope

Repo-local routing/context only for WUDup. Global instructions control default behavior, commits, security, validation, and final response format unless this file gives a more specific repo-local rule.

## Context Budget

- Start with `Path map`; read only rows relevant to the task.
- Prefer `rg`, targeted file reads, manifest reads, and nearby examples.
- If editing a path with a nested `AGENTS.md`, read that file before editing.
- Avoid broad tree reads.

## Path Map

| Path | Purpose | Read first | Edit notes | Avoid unless required |
|---|---|---|---|---|
| `bin/updates` | Host CLI wrapper that displays WUD Docker updates, TrueNAS update status, alerts, and optionally calls the updater. | `README.md`, `bin/docker-update-from-wud` usage block. | Preserve prompt, `--dry-run`, `--yes`, config-file, `sudo`, and updater handoff behavior. | TrueNAS `midclt` handling unless the task targets system update or alert checks. |
| `bin/docker-update-from-wud` | Symlink-safe dispatcher for the default Python updater. | `README.md`, `src/wudup/cli.py`, `src/wudup/updater.py`, and dispatcher tests. | Keep argument pass-through exact; preserve `PYTHON_BIN` and `PYTHONPATH` behavior. | Adding updater logic here instead of in Python. |
| `pyproject.toml`, `src/wudup/` | Python updater, WebUI backend, config, DB, planning, Docker/Compose helpers, and CLI entrypoints. | `src/wudup/AGENTS.md`, then the owning module and focused Python tests. | Keep `wudup updates` opt-in until promoted separately; follow scoped module/test ownership. | Moving WUD callback scripts out of shell. |
| `webui/` | Vue 3/Vite/TypeScript SPA for the read-only WebUI plus static GitHub Pages demo mode. | `webui/AGENTS.md`, then the owning store/API/component and focused tests. | Follow scoped frontend, store, and demo guidance. | Public demo mutation backends or machine-specific dev assumptions. |
| `Makefile` | Developer convenience targets for WebUI demo state and local dev. | `Makefile`, `webui/package.json`, `webui/scripts/*`, and relevant tests. | Keep targets thin wrappers around checked-in scripts; do not hard-code machine-specific paths. | Production install or release behavior unless the task explicitly targets it. |
| `scripts/lock.sh`, `requirements*.txt`, `.github/actions/setup-python-env/action.yml` | Hashed runtime, build-backend, and CI dependency locks. | `pyproject.toml`, `scripts/lock.sh`, `docs/SONAR_TRIAGE.md`. | Regenerate together with `make lock`; verify wheel-only installs and local source builds without dependency resolution. | Hand-edited hashes or broad binary-only flags on the optional TrueNAS source build. |
| `wud/on-update.sh`, `wud/append-updates.sh` | WUD notification callback and line-oriented update-list writer. | Both files plus WUD env variable usage. | Keep POSIX `sh` compatibility and container defaults for `/wud` and `/out`. | Host-specific paths, secrets, or behavior that belongs in `bin/`. |
| `wud/release-notes-to-discord.sh`, `wud/github-release-embed.sh`, `wud/tag-manager.sh`, `wud/http.sh`, `wud/upstreams.txt` | Canonical shell Discord/GitHub release-note router, compatibility wrappers, shared HTTP behavior, and LinuxServer.io upstream mapping. | `wud/release-notes-to-discord.sh`, wrapper entrypoint when compatibility is involved, `wud/http.sh`, `wud/upstreams.txt`, and `src/wudup/release_notes.py` for WebUI metadata. | Keep WUD callbacks shell-based; keep legacy wrapper arguments/env accepted; keep webhook/token values environment-driven and redacted in logs. Preserve standardized `curl`/`jq`-based GitHub and Discord behavior. | Network calls unless validating release-note behavior. |
| `install.sh` | Idempotent installer that chmods scripts and creates host symlinks for CLI commands and WUD scripts. | `install.sh`, then README install section. | Preserve refusal to replace non-symlink targets and existing env overrides. | Changing default target layout unless the task asks for installer behavior changes. |
| `Dockerfile`, `entrypoint.sh`, `docs/examples/docker-compose.example.yml`, `docs/examples/docker-compose.webui.yml`, `docs/examples/docker-compose.hardened.yml`, `docs/examples/docker-compose.truenas.yml`, `docs/examples/docker-compose.build.yml`, `.dockerignore` | Container packaging for running the updater helpers with Docker CLI access, the long-running WebUI container, and optional TrueNAS API reachability. | `README.md`, `docs/DEPLOYMENT.md`, `entrypoint.sh`, `bin/updates`, `bin/docker-update-from-wud`, `src/wudup/web.py` for WebUI examples. | Keep the default command non-mutating, keep WebUI examples read-only unless mutation work is explicit, preserve command dispatch, keep Docker socket or socket-proxy access and host stack mounts explicit, and keep TrueNAS API keys secret-file based in examples. | Replacing WUD's separate `/wud` script mount, enabling WebUI mutations by default, or baking version-specific TrueNAS clients into the default image. |
| `tests/` | Local test runner, focused shell tests, Python tests, fake command implementations, WebUI backend tests, and Docker E2E harnesses. | `tests/run-all.sh`, then the focused test for the behavior being changed. | Keep tests temp-dir based; fake Docker for default tests; reserve real Docker mutations for explicit Docker-gated harnesses; keep Python dev dependencies explicit in `pyproject.toml`. | Adding dependencies or broad fixtures when a small shell fake, unittest, or Docker-gated E2E fixture is enough. |
| `scripts/check_maintainability.py`, `maintainability-policy.json` | Draft committed-blob size ratchet and its sole threshold/category/exception policy. | `docs/DEVELOPMENT.md` maintainability section and focused synthetic Git tests. | Keep baseline pending until predecessor integration; use explicit report-only inventory meanwhile. | Initial ceilings from unmerged branches, automatic exceptions, or CI activation before baseline review. |
| `SECURITY.md`, `README.md`, `docs/` | User-facing security policy, overview, deployment reference, examples, and feature explainers. | For security policy changes, `docs/DEPLOYMENT.md`, `.github/ISSUE_TEMPLATE/config.yml`, and security workflow docs; otherwise scripts being described, plus `docs/README.md` for docs routing. | Keep concise, accurate, and free of secrets or machine-specific paths. Keep root README as the short entrypoint, `SECURITY.md` as the private reporting policy, and detailed references under `docs/`. | Operational assumptions not present in code. |
| `template.env` | Example host and optional WUD environment configuration. | `template.env`, then the script consuming the changed variable. | Keep values example-only, environment-driven, and free of real secrets or machine-specific paths. | Adding new knobs not supported by scripts or README examples. |
| `.gitignore` | Ignore rules for local logs, temp files, WUD output, and desktop metadata. | `.gitignore` only. | Keep generated/runtime data out of Git. | Broad ignore patterns that could hide source files. |

## Key Invariants

- WUD output is a line-oriented file of image/container targets; blank and comment lines are ignored, and optional `sha256=` digest suffixes must stay compatible with the Python updater path.
- Mutating Docker operations require explicit confirmation or `--yes`; `--dry-run` must not pull, restart, clean the WUD file, or otherwise mutate host state.
- Secrets such as Discord webhooks and GitHub tokens must come from the environment or host-local config and must not be logged in full.
- Write error messages and error logs in plain language that a WUDup operator can understand without knowing this repository's internals. State what failed, why when known, and what the operator can do next; put implementation details after that human-readable summary.
- Container-facing scripts assume `/wud` for mounted scripts and `/out` for WUD output; host paths belong in install/config, not hard-coded into container scripts.

## Scoped AGENTS

- `src/wudup/AGENTS.md` owns Python backend module boundaries, WebUI backend safety, updater compatibility, and focused Python test guidance.
- `webui/AGENTS.md` owns frontend state, typed API client, components/views, static demo, and frontend validation guidance.
- `.github/AGENTS.md` owns CI workflows, security scanning, dependency bots, Danger, Sonar, GitHub templates, and release automation; read it before editing anything under `.github/` or the root files `renovate.json`, `release-please-config.json`, `.release-please-manifest.json`, `dangerfile.js`, `sonar-project.properties`, or `CHANGELOG.md`.
- Prefer the closest scoped file over expanding root; nested guidance should replace duplicated root detail.

## Issue Creation

- When creating GitHub issues, start from the matching template under `.github/ISSUE_TEMPLATE/`.
- Categorize issues with the template's existing labels/tags such as `bug`, `enhancement`, and `needs-triage`; only add other existing repo labels when they clearly apply.

## Repo Commands

Use the shell already used by the target script.

| Purpose | Command |
|---|---|
| install | `./install.sh` |
| shell lint | `shellcheck install.sh bin/updates bin/docker-update-from-wud wud/*.sh` |
| Bash syntax check | `bash -n install.sh bin/updates bin/docker-update-from-wud wud/http.sh wud/release-notes-to-discord.sh wud/github-release-embed.sh wud/tag-manager.sh` |
| POSIX syntax check | `sh -n wud/on-update.sh wud/append-updates.sh` |
| updater dry run | `bin/docker-update-from-wud --base "$DOCKER_BASE" --file "$WUD_OUT_FILE" --dry-run` |
| host status dry run | `bin/updates --dry-run` |
| full local test suite | `tests/run-all.sh` |
| updater behavior tests | `tests/test-docker-update-from-wud.sh` |
| WUD append tests | `tests/test-wud-append-updates.sh` |
| release-note payload tests | `tests/test-release-notes-to-discord.sh` |
| installer tests | `tests/test-install.sh` |
| host wrapper tests | `tests/test-updates-wrapper.sh` |
| container entrypoint tests | `tests/test-entrypoint.sh` |
| container build test | `tests/container-build.sh` |
| Docker Compose E2E test | `tests/e2e-docker-compose.sh` |
| deployment compose config check | `docker compose -f docs/examples/docker-compose.example.yml config` |
| long-running WebUI compose config check | `docker compose -f docs/examples/docker-compose.webui.yml config` |
| hardened deployment compose config check | `docker compose -f docs/examples/docker-compose.hardened.yml config` |
| TrueNAS API deployment compose config check | `docker compose -f docs/examples/docker-compose.truenas.yml config` |
| local build compose config check | `docker compose -f docs/examples/docker-compose.build.yml config` |
| container image build | `docker build -t wudup:local .` |
| Python dev dependency install | Check for `.venv/bin/python` first and activate it when present; otherwise run `python3 -m venv .venv`, `. .venv/bin/activate`, then `python -m pip install -e '.[dev]'` |
| Python backend validation | See `src/wudup/AGENTS.md`. |
| update pip lockfile | `make lock` |
| Live digest verification probe | `tests/live-digest-verification.py alpine:3.20 quay.io/prometheus/busybox:latest` |
| WebUI validation and local dev | See `webui/AGENTS.md`. |
| maintainability checker tests | `python -m pytest tests/test_python_maintainability*.py` and `python -m py_compile scripts/check_maintainability.py` |
| format check | Not configured. |

## Validation Selection

- Shell script change: run syntax checks for the touched shell dialect first, then ShellCheck and the focused test for the touched behavior.
- Updater behavior change: run `tests/test-docker-update-from-wud.sh`; prefer fake Docker tests and `--dry-run` validation with disposable or known-safe WUD input before any mutating run.
- WUD append behavior change: run `tests/test-wud-append-updates.sh`; use a temporary `WUD_OUT_FILE` and representative WUD env vars.
- Installer change: run `tests/test-install.sh`; tests should use temp env overrides for `BIN_DIR`, `DOCKER_BASE`, `WUD_SCRIPTS_LINK`, and `WUD_OUT_DIR`.
- Host wrapper change: run `tests/test-updates-wrapper.sh`; fake `sudo` and configured updater commands rather than invoking real system mutation.
- Container packaging change: run `bash -n entrypoint.sh`, ShellCheck through `tests/run-all.sh`, `tests/test-entrypoint.sh`, and `tests/container-build.sh` when Docker is available. Run `tests/e2e-docker-compose.sh` when Docker socket, updater handoff, WUD script sync, or real Compose update behavior changes. The container build test validates Compose config, including the TrueNAS API example, builds the image, and smoke-runs the default non-mutating command; the Docker E2E test uses a local registry and real Compose stack to verify update and callback wiring.
- Release-note behavior change: syntax-check the touched scripts, run ShellCheck, run `tests/test-release-notes-to-discord.sh`, and avoid live Discord/GitHub calls unless explicitly requested or needed. For wrapper compatibility changes, cover `wud/github-release-embed.sh` and `wud/tag-manager.sh` legacy invocation paths. For WebUI release-note metadata changes, also follow the scoped Python/WebUI validation files.
- Python updater/config change: use `src/wudup/AGENTS.md`.
- Rich terminal rendering change: create or update focused tests that exercise the Rich-enabled path for the touched surface, using mocks when local Rich is unavailable; run `python3 -m unittest tests.test_python_terminal` plus Python syntax checks before broader suites.
- WebUI frontend change: use `webui/AGENTS.md`; also use `src/wudup/AGENTS.md` when API contracts or auth assumptions change.
- Local WebUI browser check: use the Playwright plugin and the low-token flow in `webui/AGENTS.md`; if the plugin is unavailable, report the fallback used.
- Cross-cutting behavior change: run `tests/run-all.sh` when practical before finishing.
- Docs-only change: no tests required unless examples or commands were changed enough to need syntax validation.
- Unknown command: inspect scripts/docs, then prefer extending `tests/run-all.sh` or a focused `tests/test-*.sh` instead of inventing a separate harness.

## Maintenance Notes

- When adding a top-level file, script, test harness, or user-facing config surface, update the root path map and repo-wide commands/validation only when they add useful routing not already owned by a scoped `AGENTS.md`; workflows and CI/release config belong in `.github/AGENTS.md`.
- Python syntax coverage uses `compileall` in `tests/run-all.sh`; no manifest update is needed when adding, removing, or renaming Python files under the checked directories.
- When creating a new backend module or frontend surface, update the closest scoped `AGENTS.md` if ownership or validation guidance changes.

## Generated/Low-Value Paths

Do not read or edit unless directly required.

- `.DS_Store`
- `*.log`
- `*.tmp`
- `out/`
- WUD runtime output such as `images.todo`

## Nested AGENTS Suggestions

Intentional scoped guides: `src/wudup/AGENTS.md`, `webui/AGENTS.md`, and `.github/AGENTS.md`.
Do not add `tests/AGENTS.md` until focused backend test files make test ownership stable enough to replace root guidance.

## Edit Discipline

- Identify the owning path from `Path map` before editing.
- Read nearest implementation and test examples first.
- Make the smallest correct change.
- Prefer scoped guidance over root expansion; update nested `AGENTS.md` files when local rules change.
- Do not normalize formatting outside touched lines.
- Do not move code across directories unless requested.
- If multiple areas are touched, re-check whether nested `AGENTS.md` files apply.
