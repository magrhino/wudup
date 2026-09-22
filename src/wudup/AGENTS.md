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
| Web listeners and startup summary | `web_startup.py` | Keep the container wildcard dual-stack with IPv4 fallback when IPv6 is unavailable; preserve explicit binds, native client addresses, and loopback readiness. Validate with `tests/test_python_web_startup.py` and `tests/test_python_web_health.py`. |
| Web models and schemas | `web_models.py` | Preserve Pydantic fields, defaults, `Field(...)` constraints, and `Literal` precision. |
| Web auth, setup, sessions, CSRF, Host/Origin safety | `web_auth.py` | Security-sensitive; preserves credentials, sessions, cookies and CSRF/Host/Origin acceptance policy. Actor classification stays here because it validates bearer credentials; shared formatting/context/transactions have explicit owners below. |
| Web health, readiness, and doctor routes | `web_health.py` | Preserve unauthenticated `/healthz`, local-only `/readyz`, authenticated readiness, doctor option/env construction, and redaction. |
| Web diagnostics support bundle and apply preflight | `web_diagnostics.py` | Preserve support-bundle redaction and apply preflight missing-check aggregation, status codes, failures, and warnings. |
| Web run history, verification, logs, and rollback guidance | `web_runs.py`, `web_run_verification.py`, `web_rollback.py` | Keep run reads authenticated and read-only; rollback guidance must fail closed and never pull, retag, rewrite Compose, restart services, or write audit state. |
| Web onboarding checklist and core update tour | `web_onboarding.py` | Preserve auth/CSRF behavior, dismissed-onboarding short-circuiting, SQLite setting keys, and read-only-mode tour persistence. |
| Web database reads, settings and transactions | `web_database.py` | Preserve read-only SQLite URI/schema handling, shared settings SQL, and immediate begin/commit/rollback order. |
| Web operator errors and support-bundle sanitization | `web_redaction.py` | Preserve secret/path replacement order, recursive values, exact safe messages, and no input mutation. Import shared APIs here; auth aliases support staged callers only. |
| Web request settings and proxy context | `web_request_context.py` | Reads configured settings and interprets existing client/origin context. Auth retains credential/actor policy and request acceptance; do not duplicate trust or authentication decisions. |
| WUD client configuration and HTTP transport | `web_wud_transport.py` | Own secret-file loading, header validation, URL normalization, and requests; `web_wud_api.py` retains public configuration exports; cache/watch orchestration belongs to `web_wud_cache.py`. Preserve timeouts, authentication, and redaction. |
| WUD snapshots, refresh, and watcher state | `web_wud_cache.py` | Single owner of cache dictionaries and locks, startup/checkpoint lifecycle, cooldowns, and watch batches. Preserve lock scopes/order, cache/retry timing, and persistence policy; public entrypoints remain in `web_wud_api.py`. |
| WUD container observations | `web_wud_observations.py` | Own payload parsing, target matching, degraded recovery/reconciliation, diagnostics, and persisted row conversion. No mutable cache state; `web_wud_cache.py` serializes refresh and publishes results. |
| Web pending reads, cleanup, removal, and WUD rescans | `web_pending.py`, `web_pending_rescan*.py` | Keep WUD rescan non-file-mutating; preserve source hashes, stale selection checks, WUD locks, audit records, and WUD API degradation handling. |
| Tracked Compose inventory and tracking-label repair | `web_tracking.py` | Include every discovered Compose service; treat missing WUD metadata as unknown. Keep repair plan-first, label-only, stale-source checked, single-service, and audited. |
| Web apply jobs, streams, plan apply | `web_jobs.py` | Preserve one-job-at-a-time, stale-plan rejection, WUD locks, audit, and progress events. |
| Retag preview job lifecycle | `web_retag_preview.py` | Own the single preview executor, app-state lock/job dictionary, bounded retention, progress, and terminal responses. Routes in `web_retags.py` supply the current plan builder; preserve per-job result correlation, redaction, and shutdown without moving planning or apply. |
| Retag apply execution and recovery | `web_retag_apply.py`, `web_retag_audit.py`, `web_retag_runtime.py` | The apply owner coordinates the existing single-job/WUD-lock lifecycle, immediate runtime revalidation, Compose rewrite/pull/recreate/health/recovery, and progress. Audit owns run/events and known-image persistence; runtime owns shared Compose identities. Routes supply current plan/config callbacks; preserve stopped-service consent, operation ordering, source-hash restoration, partial-success provenance, and redaction. |
| Web auto-update scheduler | `web_scheduler.py` | Keep disabled unless mutations are enabled; preserve reservations and timing behavior. |
| Discord notification rendering and delivery | `web_discord.py` | Owns immutable rendering input, copy, escaping, payload limits/batching, webhook resolution and HTTP delivery. Scheduler, source selection, reservations, history, audit and API responses stay in `web_release_notifications.py`; test delivery through the actual owner with mocked HTTP. |
| Web self-update and container restart | `web_self_update.py` | Preserve plan TTL, image/tag validation, restart validation, audit, and redaction. |
| Updater CLI facade and runner orchestration | `updater.py` | Preserve `UpdateFromWudRunner` and `run_update_from_wud`; helper APIs belong in their owning modules, not facade re-exports. |
| Updater dataclasses, typed records, exceptions | `updater_models.py` | Preserve dataclass options, defaults, and custom exception classes. |
| Compose tag/digest/exclusion rewrite operations | `compose_rewrite.py` | Stable entrypoints own update/approval matching, regex policy, and resolved-tag marker semantics. Keep YAML mechanics and persistence in their owners. |
| Compose round-trip YAML source editing | `compose_source.py` | Own parsing, source spans, label styles, anchor/alias guards, and comment-token containers; preserve exact source handling and fail-closed errors without choosing update policy or writing files. |
| Compose atomic persistence and backups | `compose_persistence.py` | All writers, backups, and guarded restores share the directory lock. Preserve source-hash checks, file mode/owner, replacement ordering, and temporary-file cleanup. |
| Registry HTTPS transport and authentication | `registry_http.py`, `digest_verifier.py` | Keep token origins authorized per registry, DNS addresses pinned, redirects disabled, and request size/time bounded; the live probe shares the resolver. Run `tests/test_python_registry_http.py` and digest verifier tests. |
| Docker tag-stream parsing and WebUI decision planning | `tag_streams.py` | Keep detection strict, manifest-verified, LinuxServer-aware, and free of GET-time registry calls. |
| Release-note API and context orchestration | `release_notes.py`, `release_note_models.py` | Preserve public imports, shared record defaults, context cache keys, candidate selection, and backfill fallback. Provider, security, and cache owners must not import the facade. |
| GitHub/LSIO release-note providers | `release_note_providers.py` | Own the shared GitHub client, source discovery, bounded release lookup, breaking detection, and LSIO classification; preserve request/fallback order and upstream mappings. |
| Release-note security assessment | `release_note_security.py` | Own advisory matching, version-range evidence, severity, scan/fetch limits, and retryable reasons; use the same provider client without adding cache writes. |
| Release-note cache persistence | `release_note_cache.py` | Own legacy row decoding, digest pruning, upserts, and TTL decisions; preserve SQL/serialization and keep cached reads network-free. |

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
