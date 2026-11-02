# Integrations API

WUDup serves a small, read-only JSON API for dashboards, chat bots, and
scripts under `/api/integrations/v1`. It summarizes pending updates, release
and security signals, the last update run, and issues worth alerting on, in
responses capped to a few kilobytes.

The API only reads. Its token cannot apply updates, change settings, or call
any other WUDup route, even when `WUD_WEB_MUTATIONS_ENABLED=true`.

## Token

Every request needs a read-only API token in the `Authorization` header:

```bash
curl -H "Authorization: Bearer $WUDUP_READONLY_TOKEN" \
  http://wudup:7417/api/integrations/v1/summary
```

Set the token one of two ways. Container config wins when both exist.

- **Container config:** set `WUD_WEB_READONLY_TOKEN_FILE` to a mounted file that
  holds only the token, or `WUD_WEB_READONLY_TOKEN` for local development. Use
  at least 32 random characters (for example `openssl rand -hex 32`) and a
  different value from `WUD_WEB_TOKEN`. Rotate by changing the value and
  restarting WUDup.
- **WebUI:** open **Settings > Integrations** and choose **Generate token**.
  The token is shown once; WUDup stores only its SHA-256 hash, so copy it into
  the integration's secret store right away. Replacing or revoking it takes
  effect on the next request. This needs `WUD_WEB_MUTATIONS_ENABLED=true`; with
  WebUI changes off, revoke a generated token with
  `wudup web readonly-token revoke`.
- If you set the container-config token after generating one in the WebUI, the
  generated token stops working but stays stored, and it would work again if
  the container setting were removed. Settings shows it as **Stored WebUI
  token**; revoke it there or with the CLI command above.

Other things to know:

- The token is only read from the `Authorization` header, never from a query
  string, so it stays out of access logs.
- Five wrong tokens from one client address lock that address out of this API
  for a minute. Behind a reverse proxy, list the proxy in
  `WUD_WEB_TRUSTED_PROXIES`.
- `WUD_WEB_TOKEN` and browser sessions are not accepted here, and the
  read-only token is not accepted anywhere else.
- Requests must use a host listed in `WUD_WEB_ALLOWED_HOSTS`. On a shared Docker
  network, add the container name (for example `wudup`).

## Endpoints

### `GET /api/integrations/v1/summary`

```json
{
  "v": 1,
  "checked_at": "2026-11-02T21:00:00+00:00",
  "wud": {"state": "ready", "fresh": true, "detail": ""},
  "updates": {
    "total": 7,
    "items": [
      {"service": "media/jellyfin", "current": "10.10.3", "target": "10.10.4",
       "kind": "patch", "breaking": false, "security": "critical_high",
       "fixes": {"critical": 1, "high": 3}, "scan": "complete",
       "auto": true, "skipped": false}
    ],
    "more": 0,
    "unplaced": 0,
    "snoozed": 1
  },
  "last_run": {"id": 812, "at": "2026-11-02T03:05:00+00:00", "mode": "live",
               "status": "failure", "services": 3, "failed": ["media/jellyfin"],
               "reason": "health check timed out"},
  "issues": [
    {"id": "wudup:run-failed:812", "severity": "urgent", "code": "run-failed",
     "name": "Update run failed",
     "summary": "media/jellyfin: health check timed out", "since": "2026-11-02T03:05:00+00:00"}
  ],
  "issues_more": 0
}
```

- `wud.state` is `ready`, `unavailable`, `auth_required`, or `error`. `detail`
  is set only when the state is not `ready`.
- `updates.items` lists at most 20 updates: security fixes first, then breaking
  updates, then by name. `more` counts the rest.
  - `service` is `stack/service`. It is the image repository instead when WUDup
    could not match the update to a Compose service; `unplaced` counts those.
  - `security` comes from release notes: `critical_high` (a verified
    critical or high advisory), `review`, `none`, or `unknown`.
  - `fixes` counts critical and high findings the target image fixes compared
    with the running image. It is `null` when that comparison is unknown.
  - `scan` is the cached security scan state, such as `complete`,
    `not_scanned`, or `disabled`.
  - `auto` means the service auto-updates; `skipped` means the release is
    skipped.
  - `snoozed` counts updates hidden by a snooze.
- `last_run` is the newest real (non-dry-run) update run, or `null`. `status` is
  `success`, `failure`, `needs_review`, `running`, `missed`, or `unknown`.
- `issues` lists at most 10 issues, urgent first; `issues_more` counts the rest.

### `GET /api/integrations/v1/services/{stack}/{service}`

Details for one Compose service, for example
`/api/integrations/v1/services/media/jellyfin`. It returns 404 when WUDup does
not track that service.

```json
{
  "v": 1, "service": "media/jellyfin", "image": "jellyfin/jellyfin",
  "current": "10.10.3", "target": "10.10.4", "kind": "patch",
  "runtime": "running", "update_available": true,
  "release": {"tag": "v10.10.4", "title": "10.10.4", "published_at": "…",
              "breaking": false, "breaking_reasons": [],
              "security": "critical_high", "advisories": ["GHSA-…"],
              "link": "https://github.com/…"},
  "scan": {"state": "complete", "scanned_at": "…", "comparison": "improved",
           "target": {"critical": 0, "high": 2}, "fixes": {"critical": 1, "high": 3}},
  "policy": {"mode": "live", "auto": true, "time": "03:00", "days": ["sat"]},
  "snooze": null,
  "last_run": {"id": 812, "at": "…", "mode": "live", "status": "failure",
               "services": 3, "failed": ["media/jellyfin"], "reason": "…"}
}
```

A tracked service without a pending update has `update_available: false` and
no `target`, `release`, or `scan`.

## Issues

Issue ids are stable while the condition lasts, so an alerting tool can
confirm, remind, and resolve by id. `severity` is `urgent` or `digest`.

| `code` | Severity | When |
|---|---|---|
| `wud-unreachable` | urgent | WUDup cannot read update checks from WUD. |
| `run-failed` | urgent for 24 hours, then digest | The latest real update run failed or left a service unhealthy. Only update runs count (`pause`, `stop`, `live`, `web-retag`, `web-self-update`); dry runs and other history rows such as logins or settings changes neither raise nor clear it. Clears when a newer update run succeeds. |
| `security-release` | urgent | A pending update's release notes name a verified critical or high advisory. Not raised for a skipped release. |
| `breaking-update` | digest | A pending update's release notes flag breaking changes. Not raised for a skipped release. |
| `auto-update-missed` | digest | A scheduled auto-update was skipped in the last 24 hours. |
| `update-unplaced` | digest | WUD reported updates WUDup could not match to a Compose service. |
| `scan-broken` | digest | Security scanning is on, but scans are failing. |
| `wud-stale` | digest | WUD could not recheck some containers, so their data may be old. |

Skipped releases (`skipped: true`) raise no issues, including security ones:
skipping a release is an explicit operator decision. Raw scanner counts are reported in `fixes`
and `scan` but never raise issues on their own, because base-image findings
would make them constant noise.

## What Is Left Out

The API never returns release-note bodies or other long upstream text, and it
trims titles and reasons to short, plain strings. Treat those strings as
untrusted, especially when passing them to a language model.

It also leaves out data that is slow to compute: self-update checks (which call
GitHub) and Docker readiness checks. Use the WebUI or `/readyz` for those.

## Cost And Freshness

Each summary request reads the same state as the WebUI Pending page: the cached
WUD snapshot (at most 30 seconds old), Compose discovery, and the SQLite caches
for release notes and scans. It never refreshes release notes or runs scans.
Poll no more than once a minute, and cache on the client side if several tools
share the data.

## Versioning

Both responses carry `"v": 1`. Within version 1, fields are only added, never
renamed or removed. A breaking change gets a new `/api/integrations/v2` path.
