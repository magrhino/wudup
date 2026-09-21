# AGENTS.md

## Scope

Rules for files under `webui/`. Root `AGENTS.md` controls repo-wide safety, releases, and files outside this directory; this file owns frontend architecture and scoped validation. `src/wudup/AGENTS.md` controls backend contract work.

## Context Budget

- Read `package.json`, the owning store/API/component, and focused tests for the task.
- Avoid broad frontend tree reads; use `rg` and nearby examples.
- For backend contract changes, read `src/wudup/AGENTS.md`, then inspect the matching `web_*` backend module and focused Python tests.

## Ownership Map

| Area | Owns | Read first | Avoid |
|---|---|---|---|
| `src/api/client.ts` | Typed backend API client and response mapping. | Matching backend route/model plus consuming store. | Duplicating fetch logic in components or stores. |
| `src/stores/connection.ts` | Status, doctor, restart, diagnostics. | API client and consuming view. | Mixing pending-update or settings state here. |
| `src/stores/updates.ts` | Pending updates, release notes, self-update, apply jobs. | API client, job/release-note views, focused tests. | Sharing mutation state through localStorage. |
| `src/stores/tracking.ts`, `src/views/TrackedContainersView.vue` | Compose service inventory, WUD tracking evidence, and plan-first tracking repair. | Typed tracking API and focused view/backend tests. | Treating unavailable WUD inventory as confirmed untracked or enabling demo repair mutations. |
| `src/stores/runs.ts` | Run history and logs. | API client and run/log views. | Duplicating run state in other stores. |
| `src/stores/settings.ts` | Settings, onboarding, policies, snoozes, tag exclusions. | API client and settings/onboarding views. | Cross-store writes without a clear owner. |
| `src/views/`, `src/components/` | Presentation and user interaction. | Owning store and nearby component tests. | Backend calls outside the typed client. |
| `scripts/` | Local/demo tooling. | `package.json`, `Makefile`, focused script tests. | Production assumptions or machine-specific paths. |

## Frontend Architecture Rules

- Keep state ownership explicit: one feature, one owning store.
- Do not recreate a monolithic WebUI store or duplicate state across stores.
- Keep components mostly presentational; route backend calls through the typed API client and owning store.
- Keep auth/session state out of localStorage; only transient apply-job recovery may use session storage.
- Keep public demo mode fixture-backed, sanitized, and mutation-free.
- Do not add mutation UX without matching backend CSRF/origin, read-only-mode, and audit behavior.
- Never add dev auth bypass, fake Docker, SQLite mutation, or real backend mutation paths to the static demo.

## Backend Contract Rules

- Backend route implementations live outside `webui/`; follow `src/wudup/AGENTS.md` before editing them.
- Preserve API paths, methods, response shapes, auth/session assumptions, and error surfaces unless the task explicitly changes the contract.
- When frontend API types change, update matching backend tests or explain why the change is frontend-only.

## Validation

Choose the smallest useful set:

- Dependency change: `npm --prefix webui ci`
- Type/API/store change: `npm --prefix webui run typecheck`
- Unit behavior change: `npm --prefix webui run test`
- Build-affecting change: `npm --prefix webui run build`
- Static demo change: `npm --prefix webui run build:demo` and `npm --prefix webui run test:smoke:demo`
- Browser auth/routing/smoke fixture change: install Chromium when needed, then `npm --prefix webui run test:smoke`
- Browser validation: prefer the Playwright plugin. Start the relevant dev/demo server, navigate directly to the route, use targeted waits/clicks/evaluations, check console messages, and cover desktop/mobile widths when layout changes.
- Keep Playwright output low-token: avoid full snapshots unless debugging, prefer `browser_evaluate` for compact state, write screenshots/snapshots to files, and remove generated `.playwright-mcp` or screenshot artifacts.
- Local/demo tooling change: `node --check webui/scripts/dev-server.mjs` plus focused script tests; use `make webui-demo-state` or `make webui-dev` when validating those flows.
- Backend API contract change: run the focused Python WebUI backend tests selected by `src/wudup/AGENTS.md`

## Edit Discipline

- Make the smallest correct change in the owning store/component/client.
- Add or update focused tests near the changed behavior.
- Do not normalize formatting outside touched lines.
- Keep generated assets, local logs, and machine-specific paths out of Git.
