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

## Common Runtime

| Variable | Default | Purpose |
|---|---|---|
| `DOCKER_BASE` | Host: `$HOME/docker`; container examples: `${HOST_DOCKER_BASE:-/srv/docker}` | Compose project search root. Containerized runs should mount this at the same absolute path the Docker daemon uses. |
| `HOST_DOCKER_BASE` | unset | Optional daemon-visible host root matching `DOCKER_BASE` inside the helper. The path must also be mounted/readable inside the helper because Compose uses it as `--project-directory`. |
| `DOCKER_HOST` | Docker CLI default | Optional Docker daemon endpoint, such as the hardened example's socket proxy. |
| `WUD_OUT_FILE` | Host: `$DOCKER_BASE/wud/out/images.todo`; container: `/out/images.todo` | Shared pending-update file. |
| `WUD_LOG_DIR` | Host: `./logs`; container: `/logs` | Updater log directory. Set to `$DOCKER_BASE/logs` to keep the previous layout. |
| `WUD_DB_PATH` | `$WUD_LOG_DIR/wudup.sqlite` | SQLite database path for setup state, sessions, run history, audit records, and managed tag exclusions. Preserve this file for WebUI login continuity and history. |
| `WUD_UPDATE_MODE` | `stop` | Update mode for matched Compose services or stacks: `pause`, `stop`, or `live`. |
| `WUD_MAX_WAIT` | `180` | Seconds to wait for health after recreation. |
| `WUD_LOCK_TIMEOUT` | `30` | Whole seconds `docker-update-from-wud` waits for its todo-file lock. Digits only; the WebUI refuses to start with any other value. |
| `WUD_TIMEZONE` | `UTC` | IANA timezone name, such as `America/Chicago`, used for WebUI auto-update policy schedules. |
| `WUD_COMPOSE_IGNORE_PATHS` | empty | Comma-separated relative directory names or paths excluded from Compose discovery. When unset in the WebUI, the managed Settings value can control this. |
| `WUD_REGISTRY_AUTH_ORIGINS` | `{}` | JSON mapping of registry HTTPS origins to explicitly trusted separate token-server HTTPS origins. See [Registry Authentication](wiki/digest-verification.md#registry-authentication). Set in the WUDup process environment. |
| `WUD_DIGEST_PIN_UPDATES` | `false` | Opt-in digest-pin mode for approved tag updates. Environment configuration overrides the managed WebUI setting. |
| `OUT_UID` / `OUT_GID` | unset | Optional owner for rewritten todo files and updater logs. `OUT_GUID` is accepted as an alias for `OUT_GID`. |

## Image And WebUI

| Variable | Default | Purpose |
|---|---|---|
| `WUDUP_IMAGE` | `ghcr.io/magrhino/wudup:latest` | Image reference used by the long-running WebUI Compose examples. Use a `-trivy` tag when enabling candidate security scan refreshes in a container. |
| `WEBUI_LOG_DIR` | `./logs` | Host-side directory mounted at `/logs` by the long-running WebUI Compose examples; persists updater logs and SQLite state. |
| `WEBUI_HTTP_BIND` | `127.0.0.1` | Host-side bind address used by the long-running WebUI Compose examples. Keep loopback for first run; use a LAN address or `0.0.0.0` only with `WUD_WEB_PUBLIC_ORIGIN` configured. |
| `WUD_WEB_TOKEN` | unset | Optional bearer token for API clients after first-run setup. This token is not accepted by the browser login form and does not bypass setup. |
| `WUD_WEB_DEV_NO_AUTH` | `false` | Explicitly disables WebUI API auth for tests or local development only. |
| `WUD_WEB_ALLOWED_ORIGINS` | same origin only | Comma-separated extra origins accepted by the CSRF/Origin checks for login, logout, and future mutating WebUI routes. |
| `WUD_WEB_PUBLIC_ORIGIN` | unset | Public `http://` or `https://` origin used for setup links, CSRF origin checks, allowed-host derivation, and secure-cookie auto-detection. Set this for LAN or reverse-proxy exposure. |
| `WUD_WEB_ALLOWED_HOSTS` | loopback, configured public origin, and bind host | Optional comma-separated hostnames or IPs accepted in the HTTP `Host` header in addition to the public origin. Use this for extra LAN aliases or proxy-facing hostnames. |
| `WUD_WEB_TRUSTED_PROXIES` | unset | Comma-separated proxy IP/CIDR/hostname entries whose `Forwarded` or `X-Forwarded-*` headers are trusted for client address and scheme/host detection. `X-Forwarded-*` values win when present, so a proxy that sets only `Forwarded` should also strip or overwrite client-sent `X-Forwarded-*` headers. Hostnames resolve once at WebUI startup. |
| `WUD_WEB_SECURE_COOKIES` | `auto` | Cookie `Secure` mode: `auto` enables it for effective HTTPS origins, `true` always enables it, and `false` disables it for local HTTP testing. |
| `WUD_WEB_MUTATIONS_ENABLED` | `false` | Enables browser plan/apply update mutations, candidate security scan refresh jobs, and Settings container restart when set to `true`. Leave unset or `false` for read-only WebUI deployments. |
| `WUD_WEB_RESTART_CONTAINER` | Docker `HOSTNAME` inside a container, otherwise unset | Optional Docker container name or ID restarted from Settings. Set this explicitly only when the auto-detected current container target is unavailable or wrong. |
| `WUD_WEB_HOST` | Host/direct app: `127.0.0.1`; container image: `0.0.0.0` | Host passed to Uvicorn when running `wudup web`. The image default makes published Docker ports reachable; Compose still controls host-side exposure with `WEBUI_HTTP_BIND`. |
| `WUD_WEB_PORT` | `7417` | Port passed to Uvicorn when running `wudup web`. |
| `WUD_WEB_STATIC_DIR` | packaged SPA, auto-detected if present | Optional built SPA directory override. Backend tests and API startup do not require a frontend build. |
| `WUD_WEB_UPSTREAM_MAP` | auto-detected | Optional LinuxServer.io image to upstream GitHub repository map used by WebUI release-note link metadata. |

## WUD API And Pending Source

| Variable | Default | Purpose |
|---|---|---|
| `WUD_API_BASE_URL` | `http://wud:3000` | Internal WUD API base URL the WebUI reads pending updates and container metadata from. |
| `WUD_API_STARTUP_WAIT_SECONDS` | `0`, `5` in Compose examples | Seconds to retry the initial WUD API health probe during WebUI startup before reporting degraded WUD API discovery. |
| `WUD_API_AUTH_BEARER_TOKEN_FILE` / `WUD_API_AUTH_BEARER_TOKEN` | unset | Optional bearer token for WUDup's outbound WUD API calls. Prefer the `_FILE` form in containers; direct values are intended for local development. Do not combine bearer and basic auth. |
| `WUD_API_AUTH_BASIC_USER` + `WUD_API_AUTH_BASIC_PASSWORD_FILE` / `WUD_API_AUTH_BASIC_PASSWORD` | unset | Optional basic auth credentials for WUDup's outbound WUD API calls. The user and one password source must be set together. Prefer the `_FILE` password form in containers. |
| `WUD_API_HEADERS_FILE` | unset | Optional UTF-8 JSON object of static WUD API request headers, such as `{"X-Api-Key":"example"}`. Header names and values are validated, values are redacted, and an `Authorization` header cannot be combined with bearer or basic auth. |

WUD 9 requires authentication for `/api/containers` even on a private Docker
network. A successful unauthenticated `/health` probe does not establish API
access. Configure one of the outbound credential methods above using a **WUD**
account or personal API token; the WUDup WebUI login is separate.

The Compose examples also pass `WUD_AUTH_ADMIN_USER` (default `admin`) and
`WUD_AUTH_ADMIN_PASSWORD` to **WUD** to bootstrap its local administrator. Set a
strong password before first start and preserve the `wud-store:/store` volume.
These bootstrap variables do not configure WUDup's outbound API credentials.
See [WUD API authentication](wiki/webui-container.md#wud-api-authentication)
for the password-file mount and internal-port setup.

## Candidate Security Scans

| Variable | Default | Purpose |
|---|---|---|
| `WUD_SECURITY_SCANNING_ENABLED` | `false` | Enables opt-in vulnerability advisory metadata for pending candidates, with installed-digest comparison when WUD `local_digest` metadata is available. Results do not gate updates, snooze updates, or mark an image safe. |
| `WUD_SECURITY_SCANNER_EXECUTABLE` | `trivy` | Advanced override for the scanner executable path. Not required when using a `-trivy` image tag because `trivy` is already on `PATH`. |
| `WUD_SECURITY_SCAN_CACHE_DIR` | Container: `/logs/trivy-cache`; host override: `./logs/trivy-cache` | Optional Trivy cache directory passed to scan refresh jobs. |
| `WUD_SECURITY_SCAN_TIMEOUT_SECONDS` | `300` | Per-candidate scanner timeout passed to Trivy. |

Browser scan refresh also requires `WUD_WEB_MUTATIONS_ENABLED=true`; read-only
deployments can only read cached scan metadata.

## Command Runner

| Variable | Default | Purpose |
|---|---|---|
| `WUDUP_BANNER` | `auto` | Startup banner mode: `auto` prints on TTY startup, `true` forces it, and `false` disables it. |
| `WUDUP_RELEASE_CHECK` | `auto` | Latest-release check mode: `auto` or `true` lets startup banner, WebUI self-update banner, and self-update release checks try GitHub briefly, and `false` disables the network check. |
| `PYTHON_BIN` | `python3`, with repo `.venv` fallback when unset | Python interpreter used by Python entrypoint wrappers. Set this to bypass automatic `.venv` fallback. |
| `WUDUP_VENV` | Repo-local `.venv` | Optional venv path used by the `docker-update-from-wud` wrapper for runtime dependencies. |
| `WUD_APP_DIR` | `/app` | Application root inside the helper container. |

## Release Notes And Notifications

| Variable | Default | Purpose |
|---|---|---|
| `WUD_RELEASE_NOTES_ENABLED` | unset | Optional env override for WebUI Discord release-note notifications. Leave unset to manage the setting from Settings; set `true` or `false` only when the deployment should force the value and make the Settings toggle read-only. When enabled, verified Critical/High items are delivered immediately even in `on_demand` mode. |
| `DISCORD_WEBHOOK` | unset | Discord webhook for WebUI-sent release-note notifications, including immediate verified Critical/High alerts. When set, it overrides and disables the WebUI-managed webhook field. |
| `GITHUB_TOKEN` | unset | Optional GitHub API token for higher release-note and public advisory lookup rate limits in WebUI notifications and metadata refreshes. |
| `UPSTREAM_MAP` | `/app/wud/upstreams.txt` | LinuxServer.io image to upstream repository map used for WebUI release-note lookups. `WUD_WEB_UPSTREAM_MAP` takes precedence when set. |

Provide GitHub tokens through the WUDup/WebUI runtime environment or another
host-local secret store. Discord release webhooks can also be saved from
WebUI Settings; the raw URL is stored in SQLite, so protect `WUD_DB_PATH` as a
secret-bearing file.

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

Security prioritization applies only after WUD detects an update. It never
applies the container update automatically. Existing Docker Hub deployments
should adopt `WUD_REGISTRY_HUB_PUBLIC_WATCHDIGEST=true` from the current Compose
examples so WUD can detect same-tag digest changes. No additional WUDup setting
is required for security prioritization.

## Legacy Aliases

Legacy `WUD_UPDATER_BANNER`, `WUD_UPDATER_RELEASE_CHECK`, and
`WUD_UPDATER_VENV` variables remain accepted as fallbacks. Prefer the
`WUDUP_*` names for new configuration.
