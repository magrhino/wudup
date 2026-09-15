# WebUI Container

The WebUI container is the recommended WUDup deployment. It runs the FastAPI
backend and packaged Vue SPA from the same image, starts read-only by default,
and keeps browser access bound to loopback unless you expose it intentionally.

## Start The WebUI

Copy the env example, review the host stack path and browser exposure settings,
then start the service:

```bash
WEBUI_ENV="$HOME/.config/wudup/webui.env"
mkdir -p "$HOME/.config/wudup"
test -f "$WEBUI_ENV" || cp docs/examples/webui.env.example "$WEBUI_ENV"
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml up -d
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml logs wudup
```

The example publishes the browser endpoint on loopback:

```text
http://127.0.0.1:7417
```

Set `HOST_DOCKER_BASE` in the env file to the daemon-visible directory that
contains your Compose stacks. The same path must be mounted into the WUDup
container so Docker Compose can resolve relative bind mounts, `.env` files,
`env_file` entries, and build contexts from the host path the Docker daemon
actually sees.

For a socket-proxy deployment, use
[`docs/examples/docker-compose.hardened.yml`](../examples/docker-compose.hardened.yml).
That example exposes the Docker socket only to the socket proxy sidecar and
points WUD/WUDup at `tcp://socket-proxy-wudup:2375`.

Run `doctor` after changing mounts, Docker access, or helper environment:

```bash
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml run --rm wudup doctor
```

The authenticated WebUI Doctor page runs the same deployment checks from the
browser and adds database, auth, origin, secure-cookie, static asset, and
mutation-gate checks.

## First Login

On first start, if no admin user exists in `WUD_DB_PATH`, the server logs a
one-time setup link. Open the `/#/setup?claim=...` URL, create the first admin
username, and choose a password with at least 12 characters. After setup, sign
in at `http://127.0.0.1:7417`.

The WebUI opens Settings with a first-run checklist after admin setup. Keep the
checklist visible until Docker access, WUD output sharing, Compose discovery,
script sync, persistence, browser exposure, and mutation mode match the
deployment you intended, or dismiss it once those checks are understood.

Setup claims, password hashes, browser sessions, update runs, managed tag
exclusions, audit records, and managed preferences are stored in SQLite at
`WUD_DB_PATH`. The example stores that file at `/logs/wudup.sqlite`, backed by
`WEBUI_LOG_DIR` on the Compose host. Preserve that directory when recreating the
container or the WebUI will require setup again and previous history will be
lost.

## Pending Updates

The WebUI can read pending updates from WUD's API, from the shared callback todo
file, or from API-first mode with file fallback:

```dotenv
WUD_PENDING_SOURCE=api
# WUD_PENDING_SOURCE=file
# WUD_PENDING_SOURCE=auto
```

The default `api` mode derives pending lines from WUD's `/api/containers`
metadata over the private Compose app network. `file` uses the legacy callback
todo file, and `auto` uses the API when usable before falling back to
`WUD_OUT_FILE`. The host `updates` and `docker-update-from-wud` commands remain
legacy file-mode helpers.

The Compose examples place WUD and WUDup on a private app network and set
`WUD_API_BASE_URL=http://wud:3000` so WUDup can read WUD metadata without
publishing WUD's port to the host. If WUD's API is protected by a proxy or
static auth layer, configure bearer, basic, or JSON header-file credentials for
WUDup's outbound WUD API client. Prefer `_FILE` variables for container secrets.

After WUD API access is healthy, you can set `WUDUP_LEGACY_SCRIPTS=false`.
Remove WUD command triggers that call `/wud/append-updates.sh`,
`/wud/on-update.sh`, or `/wud/tag-manager.sh`, then recreate the stack so stale
trigger configuration is gone. In that mode, script sync installs no WUD command
scripts, and WebUI pending behavior is API-first.

## WUD Callback Scripts

The WebUI example starts WUD and syncs packaged callback scripts into a shared
`wud-scripts` volume. For file-mode fallback, configure WUD to call:

```text
/wud/append-updates.sh
```

Use `/wud/on-update.sh` only when you intentionally keep the legacy shell
release-note notification path. WUDup polls WUD's API for WebUI release-note
notifications by default.

See [Container Script Sync](container-script-sync.md) for managed volume safety
rules and manual sync commands.

## Candidate Security Scans

Candidate security scans are opt-in advisory metadata. When WUD provides
`local_digest` metadata, refresh jobs also compare the installed digest with the
candidate. Results do not gate updates, snooze updates, bypass snoozes, or mark
an image safe.

To refresh scan results from the WebUI container, use a Trivy image variant and
enable both scan metadata and browser-triggered jobs:

```dotenv
WUDUP_IMAGE=ghcr.io/magrhino/wudup:latest-trivy
WUD_SECURITY_SCANNING_ENABLED=true
WUD_WEB_MUTATIONS_ENABLED=true
```

Read-only deployments can display cached scan results, but browser refresh
requires mutation mode because it starts scanner jobs. See
[Candidate Security Scanning Signals](security-scanning-signals.md) for the
scanner contract and cache identity rules.

## Network Exposure

For a local workstation, keep the default loopback port binding. For LAN or
reverse-proxy exposure, change the published bind address intentionally and set
the browser-visible origin:

```dotenv
WEBUI_HTTP_BIND=0.0.0.0
WUD_WEB_PUBLIC_ORIGIN=https://wud.example.test
# WUD_WEB_ALLOWED_HOSTS=updates.example.test,192.168.1.20
WUD_WEB_TRUSTED_PROXIES=127.0.0.1/32
```

Use `WUD_WEB_ALLOWED_HOSTS` only for extra host aliases that differ from the
public origin host. If TLS terminates at a reverse proxy, keep
`WUD_WEB_SECURE_COOKIES=auto` unless you have a specific local HTTP testing
reason to disable secure cookies. Only list proxy IPs, CIDRs, or hostnames you
control in `WUD_WEB_TRUSTED_PROXIES`; hostnames resolve once at WebUI startup,
so restart WUDup after proxy container IP changes.

The packaged image defines a default Docker healthcheck against `/readyz`, a
no-auth readiness endpoint. Override it only when you need custom timing or a
non-WebUI command. API clients should use the authenticated `/api/v1/ready`
endpoint.

## Read-Only And Mutations

The WebUI is read-only by default. It can display pending updates, run history,
logs, diagnostics, and settings without enabling browser-initiated Docker
mutations.

To apply updates, refresh candidate scans, or restart the WUDup container from
the browser, set:

```dotenv
WUD_WEB_MUTATIONS_ENABLED=true
WUD_TIMEZONE=America/Chicago
```

Mutation requests still require an authenticated browser session, CSRF checks,
Origin/Host validation, one active job at a time, audit records, and the
WebUI's plan-first apply flow. Keep the Docker socket or socket proxy, stack
root, WUD output file, logs, and SQLite database mounted as intended before
enabling mutation mode.

Normal WebUI apply jobs, including scheduled jobs, reject a recreate scope
that includes WUDup before changing Compose files or removing pending entries.
This also covers a sibling service whose stack-level recreate would include
WUDup. Remove that selection to update other services; use the self-update
action for WUDup or recreate its stack from the host.
If a runtime target is configured or auto-detected, its container identity and
the selected Compose containers must be readable; a lookup failure blocks the
job before mutation. Check Docker access and `WUD_WEB_RESTART_CONTAINER` before
retrying.

WebUI self-update pulls prepare an image but do not treat `docker restart` as
adoption. The response verifies the running container image ID after the pull
and reports `prepared_only` until an external Compose recreate moves the
container onto the prepared image. If the current container image reference
cannot be inspected with enough tag context to preserve its variant,
self-update fails closed so a `-trivy` image cannot be silently replaced by the
default image.

Runtime configuration comes from command-line overrides, then environment, then
code defaults. SQLite-backed managed preferences are limited to allowlisted
non-secret WebUI preferences such as theme, onboarding state, and managed
settings. They do not override CLI arguments, environment variables, paths,
secrets, Docker commands, or updater behavior.

## Admin Recovery

If the admin password is lost or you need to rotate admin credentials, run the
recovery command against the same `WUD_DB_PATH`:

```bash
wudup web reset-admin --user admin
```

For the Compose example, run it through the WebUI container so it uses the
mounted SQLite database:

```bash
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml run --rm wudup web reset-admin --user admin
```

The command prints one `/#/reset-admin?claim=...` link. Opening that link lets
the named admin set a new password, revokes existing sessions for that admin,
invalidates the old password immediately, and records local audit history
without storing or printing the raw recovery claim.

## Maintenance

Pull the new image and recreate WUDup so startup sync refreshes the managed WUD
script volume:

```bash
WEBUI_ENV="${WEBUI_ENV:-$HOME/.config/wudup/webui.env}"
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml pull wudup
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml up -d --force-recreate wudup
```

For local image development, use the build example instead:

```bash
docker compose -f docs/examples/docker-compose.build.yml build wudup
docker compose -f docs/examples/docker-compose.build.yml up -d --force-recreate wudup
```
