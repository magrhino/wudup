# Configuration

WUDup configuration is environment-driven. Command-line flags override the
running process environment, and environment values override code defaults.
SQLite-backed WebUI preferences are limited to allowlisted non-secret settings
and do not replace paths, secrets, Docker commands, or updater behavior.

For commands run outside the container, copy and edit the tracked template
[`docs/examples/template.env`](examples/template.env), then export it into the
process environment. WUDup does not read an env file automatically.

Boolean values use `true` and `false`; legacy aliases `1`, `0`, `yes`, `no`,
`on`, and `off` are still accepted where boolean parsing is supported.

## How Settings Reach The Container

In the Compose examples, keep the env file at `.env` next to the Compose file.
Compose uses it twice: it fills in `${VAR}` references in the Compose file, and
the `wudup` service loads the same file with `env_file`, so every variable in
it reaches WUDup. The `env_file` entry is optional (`required: false`) and needs
Compose 2.24 or newer.

The `wudup` service's `environment:` block wins over `.env`. In the WebUI
examples it pins the [fixed container paths](#fixed-container-paths) and sets
the Compose defaults that differ from WUDup's own: `WUD_WEB_PORT`,
`WUD_API_STARTUP_WAIT_SECONDS`, and `OUT_UID`/`OUT_GID`. Those defaults still
come from `.env` through `${VAR}` references. It also signs WUDup in to WUD's
API with the shared [WUD password secret](#wud-password-secret):
`WUD_API_AUTH_BASIC_USER` comes from `WUD_AUTH_ADMIN_USER`, and
`WUD_API_AUTH_BASIC_PASSWORD_FILE` is fixed at `/run/secrets/wud_api_password`.
`HOST_DOCKER_BASE` is set from the
same `${HOST_DOCKER_BASE}` reference as `DOCKER_BASE` and the stack mount, so
WUDup always maps stacks with the root Compose mounted. The command-runner
examples pin only their fixed paths, `HOST_DOCKER_BASE`, and `OUT_UID`/`OUT_GID`.

A file passed with `--env-file` only fills in `${VAR}` references. To keep the
env file somewhere else, set `WUDUP_ENV_FILE` in it to the file's own path (or
export it in the shell) so the `wudup` service loads that file instead of
`.env`. Variables exported only in the shell, or set in a Portainer stack's
environment, fill in `${VAR}` references. They reach WUDup only through the
variables the `environment:` block interpolates (such as `HOST_DOCKER_BASE` and
`WUD_WEB_PORT`) or when the file `env_file` loads also contains them.

Every variable WUDup receives also reaches the `docker compose` commands it
runs for your stacks, where it overrides the same name in a stack's own `.env`,
so keep only WUDup and WUD settings in this file. WUDup removes
`COMPOSE_PROJECT_NAME`, `COMPOSE_FILE`, `COMPOSE_PROFILES`, `COMPOSE_ENV_FILES`,
`COMPOSE_PATH_SEPARATOR`, `COMPOSE_DISABLE_ENV_FILE`, and WUD's
`WUD_AUTH_ADMIN_USER`/`WUD_AUTH_ADMIN_PASSWORD` before it runs Compose for your
stacks, because in its own environment they describe the WUDup deployment.

WUD receives only the variables listed under its own service and never loads
`.env` with `env_file`. WUD reads its own `WUD_*` settings, so do not pass
WUDup variables to the `wud` service.

## Variable Groups

| Group | Set it in |
|---|---|
| [First-run Compose settings](#first-run-compose-settings) | Compose env file, before the first start. |
| [Fixed container paths](#fixed-container-paths) | Set by the Compose examples; change the mount, not the variable. |
| [Secrets](#secrets) | Compose env file or a mounted secret file; prefer `_FILE` forms. |
| [Exposure and mutation policy](#exposure-and-mutation-policy) | Compose env file only; the WebUI cannot change these. |
| [Environment overrides of WebUI settings](#environment-overrides-of-webui-settings) | WebUI Settings by default; set the variable only to force a value. |
| [Advanced settings](#advanced-settings) | Compose env file, only when a default does not fit. |
| [Development and image-internal](#development-and-image-internal) | Tests, local development, or the image build; not deployments. |
| [Legacy](#legacy) | Accepted for compatibility or ignored; prefer current names. |

## First-Run Compose Settings

Set these in the Compose env file. Set `HOST_DOCKER_BASE` to your stack root;
the other defaults fit a loopback, read-only first run.

| Variable | Default | Purpose |
|---|---|---|
| `WUDUP_ENV_FILE` | `.env` | Read only by Compose, from the shell or a file passed with `--env-file`. Names the env file the `wudup` service loads with `env_file`; a relative path is relative to the Compose file. `wudup init` writes it into the env file it generates. |
| `WUDUP_SECRETS_DIR` | `./secrets` | Read only by Compose. Directory that holds `wud_api_password`, the [WUD password secret](#wud-password-secret); a relative path is relative to the Compose file. |
| `HOST_DOCKER_BASE` | Compose examples: `/srv/docker`; host commands: unset | Host directory that contains your Compose stacks. The Compose examples mount it at the same absolute path and set `DOCKER_BASE` from it. For host commands it is an optional daemon-visible host root matching `DOCKER_BASE` inside the helper. The path must also be mounted/readable inside the helper because Compose uses it as `--project-directory`. |
| `WEBUI_LOG_DIR` | `./logs` | Host-side directory mounted at `/logs` by the long-running WebUI Compose examples; persists updater logs and SQLite state. |
| `WEBUI_HTTP_BIND` | `127.0.0.1` | Host-side bind address used by the long-running WebUI Compose examples. Keep loopback for first run; use a LAN address or `0.0.0.0` only with `WUD_WEB_PUBLIC_ORIGIN` configured. |
| `WUD_WEB_PORT` | `7417` | Port passed to Uvicorn when running `wudup web`. The WebUI Compose examples also publish this port on `WEBUI_HTTP_BIND`. |
| `WUDUP_IMAGE` | `ghcr.io/magrhino/wudup:latest` | Image reference used by the long-running WebUI Compose examples. Use a `-trivy` tag when enabling candidate security scan refreshes in a container. |
| `OUT_UID` / `OUT_GID` | Compose examples: `1000`; host commands: unset | Optional owner for rewritten todo files and updater logs. `OUT_GUID` is accepted as an alias for `OUT_GID`. |
| `WUD_TIMEZONE` | `UTC` | IANA timezone name, such as `America/Chicago`, used for WebUI auto-update policy schedules. |

## Fixed Container Paths

The Compose examples set these to paths inside the container that match the
mounts they define. In a container, change the mount (or `HOST_DOCKER_BASE`)
rather than the variable. Host commands set them directly.

| Variable | Default | Purpose |
|---|---|---|
| `DOCKER_BASE` | Host: `$HOME/docker`; container examples: `${HOST_DOCKER_BASE:-/srv/docker}` | Compose project search root. Containerized runs should mount this at the same absolute path the Docker daemon uses. |
| `DOCKER_HOST` | Docker CLI default | Optional Docker daemon endpoint, such as the hardened example's socket proxy. |
| `WUD_OUT_FILE` | Host: `$DOCKER_BASE/wud/out/images.todo`; container: `/out/images.todo` | Shared pending-update file. |
| `WUD_LOG_DIR` | Host: `./logs`; container: `/logs` | Updater log directory. Set to `$DOCKER_BASE/logs` to keep the previous layout. |
| `WUD_DB_PATH` | `$WUD_LOG_DIR/wudup.sqlite` | SQLite database path for setup state, sessions, run history, audit records, and managed tag exclusions. Preserve this file for WebUI login continuity and history. |
| `HOME` | Container user's home; hardened example: `/tmp` | Home directory for Docker CLI configuration. The hardened example points it at the `/tmp` tmpfs so the Docker CLI has a writable home on the read-only root filesystem. |

See [Database File Permissions](#database-file-permissions) for how WUDup
protects `WUD_DB_PATH`.

## Secrets

Prefer the `_FILE` forms in containers: mount a file that contains only the
secret, read-only, and point the variable at it. Only the WUD API credentials
have `_FILE` forms today.

| Variable | Default | Purpose |
|---|---|---|
| `WUD_AUTH_ADMIN_USER` | `admin` | Read by **WUD** to bootstrap its local administrator. The Compose examples also use it as `WUD_API_AUTH_BASIC_USER`; see [WUD password secret](#wud-password-secret). |
| `WUD_AUTH_ADMIN_PASSWORD` | unset | Read by **WUD**. The Compose examples no longer pass it; they pass the [WUD password secret](#wud-password-secret) as `WUD_AUTH_ADMIN_PASSWORD__FILE` instead. |
| `WUD_API_AUTH_BEARER_TOKEN_FILE` / `WUD_API_AUTH_BEARER_TOKEN` | unset | Optional bearer token for WUDup's outbound WUD API calls. Prefer the `_FILE` form in containers; direct values are intended for local development. Do not combine bearer and basic auth. |
| `WUD_API_AUTH_BASIC_USER` + `WUD_API_AUTH_BASIC_PASSWORD_FILE` / `WUD_API_AUTH_BASIC_PASSWORD` | unset | Optional basic auth credentials for WUDup's outbound WUD API calls. The user and one password source must be set together. Prefer the `_FILE` password form in containers. |
| `WUD_API_HEADERS_FILE` | unset | Optional UTF-8 JSON object of static WUD API request headers, such as `{"X-Api-Key":"example"}`. Header names and values are validated, values are redacted, and an `Authorization` header cannot be combined with bearer or basic auth. |
| `WUD_WEB_TOKEN` | unset | Optional bearer token for API clients after first-run setup. Use a long random value of at least 32 characters; startup warns about shorter tokens. Five wrong tokens from one client address lock out bearer requests from that address for a minute, including the correct token; password logins are unaffected. Behind a reverse proxy, list it in `WUD_WEB_TRUSTED_PROXIES` so clients are not grouped under the proxy address. This token is not accepted by the browser login form and does not bypass setup. |
| `GITHUB_TOKEN` | unset | Optional GitHub API token for higher release-note and public advisory lookup rate limits in WebUI notifications and metadata refreshes. |
| `DISCORD_WEBHOOK` | unset | Discord webhook for WebUI-sent release-note notifications, including immediate verified Critical/High alerts. When set, it overrides and disables the WebUI-managed webhook field. |

WUD 9 requires authentication for `/api/containers` even on a private Docker
network. A successful unauthenticated `/health` probe does not establish API
access. Configure one of the outbound credential methods above using a **WUD**
account or personal API token; the WUDup WebUI login is separate.

Provide GitHub tokens through the WUDup/WebUI runtime environment or another
host-local secret store. Discord release webhooks can also be saved from
WebUI Settings; the raw URL is stored in SQLite, so protect `WUD_DB_PATH` as a
secret-bearing file.

### WUD Password Secret

The Compose examples keep WUD's administrator password in one file,
`${WUDUP_SECRETS_DIR:-./secrets}/wud_api_password`, and mount it in both
services as the `wud_api_password` Compose secret. WUD reads it with
`WUD_AUTH_ADMIN_PASSWORD__FILE` (WUD 9.3.0 or newer) to bootstrap its
administrator, and the WebUI examples' `wudup` service reads it with
`WUD_API_AUTH_BASIC_PASSWORD_FILE`. Create the file before first start, keep it
mode `0644` inside a mode `0700` directory, and preserve the `wud-store:/store`
volume. See [WUD API authentication](wiki/webui-container.md#wud-api-authentication)
for generating the password, changing it, and using a WUD API token instead.

## Exposure And Mutation Policy

These settings are security boundaries, so they are environment-only: the
WebUI cannot change them. Keep the defaults for a loopback, read-only first run.

| Variable | Default | Purpose |
|---|---|---|
| `WUD_WEB_PUBLIC_ORIGIN` | unset | Public `http://` or `https://` origin used for setup links, CSRF origin checks, allowed-host derivation, and secure-cookie auto-detection. Set this for LAN or reverse-proxy exposure. |
| `WUD_WEB_ALLOWED_HOSTS` | loopback, configured public origin, and bind host | Optional comma-separated hostnames or IPs accepted in the HTTP `Host` header in addition to the public origin. Use this for extra LAN aliases or proxy-facing hostnames. |
| `WUD_WEB_ALLOWED_ORIGINS` | same origin only | Comma-separated extra origins accepted by the CSRF/Origin checks for login, logout, and future mutating WebUI routes. |
| `WUD_WEB_TRUSTED_PROXIES` | unset | Comma-separated proxy IP/CIDR/hostname entries whose `Forwarded` or `X-Forwarded-*` headers are trusted for client address and scheme/host detection. `X-Forwarded-*` values win when present, so a proxy that sets only `Forwarded` should also strip or overwrite client-sent `X-Forwarded-*` headers. Hostnames resolve once at WebUI startup. |
| `WUD_WEB_SECURE_COOKIES` | `auto` | Cookie `Secure` mode: `auto` enables it for effective HTTPS origins, `true` always enables it, and `false` disables it for local HTTP testing. |
| `WUD_WEB_MUTATIONS_ENABLED` | `false` | Enables browser plan/apply update mutations, candidate security scan refresh jobs, and Settings container restart when set to `true`. Leave unset or `false` for read-only WebUI deployments. |

## Environment Overrides Of WebUI Settings

Leave these unset to manage the value from WebUI Settings. When one is set, the
environment value wins and Settings shows the control as read-only with the
reason. [`DISCORD_WEBHOOK`](#secrets) works the same way for the webhook field.

| Variable | Default | Purpose |
|---|---|---|
| `WUD_COMPOSE_IGNORE_PATHS` | empty | Comma-separated relative directory names or paths excluded from Compose discovery. When unset in the WebUI, the managed Settings value can control this. |
| `WUD_DIGEST_PIN_UPDATES` | `false` | Opt-in digest-pin mode for approved tag updates. Environment configuration overrides the managed WebUI setting. |
| `WUD_RELEASE_NOTES_ENABLED` | unset | Optional env override for WebUI Discord release-note notifications. Leave unset to manage the setting from Settings; set `true` or `false` only when the deployment should force the value and make the Settings toggle read-only. When enabled, verified Critical/High items are delivered immediately even in `on_demand` mode. |

These preferences have no environment variable and are stored in SQLite from
Settings: theme, sidebar badges, onboarding checklist, retag digest pins, and
the release-notification delivery mode, grouping, resend policy, cooldown, and
verbosity.

## Advanced Settings

The defaults fit the Compose examples. Change these only for a specific need.

| Variable | Default | Purpose |
|---|---|---|
| `WUD_API_BASE_URL` | `http://wud:3000` | Internal WUD API base URL the WebUI reads pending updates and container metadata from. When WUD API auth or static headers are configured, they are never sent to a redirected address; a redirect is reported as a configuration error, so point this at the address that serves the WUD API directly. |
| `WUD_API_STARTUP_WAIT_SECONDS` | `0`, `5` in Compose examples | Seconds to retry the initial WUD API health probe during WebUI startup before reporting degraded WUD API discovery. |
| `WUD_UPDATE_MODE` | `stop` | Update mode for matched Compose services or stacks: `pause`, `stop`, or `live`. |
| `WUD_MAX_WAIT` | `180` | Seconds to wait for health after recreation. |
| `WUD_LOCK_TIMEOUT` | `30` | Whole seconds `docker-update-from-wud` waits for its todo-file lock. Digits only; the WebUI refuses to start with any other value. |
| `WUD_REGISTRY_AUTH_ORIGINS` | `{}` | JSON mapping of registry HTTPS origins to explicitly trusted separate token-server HTTPS origins. See [Registry Authentication](wiki/digest-verification.md#registry-authentication). Set in the WUDup process environment. |
| `WUD_WEB_RESTART_CONTAINER` | Docker `HOSTNAME` inside a container, otherwise unset | Optional Docker container name or ID restarted from Settings. Set this explicitly only when the auto-detected current container target is unavailable or wrong. |
| `WUD_WEB_HOST` | Host/direct app: `127.0.0.1`; container image: `0.0.0.0` | Host passed to Uvicorn when running `wudup web`. The image default makes published Docker ports reachable; Compose still controls host-side exposure with `WEBUI_HTTP_BIND`. |
| `WUD_WEB_STATIC_DIR` | packaged SPA, auto-detected if present | Optional built SPA directory override. Backend tests and API startup do not require a frontend build. |
| `WUD_WEB_UPSTREAM_MAP` | auto-detected | Optional LinuxServer.io image to upstream GitHub repository map used by WebUI release-note link metadata. |
| `UPSTREAM_MAP` | `/app/wud/upstreams.txt` | LinuxServer.io image to upstream repository map used for WebUI release-note lookups. `WUD_WEB_UPSTREAM_MAP` takes precedence when set. |
| `WUDUP_BANNER` | `auto` | Startup banner mode: `auto` prints on TTY startup, `true` forces it, and `false` disables it. |
| `WUDUP_RELEASE_CHECK` | `auto` | Latest-release check mode: `auto` or `true` lets startup banner, WebUI self-update banner, and self-update release checks try GitHub briefly, and `false` disables the network check. |
| `NO_COLOR` | unset | Standard convention: when set to any value, command-line output skips Rich formatting and color. |

### Candidate Security Scans

| Variable | Default | Purpose |
|---|---|---|
| `WUD_SECURITY_SCANNING_ENABLED` | `false` | Enables opt-in vulnerability advisory metadata for pending candidates, with installed-digest comparison when WUD `local_digest` metadata is available. Results do not gate updates, snooze updates, or mark an image safe. |
| `WUD_SECURITY_SCANNER_EXECUTABLE` | `trivy` | Advanced override for the scanner executable path. Not required when using a `-trivy` image tag because `trivy` is already on `PATH`. |
| `WUD_SECURITY_SCAN_CACHE_DIR` | Container: `/logs/trivy-cache`; host override: `./logs/trivy-cache` | Optional Trivy cache directory passed to scan refresh jobs. |
| `WUD_SECURITY_SCAN_TIMEOUT_SECONDS` | `300` | Per-candidate scanner timeout passed to Trivy. |

Browser scan refresh also requires `WUD_WEB_MUTATIONS_ENABLED=true`; read-only
deployments can only read cached scan metadata.

Security prioritization applies only after WUD detects an update. It never
applies the container update automatically. Existing Docker Hub deployments
should adopt `WUD_REGISTRY_HUB_PUBLIC_WATCHDIGEST=true` from the current Compose
examples so WUD can detect same-tag digest changes. No additional WUDup setting
is required for security prioritization.

## Development And Image-Internal

| Variable | Default | Purpose |
|---|---|---|
| `WUD_WEB_DEV_NO_AUTH` | `false` | Explicitly disables WebUI API auth for tests or local development only. |
| `WUD_WEB_DEMO_SELF_UPDATE` | `false` | Used only by the static demo fixtures to return a canned self-update response. Do not set it in deployments. |
| `PYTHON_BIN` | `python3`, with repo `.venv` fallback when unset | Python interpreter used by Python entrypoint wrappers. Set this to bypass automatic `.venv` fallback. |
| `WUDUP_VENV` | Repo-local `.venv` | Optional venv path used by the `docker-update-from-wud` wrapper for runtime dependencies. |
| `WUD_APP_DIR` | `/app` | Application root inside the helper container. |
| `WUDUP_BUILD_VERSION` | Empty; `edge-<sha>` in edge images | Set by the image build, not by operators. Edge images record their commit here; the WebUI shows it and `/api/v1/status` returns it as `build_version`. |

Settings also lists derived values named like variables:
`WUD_WEB_AUTH_REQUIRED`, `WUD_WEB_AUTO_UPDATE_SCHEDULER_ENABLED`,
`WUD_WEB_SECURE_COOKIES_EFFECTIVE`, and `WUD_WEB_STATIC_SPA_AVAILABLE`. WUDup
computes them from other settings; setting them has no effect.

## Database File Permissions

WUDup creates database files with Unix mode `0600` and tightens
existing database, WAL, SHM, and rollback-journal files before opening them for
writes. Newly created database directories use `0700`, with newly created
intermediate directories set to `0755` so the configured owner can traverse them.
WUDup enforces these modes even with a restrictive umask, without changing the
process-wide umask. Existing intermediate directories keep their permissions.
WUDup also repairs an existing database directory to `0700` if it allows
group/other writes and is owned
by root, the running WUDup UID, or configured `OUT_UID`. This covers first startup
with a pre-created volume directory and upgrades with existing databases. Only
the database directory is repaired; ancestor directories and unrelated log files
stay unchanged. When running as root with `OUT_UID` configured, WUDup also assigns
the repaired directory to that UID before restricting access to `0700`, so the
configured owner can still reach its database files. This ownership handoff also
applies to a root-owned database directory that is already `0700`. Existing
traversable directories such as `0755` retain their owner and permissions. The
directory's group stays unchanged. Use a dedicated directory for `WUD_DB_PATH`
if other applications need shared access to your logs. Run database clients under
the same UID (or root); the configured `OUT_UID`/`OUT_GID` ownership handoff remains
supported. Database files must be regular files without symbolic or hard links.
If ancestor directories allow group/other writes, set `WUD_DB_PATH` to a private
directory under trusted parents. WUDup does not repair sticky shared directories.
Sticky ancestors such as `/tmp` are allowed above a private database directory.
Every directory and symbolic link in the path, including alias targets, must be
owned by root, the running WUDup UID, or the explicitly configured `OUT_UID`.
Database and sidecar owners must also be one of those trusted UIDs. A private
directory beneath an untrusted user's directory is rejected because that user
could replace it. WUDup checks owners before changing permissions or handing off
a repaired directory to `OUT_UID`.
The filesystem must enforce these modes. Remove any inherited or named-user ACL
grants that independently allow other accounts to access the database; WUDup
does not manage platform-specific ACL policies.

## Legacy

Legacy `WUD_UPDATER_BANNER`, `WUD_UPDATER_RELEASE_CHECK`, and
`WUD_UPDATER_VENV` variables remain accepted as fallbacks. Prefer the
`WUDUP_*` names for new configuration.

`OUT_GUID` is accepted as an alias for `OUT_GID`.

`WUD_SYNC_SCRIPTS`, `WUD_SCRIPTS_DIR`, `WUD_PENDING_SOURCE`, and
`WUDUP_LEGACY_SCRIPTS` are ignored with a startup warning and can be deleted.
See [Removed Features](DEPLOYMENT.md#removed-features).
