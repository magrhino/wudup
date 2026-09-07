# AGENTS.md

## Scope

Rules for files under `src/wudup/`. Root `AGENTS.md` controls repo-wide safety, commits, releases, docs, and files outside this directory; this file owns backend module boundaries, backend safety deltas, and scoped validation.

## Context Budget

- Read the owning module, the nearest focused tests, and `tests/run-all.sh` before changing Python backend behavior.
- Treat `web.py` and `updater.py` as facades first: inspect them for app/CLI wiring and intentional public entrypoints, then move to the focused module that owns the behavior.
- Avoid broad reads of all backend modules unless the dependency boundary is unclear.

## Backend Ownership

Prefer small modules with one clear reason to change:

| Area | Preferred owner | Notes |
|---|---|---|
| Web app factory, startup, CLI handoff | `web.py` | Do not add new route families or large helper clusters here. Import owning `web_*` modules directly instead of adding facade exports. |
| Web models and schemas | `web_models.py` | Preserve Pydantic fields, defaults, `Field(...)` constraints, and `Literal` precision. |
| Web auth, setup, sessions, CSRF, Host/Origin safety | `web_auth.py` | Security-sensitive; preserve failure bodies, cookies, headers, and redaction. |
| Web health, readiness, and doctor routes | `web_health.py` | Preserve unauthenticated `/healthz`, local-only `/readyz`, authenticated readiness, doctor option/env construction, and redaction. |
| Web diagnostics support bundle and apply preflight | `web_diagnostics.py` | Preserve support-bundle redaction and apply preflight missing-check aggregation, status codes, failures, and warnings. |
| Web run history, verification, logs, and rollback guidance | `web_runs.py`, `web_run_verification.py`, `web_rollback.py` | Keep run reads authenticated and read-only; rollback guidance must fail closed and never pull, retag, rewrite Compose, restart services, or write audit state. |
| Web onboarding checklist and core update tour | `web_onboarding.py` | Preserve auth/CSRF behavior, dismissed-onboarding short-circuiting, SQLite setting keys, and read-only-mode tour persistence. |
| Web read-only database helpers | `web_database.py` | Preserve read-only SQLite URI handling, schema validation, and database readiness messages. |
| Web pending reads, cleanup, removal, and WUD rescans | `web_pending.py`, `web_pending_rescan*.py` | Keep WUD rescan non-file-mutating; preserve source hashes, stale selection checks, WUD locks, audit records, and WUD API degradation handling. |
| Web apply jobs, streams, plan apply | `web_jobs.py` | Preserve one-job-at-a-time, stale-plan rejection, WUD locks, audit, and progress events. |
| Web auto-update scheduler | `web_scheduler.py` | Keep disabled unless mutations are enabled; preserve reservations and timing behavior. |
| Web self-update and container restart | `web_self_update.py` | Preserve plan TTL, image/tag validation, restart validation, audit, and redaction. |
| Updater CLI facade and runner orchestration | `updater.py` | Preserve `UpdateFromWudRunner` and `run_update_from_wud`; helper APIs belong in their owning modules, not facade re-exports. |
| Updater dataclasses, typed records, exceptions | `updater_models.py` | Preserve dataclass options, defaults, and custom exception classes. |
| Compose YAML tag/digest/exclusion rewrites | `compose_rewrite.py` | Preserve fail-closed YAML handling, atomic writes, file mode/owner, and cleanup. |
| Registry HTTPS transport and authentication | `registry_http.py`, `digest_verifier.py` | Keep token origins authorized per registry, DNS addresses pinned, redirects disabled, and request size/time bounded; the live probe shares the resolver. Run `tests/test_python_registry_http.py` and digest verifier tests. |
| Docker tag-stream parsing and WebUI decision planning | `tag_streams.py` | Keep detection strict, manifest-verified, LinuxServer-aware, and free of GET-time registry calls. |

If the exact owner does not exist yet, create the narrowest reasonable module instead of growing `web.py`, `updater.py`, or a giant test file.

## Backend Safety

- WebUI mutations must remain disabled unless `WUD_WEB_MUTATIONS_ENABLED=true`.
- Mutating browser requests must keep auth, CSRF, Origin, Host, read-only-mode, stale-plan, single-job, and audit protections.
- No GET route may mutate host, Docker, Compose, or SQLite state beyond safe session/bootstrap behavior such as issuing a CSRF cookie.
- Preserve custom exception classes, exception chaining, HTTP status codes, response bodies, audit behavior, failure records, and secret/path redaction.
- When a WebUI helper validates filesystem containment with resolved paths, return and read from the same resolved `Path`; do not validate one path and later open the original candidate.
- When stored or environment-derived config parsing fails during server-side WebUI reads or writes, wrap `ConfigError` in a sanitized `HTTPException` detail via `_safe_exception_detail`; keep user-submitted validation errors as explicit 4xx responses.

## Tests

- Python syntax coverage uses `compileall` in `tests/run-all.sh`; no manifest update is needed for new Python files under the checked directories.
- Prefer focused WebUI backend tests such as `tests/test_python_web_auth_*.py`, `tests/test_python_web_pending_*.py`, `tests/test_python_web_jobs.py`, `tests/test_python_web_scheduler_*.py`, and `tests/test_python_web_self_update_*.py`.
- Until focused files exist, keep behavior covered in `tests/test_python_web.py` without adding unrelated cases.
- Prefer temp-dir tests, fake Docker/Compose, and targeted unittest/pytest coverage over broad fixtures or real Docker.

## Validation

Choose the smallest useful set:

- Python backend/config change: `ruff check .`, Python syntax check, and `tests/run-all.sh --python` when practical.
- Updater behavior change: `tests/test-docker-update-from-wud.sh` plus focused Python updater tests.
- WebUI backend auth, CSRF, mutation, plan/apply, scheduler, or self-update change: focused Python WebUI backend tests plus `tests/run-all.sh --python`.
- API contract change that affects frontend types or behavior: also run relevant WebUI typecheck/unit/build commands from `webui/AGENTS.md`.
- Docs-only or instruction-only change: `git diff --check`.
