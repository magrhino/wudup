# WebUI Container

The WebUI container is the recommended WUDup deployment. It runs the FastAPI
backend and packaged Vue SPA from the same image, starts read-only by default,
and keeps browser access bound to loopback unless you expose it intentionally.

## Start The WebUI

Follow [Start The WebUI](../DEPLOYMENT.md#start-the-webui): it sets up one
directory with `docker-compose.yml` (the hardened example), `.env`, and
`secrets/`, then runs `docker compose up -d`. Run every `docker compose` command
in this guide from that directory. For how `.env` reaches WUDup, see
[Configuration](../CONFIGURATION.md#how-settings-reach-the-container).

The example publishes the browser endpoint on loopback:

```text
http://127.0.0.1:7417
```

Set `HOST_DOCKER_BASE` in the env file to the daemon-visible directory that
contains your Compose stacks. The same path must be mounted into the WUDup
container so Docker Compose can resolve relative bind mounts, `.env` files,
`env_file` entries, and build contexts from the host path the Docker daemon
actually sees.

The hardened example exposes the Docker socket only to the socket proxy
sidecar and points WUD and WUDup at `tcp://socket-proxy-wudup:2375`.
[`docker-compose.webui.yml`](../examples/docker-compose.webui.yml) mounts the
raw socket into both instead; use it only when the socket proxy does not fit.

Run `doctor` after changing mounts, Docker access, or helper environment:

```bash
docker compose run --rm wudup doctor
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
persistence, browser exposure, and mutation mode match the
deployment you intended, or dismiss it once those checks are understood.

Setup claims, password hashes, browser sessions, update runs, managed tag
exclusions, audit records, and managed preferences are stored in SQLite at
`WUD_DB_PATH`. The example stores that file at `/logs/wudup.sqlite`, backed by
`WEBUI_LOG_DIR` on the Compose host. Preserve that directory when recreating the
container or the WebUI will require setup again and previous history will be
lost.

## Pending Updates

The WebUI derives pending updates from WUD's `/api/containers` metadata over
the private Compose app network. When a WUD update check fails, the affected
update keeps its last successful result if WUDup saw one; otherwise its status
is shown as unknown rather than guessed. The `docker-update-from-wud` command
remains a legacy helper that reads a todo file you provide.

Pending shows current WUD health with its last check time and affected-container
diagnostics. Collapse the summary to keep its unresolved status and last check
visible while hiding the details. Preview a plan to distinguish advisory source
warnings from update blockers. Scan-request feedback clears when a newer WUD observation arrives;
request acceptance alone does not mean the scan or update succeeded. Partial scan
requests retain a warning and request details even when current health is good.

Each candidate shows **Release notes** beside its target, outside **Details**.
Release notes open directly in a compact panel with version, GitHub/source links,
and breaking/security evidence. The release body is shown
as text; **Read changelog** optionally loads a linked changelog. Source links stay
available if changelog loading fails. When the release version does not match
the candidate tag, the row shows **Upstream notes for** the release version: the
notes are general upstream information, not confirmation of what a mutable tag
or digest contains. The release notes panel states whether the release matched
the candidate. The same panel is
available under **Services and images** during plan review. Close it or press
Escape to return to the review without changing selection.

### Skip A Release

To hold back an update from Pending, open **Snooze** on its row. **Snooze for a
while** holds every update for the service for a set time, from one day to 30
days, and is saved in WUDup only.

When one release is bad for a service, choose **Skip** in the same menu (for
example **Skip 5.3.0**) instead of snoozing the whole service. Pick **Just
5.3.0** or the **Whole 5.3.x line**, and optionally note what broke. The skipped
release moves to the snoozed entries and stays there, including LinuxServer
rebuilds such as `5.3.0-r0-ls412` and `5.3.0-r0-ls413`. When WUD offers a
different release, it returns to Pending with a reminder of what you skipped and
why, so you can retest before applying. Scheduled auto-updates also respect
skips.

Releases are compared by their whole version, so skipping `3.0.0-rc1` does not
hide `3.0.0`, and `16.4-alpine` is its own release. For LinuxServer images only
the build suffix and package revision are ignored. A tag that is not a version,
such as a commit hash, is skipped as that exact tag, and a digest-only update is
skipped as that build. A skip is stored only in WUDup's database: it does not
change Compose files or WUD labels. Successfully applying an update or retag to
the service clears its skips; changing its settings, policies, or snoozes does
not. Release skips are listed, and can be deleted, on the **Snoozes** page.

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

## Tracked Containers

The Containers page lists discovered Compose services even when WUD observations
are unavailable. An unknown WUD status does not mean a service is untracked.
Select a service to compare its installed tag, current `wud.tag.include` filter,
and any candidate WUD has observed. Inspect also shows the Compose file that
defines the service, including when its image cannot be resolved.

An exact filter such as `^latest$` watches only that tag; detecting a new image
published under the same tag depends on WUD digest watching. A version filter
such as `^v\d+(?:\.\d+)+$` can follow differently named releases. WUDup does
not automatically broaden a single-number channel such as `16` to `^\d+$`,
which would also include other major-version tags. The on-page tester checks
tag names, not registry availability or whether digest watching is configured.
Changing a filter requires a label-only preview and explicit approval before
WUDup recreates that service. Repair progress and results stay beside the
selected service after Apply. WUDup coordinates its own Compose writers; do not
edit the same Compose file manually while a WUDup rewrite or repair is running.

## WUD API Authentication

WUD's administrator account is separate from the WUDup WebUI login. The Compose
examples keep its password in one file, `secrets/wud_api_password` next to
`docker-compose.yml`, and pass that file to both services as a Compose secret:

- WUD reads it with `WUD_AUTH_ADMIN_PASSWORD__FILE` and creates or updates its
  administrator, named by `WUD_AUTH_ADMIN_USER` in `.env` (default `admin`),
  on every start. This needs WUD 9.3.0 or newer.
- WUDup reads it with `WUD_API_AUTH_BASIC_PASSWORD_FILE` and signs in to WUD's
  API as that administrator.

The file must hold **only the password**, without a `:` (WUD's basic auth
rejects it). `openssl rand -hex 24` generates a suitable one. Keep the file mode
`0644` inside a mode `0700` directory: the hardened example's WUD service drops
every capability, so it can read the file only through its world-readable bit,
and the directory keeps other host users out. If the file is missing,
`docker compose up` stops with `bind source path does not exist`. To keep it
somewhere else, set `WUDUP_SECRETS_DIR` in `.env` to the directory that holds
it.

To change the password, edit the file and run `docker compose up -d
--force-recreate wud wudup`. WUD resets its administrator to the file's password
on every start, so a password changed in WUD's own UI does not survive a
restart. WUD stores its account in the named `/store` volume; preserve that
volume when recreating the service.

WUDup reaches WUD at `http://wud:3000` by default. Set `WUD_API_BASE_URL` in
`.env` only when the service name or port differs. Use the WUD service name and
its **container listening port**. If WUD listens on
a custom port, change `WUD_API_BASE_URL` accordingly; a published host port does
not change the container port. Keep both services on the same Docker network.
Direct internal access avoids reverse-proxy access lists and interactive SSO;
it still requires WUD credentials. Keep an HTTP connection confined to the
private Docker network; use HTTPS for traffic over untrusted networks.

For a dedicated integration identity, create a WUD user or personal API token
with permissions for the features you use. Read-only access covers container
metadata; rescans require write permission, and configuration diagnostics may
require administrator access. For a token, mount a token-only file read-only,
remove `WUD_API_AUTH_BASIC_USER`, `WUD_API_AUTH_BASIC_PASSWORD_FILE`, and the
`wud_api_password` secret from the `wudup` service, and set
`WUD_API_AUTH_BEARER_TOKEN_FILE` in `.env`; WUDup refuses to start with both
basic and bearer credentials. Browser login cookies and reverse-proxy
authentication do not configure this client.

Recreate WUDup after changing its credentials or mounts. Confirm the WUD API
check in WebUI **Doctor**, including access to `/api/containers`: `/health` can
return `200` while the container API returns `401`. A `401` means credentials
are missing or rejected; `403` can indicate insufficient permissions or a proxy
access rule. Connection failures instead call for checking the shared network,
service name, and listening port. Do not trigger a registry rescan just to test
connectivity.

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

When `DOCKER_HOST` points WUDup at a network endpoint such as
`tcp://socket-proxy:2375`, updates also reject a recreate scope that includes
a running container serving that endpoint. Stopping that container would cut
WUDup off from Docker, so it could not finish or roll back the update; update
the socket proxy from the host instead. A container serves the endpoint when
it has a network address that the `DOCKER_HOST` host resolves to from WUDup
(or, when the host does not resolve, a matching name or network alias), or
when it publishes the `DOCKER_HOST` port on the Docker host, which covers a
LAN IP, `127.0.0.1`, or `host.docker.internal`. A container that shares
another container's network with `network_mode` is checked through that
container. WUDup cannot tell which container in a shared network serves the
endpoint, so every service in the update that shares that network is blocked,
even one whose restart would leave the proxy running. Because the port is
matched on its own, an unrelated service that publishes the same host port is
also blocked. In both cases, update the service from the host. If those
containers cannot be inspected, the update is blocked before mutation. The
check does not recognize a proxy on the host network (`network_mode: host`) or
an endpoint set through a Docker context; avoid selecting the proxy in those
setups. Unix-socket and `ssh://` `DOCKER_HOST` values skip this check, so if
a container in the update serves an SSH endpoint, update it from the host.
WUD itself can still be updated because WUDup keeps Docker access while WUD
restarts.

WebUI retag apply runs the same checks on the services it retags before it
rewrites any Compose file. If a retag would recreate WUDup or the container
serving its `DOCKER_HOST` in any selected stack, no stack is retagged. Retag
also never removes orphan containers, so a WUDup or socket-proxy container left
running after its service was removed from the Compose file is not touched.

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
docker compose run --rm wudup web reset-admin --user admin
```

The command prints one `/#/reset-admin?claim=...` link. Opening that link lets
the named admin set a new password, revokes existing sessions for that admin,
invalidates the old password immediately, and records local audit history
without storing or printing the raw recovery claim.

## Maintenance

Pull the new image and recreate WUDup:

```bash
docker compose pull wudup
docker compose up -d --force-recreate wudup
```

For local image development, run the build example from a repository checkout
instead:

```bash
docker compose -f docs/examples/docker-compose.build.yml build wudup
docker compose -f docs/examples/docker-compose.build.yml up -d --force-recreate wudup
```

### Slow Pages

API responses that run Docker or WUD API calls include a `Server-Timing`
header. Open your browser's developer tools, select a request such as
`/api/v1/pending`, and check its timing details: `docker` and `wud` show the
time and call count for each, and `reused` counts Docker reads the request
shared instead of repeating. Every request still reads current container and
WUD state; nothing is reused across requests.
