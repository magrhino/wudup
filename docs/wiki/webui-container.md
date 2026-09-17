# WebUI Container

The WebUI container is the recommended WUDup deployment. It runs the FastAPI
backend and packaged Vue SPA from the same image, starts read-only by default,
and keeps browser access bound to loopback unless you expose it intentionally.

## Start The WebUI

Copy the env example, review the host stack path and browser exposure settings,
and configure [WUD API authentication](#wud-api-authentication) before starting
the service. WUD 9 needs administrator bootstrap credentials on first start and
WUDup needs separate outbound API settings:

```bash
WEBUI_ENV="$HOME/.config/wudup/webui.env"
mkdir -p "$HOME/.config/wudup"
test -f "$WEBUI_ENV" || cp docs/examples/webui.env.example "$WEBUI_ENV"
chmod 600 "$WEBUI_ENV"
# Edit WEBUI_ENV: set WUD_AUTH_ADMIN_PASSWORD and one WUD_API_AUTH_* method.
# For a _FILE credential, also add its read-only mount as described below.
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

Pending shows current WUD health with its last check time and affected-container
diagnostics. Collapse the summary to keep its unresolved status and last check
visible while hiding the details. Preview a plan to distinguish advisory source
warnings from update blockers. Scan-request feedback clears when a newer WUD observation arrives;
request acceptance alone does not mean the scan or update succeeded. Partial scan
requests retain a warning and request details even when current health is good.

Each candidate shows **Release notes** and available GitHub/source links beside
its target, outside **Details**. Release notes open directly in a compact panel
with version, source, and breaking/security evidence. The release body is shown
as text; **Read changelog** optionally loads a linked changelog. Source links stay
available if changelog loading fails. **Matched to candidate** means the release
version matches the candidate tag; **Upstream context** is general information,
not confirmation of what a mutable tag or digest contains. The same panel is
available under **Services and images** during plan review. Close it or press
Escape to return to the review without changing selection.

If an update job disappears after a WebUI restart, its recovery notice links to
the related run when known. Otherwise, review History to identify the update.
**Check outcome** reads the related job/run evidence. **Acknowledge and collapse**
keeps unresolved outcomes visible and does not mark them successful. A verified
successful run allows the notice to be dismissed. Recovery notices are retained
in the current browser tab's session storage, including across page reloads.

The Compose examples place WUD and WUDup on a private app network and set
`WUD_API_BASE_URL=http://wud:3000` so WUDup can read WUD metadata without
publishing WUD's port to the host. WUD 9 requires API credentials on this private
network too; configure them as described below.

After WUD API access is healthy, you can set `WUDUP_LEGACY_SCRIPTS=false`.
Remove WUD command triggers that call `/wud/append-updates.sh`,
`/wud/on-update.sh`, or `/wud/tag-manager.sh`, then recreate the stack so stale
trigger configuration is gone. In that mode, script sync installs no WUD command
scripts, and WebUI pending behavior is API-first.

## WUD API Authentication

WUD and WUDup have separate accounts. In the Compose env file, set
`WUD_AUTH_ADMIN_USER=admin` and a strong generated `WUD_AUTH_ADMIN_PASSWORD`
before WUD's first start. WUD stores its account in the named `/store` volume;
preserve that volume when recreating the service. Supplying bootstrap credentials
again updates that administrator's password. An existing deployment with a
stored administrator can leave the bootstrap password unset.

For basic API authentication, create a private host file containing **only the
WUD password**, with mode `0600`, readable by the WUDup runtime user. Do not
mount the entire Compose env file as the password file. Add this read-only mount
to the `wudup` service's existing `volumes` list (example host path):

```yaml
      - /srv/wudup/secrets/wud_api_password:/run/secrets/wud_api_password:ro
```

Then set these values in the Compose env file:

```dotenv
WUD_API_BASE_URL=http://wud:3000
WUD_API_AUTH_BASIC_USER=admin
WUD_API_AUTH_BASIC_PASSWORD_FILE=/run/secrets/wud_api_password
```

Use the WUD service name and its **container listening port**. If WUD listens on
a custom port, change `WUD_API_BASE_URL` accordingly; a published host port does
not change the container port. Keep both services on the same Docker network.
Direct internal access avoids reverse-proxy access lists and interactive SSO;
it still requires WUD credentials. Keep an HTTP connection confined to the
private Docker network; use HTTPS for traffic over untrusted networks.

For a dedicated integration identity, create a WUD user or personal API token
with permissions for the features you use. Read-only access covers container
metadata; rescans require write permission, and configuration diagnostics may
require administrator access. For a token, mount a token-only file read-only and
set `WUD_API_AUTH_BEARER_TOKEN_FILE` instead of the basic-auth variables. Browser
login cookies and reverse-proxy authentication do not configure this client.

Recreate WUDup after changing its credentials or mounts. Confirm the WUD API
check in WebUI **Doctor**, including access to `/api/containers`: `/health` can
return `200` while the container API returns `401`. A `401` means credentials
are missing or rejected; `403` can indicate insufficient permissions or a proxy
access rule. Connection failures instead call for checking the shared network,
service name, and listening port. Do not trigger a registry rescan just to test
connectivity.

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

The container automatically listens on IPv4 and IPv6 on port `7417` (or
`WUD_WEB_PORT`). No bind-address override is needed: the image's default
`WUD_WEB_HOST=0.0.0.0` opens separate IPv4 and IPv6 listeners. If IPv6 is
unavailable, it logs a warning and continues with IPv4. Reverse proxies on the
same Docker network can forward to `wudup:7417` using either address family.
An explicit concrete bind address remains limited to that address; `::`
explicitly selects IPv6 only. Outside the container, the CLI still defaults
to IPv4 loopback (`127.0.0.1`).

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
Recreate-scope discovery also fails closed on lookup errors. Each approved
scope is retained for execution, so a later label change cannot expand the
services that the job stops or recreates.

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
