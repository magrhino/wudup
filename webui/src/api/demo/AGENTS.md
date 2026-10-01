# AGENTS.md

## Scope

Rules for the static public WebUI demo API and fixtures under this directory.
Parent `webui/AGENTS.md` still owns frontend architecture and validation.

## Static Demo Contract

- The public demo is a read-only fixture showcase of the real Vue SPA, not a
  second WebUI deployment.
- Keep `session()`, `setupStatus()`, and `status()` read-only:
  `mutations_enabled: false`; no dev auth bypass.
- Do not add FastAPI, SQLite, fake Docker, WUD callbacks, Docker Compose, or
  real browser mutation behavior to static demo mode.
- Do not regenerate static fixtures from backend state or restore all-subset
  plan/retag catalogs. Keep fixtures small and hand-auditable.
- Mutation endpoints should reject with the shared static-demo read-only error
  or return an explicit blocked response. They must not change pending counts,
  settings, runs, logs, jobs, policies, snoozes, tag exclusions, or Compose
  state.
- Plan preview may be generated from the pending fixture, but it must remain
  non-applyable through the mutation-gate preflight check.

## Local Demo Boundary

`webui/scripts/seed_demo_state.py` and `webui/scripts/dev-server.mjs` belong to
the local full-stack contributor harness. Do not make public Pages demo behavior
depend on those scripts.

## Validation

- Static fixture/API change: `npm --prefix webui run typecheck`,
  `npm --prefix webui run test`, `npm --prefix webui run build:demo`, and
  `npm --prefix webui run test:smoke:demo`.
- Local full-stack demo seeding change: also run
  `python -m pytest tests/test_python_webui_demo_state.py -q`.
