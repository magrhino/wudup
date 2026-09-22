# Public WebUI Demo

The public demo at [magrhino.github.io/wudup](https://magrhino.github.io/wudup/)
is a static, read-only preview of the real Vue WebUI. Its job is to show
prospective users the rough shape of WUDup: pending updates, review screens,
history, settings, Doctor checks, and the visual system.

It is not a second deployment target. It must not run FastAPI, SQLite, fake
Docker, WUD callbacks, Docker Compose, authentication bypasses, or browser
mutation paths. Static demo data lives in checked-in frontend fixtures and must
stay sanitized to `demo/...` paths.
The Containers sample shows Compose services and tracking filters, but does not
include live WUD container observations or allow tracking-repair previews.

## Maintenance Rule

Keep the static demo boring:

- Use the real SPA routes, stores, components, and theme.
- Keep sessions and status read-only with `mutations_enabled: false`.
- Keep fixture data small and hand-auditable.
- Do not generate all possible plan, removal, retag, or apply-job catalogs.
- Let plan preview show impact, but keep Apply disabled through the mutation
  gate.
- Reject mutation endpoints with the shared static-demo read-only error.

The local full-stack fake-Docker demo is separate. It exists for contributor
development and can exercise backend/updater code paths with disposable local
state. Do not make public Pages demo behavior depend on that harness.

## Validation

For static demo changes, run:

```bash
npm --prefix webui run build:demo
npm --prefix webui run test:smoke:demo
```

Use `npm --prefix webui run typecheck` and `npm --prefix webui run test` when
TypeScript API, store, or fixture behavior changes.
