# Configuration

WUDup configuration is environment-driven. Command-line flags override the
running process environment, and environment values override code defaults.
SQLite-backed WebUI preferences are limited to allowlisted non-secret settings
and do not replace paths, secrets, Docker commands, or updater behavior.

For host commands, start from the tracked template:

```bash
mkdir -p "$HOME/.config/wudup"
cp docs/examples/template.env "$HOME/.config/wudup/env"
```

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
| `WUD_LOCK_TIMEOUT` | `30` | Seconds to wait for the shared todo-file lock. |
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
| `WUD_WEB_TRUSTED_PROXIES` | unset | Comma-separated proxy IP/CIDR/hostname entries whose `Forwarded` or `X-Forwarded-*` headers are trusted for scheme/host detection. Hostnames resolve once at WebUI startup. |
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
| `WUD_API_BASE_URL` | `http://wud:3000` | Internal WUD API base URL used for best-effort WebUI metadata discovery and API-backed pending source modes. |
| `WUD_API_STARTUP_WAIT_SECONDS` | `0`, `5` in Compose examples | Seconds to retry the initial WUD API health probe during WebUI startup before reporting degraded WUD API discovery. |
| `WUD_API_AUTH_BEARER_TOKEN_FILE` / `WUD_API_AUTH_BEARER_TOKEN` | unset | Optional bearer token for WUDup's outbound WUD API calls. Prefer the `_FILE` form in containers; direct values are intended for local development. Do not combine bearer and basic auth. |
| `WUD_API_AUTH_BASIC_USER` + `WUD_API_AUTH_BASIC_PASSWORD_FILE` / `WUD_API_AUTH_BASIC_PASSWORD` | unset | Optional basic auth credentials for WUDup's outbound WUD API calls. The user and one password source must be set together. Prefer the `_FILE` password form in containers. |
| `WUD_API_HEADERS_FILE` | unset | Optional UTF-8 JSON object of static WUD API request headers, such as `{"X-Api-Key":"example"}`. Header names and values are validated, values are redacted, and an `Authorization` header cannot be combined with bearer or basic auth. |
| `WUD_PENDING_SOURCE` | `api` | WebUI pending-update source: `api` derives pending lines from WUD `/api/containers`, `file` reads `WUD_OUT_FILE`, and `auto` uses API metadata when usable before falling back to `WUD_OUT_FILE`. Host CLI update commands remain legacy file-mode only. |
| `WUDUP_LEGACY_SCRIPTS` | `true` | Set `false` to disable WebUI `images.todo` fallback and sync no WUD command scripts. Remove WUD command triggers for legacy scripts and recreate the stack before disabling legacy mode. |

## Candidate Security Scans

| Variable | Default | Purpose |
|---|---|---|
| `WUD_SECURITY_SCANNING_ENABLED` | `false` | Enables opt-in vulnerability advisory metadata for pending candidates, with installed-digest comparison when WUD `local_digest` metadata is available. Results do not gate updates, snooze updates, or mark an image safe. |
| `WUD_SECURITY_SCANNER_EXECUTABLE` | `trivy` | Advanced override for the scanner executable path. Not required when using a `-trivy` image tag because `trivy` is already on `PATH`. |
| `WUD_SECURITY_SCAN_CACHE_DIR` | Container: `/logs/trivy-cache`; host override: `./logs/trivy-cache` | Optional Trivy cache directory passed to scan refresh jobs. |
| `WUD_SECURITY_SCAN_TIMEOUT_SECONDS` | `300` | Per-candidate scanner timeout passed to Trivy. |

Browser scan refresh also requires `WUD_WEB_MUTATIONS_ENABLED=true`; read-only
deployments can only read cached scan metadata.

## Command Runner And Install

| Variable | Default | Purpose |
|---|---|---|
| `WUDUP_UPDATER` | Host: repo-local `bin/docker-update-from-wud`; image: `/app/bin/docker-update-from-wud` | Updater command invoked by `updates`. |
| `WUDUP_CONFIG` | `$HOME/.config/wudup/env` | Host config file read by `updates`. |
| `WUDUP_USE_SUDO` | `false` | For the Python `updates` wrapper, set to `true` only when a host install needs sudo file fallbacks and should run `WUDUP_UPDATER` through sudo. |
| `WUDUP_BANNER` | `auto` | Startup banner mode: `auto` prints on TTY startup, `true` forces it, and `false` disables it. |
| `WUDUP_RELEASE_CHECK` | `auto` | Latest-release check mode: `auto` or `true` lets startup banner, WebUI self-update banner, and self-update release checks try GitHub briefly, and `false` disables the network check. |
| `WUDUP_SELF_UPDATE` | enabled | Set to `false`, `0`, `no`, or `off` to disable the default `updates` self-update preflight. |
| `PYTHON_BIN` | `python3`, with repo `.venv` fallback when unset | Python interpreter used by Python entrypoint wrappers. Set this to bypass automatic `.venv` fallback. |
| `WUDUP_VENV` | Repo-local `.venv` | Optional installer and wrapper venv path for host runtime dependencies. |
| `WUD_SYNC_SCRIPTS` | `auto` | Set `auto` or leave unset to sync only when the managed script directory exists and is writable. Set `true` to force startup sync or `false` to opt out. |
| `WUD_SCRIPTS_DIR` | `/managed-wud` | Optional managed script sync destination override. |
| `WUD_APP_DIR` | `/app` | Application root inside the helper container. |
| `BIN_DIR` | `$HOME/bin` | Host installer destination for the `updates` and `docker-update-from-wud` symlinks. |
| `WUD_SCRIPTS_LINK` | `$DOCKER_BASE/wud/scripts` | Host installer symlink target for the mounted `wud/` scripts. |
| `WUD_OUT_DIR` | `$DOCKER_BASE/wud/out` | Host installer-created output directory that should be mounted at `/out`. |

## Release Notes And Notifications

| Variable | Default | Purpose |
|---|---|---|
| `WUD_RELEASE_NOTES_ENABLED` | unset | Optional env override for WebUI Discord release-note notifications. Leave unset to manage the setting from Settings; set `true` or `false` only when the deployment should force the value and make the Settings toggle read-only. When enabled, verified Critical/High items are delivered immediately even in `on_demand` mode. |
| `DISCORD_WEBHOOK` | unset | Discord webhook for WebUI-sent release-note notifications, including immediate verified Critical/High alerts, and shell helpers. When set, it overrides and disables the WebUI-managed webhook field. |
| `ADMIN_WEBHOOK` | selected release webhook | Optional webhook for missing LinuxServer.io upstream mapping alerts. |
| `GITHUB_TOKEN` | unset | Optional GitHub API token for higher release-note and public advisory lookup rate limits in WUD notifications and WebUI metadata refreshes. |
| `MAX_COMMITS` | `3` | Maximum representative commits or pull requests included in Discord release embeds. |
| `COLOR_HEX` | `0x57F287` | Discord embed color used by `/wud/release-notes-to-discord.sh`. |
| `UPSTREAM_MAP` | `/wud/upstreams.txt` | LinuxServer.io image to upstream repository map used by explicit LSIO release-note mode and legacy `/wud/tag-manager.sh`. |
| `RELEASE_EMBED` | `/wud/github-release-embed.sh` | Compatibility hook used when legacy `/wud/tag-manager.sh` is configured. |
| `LOG_DIR` | `/out` | Compatibility log directory used when legacy `/wud/tag-manager.sh` is configured. |

WUD supplies callback fields such as `update_available`, `image_name`,
`image_tag_value`, `image_os`, `image_architecture`, optional `image_variant`,
`name`, `update_kind_kind`, `update_kind_remote_value`, and `result_tag`. These
are runtime inputs to mounted scripts, not deployment settings you normally set
yourself.

Provide GitHub tokens through the WUDup/WebUI runtime environment, through the
WUD container environment when using the legacy shell callback path, or through
another host-local secret store. Discord release webhooks can also be saved from
WebUI Settings; the raw URL is stored in SQLite, so protect `WUD_DB_PATH` as a
secret-bearing file.

WUDup creates database files with Unix mode `0600` and tightens
existing database, WAL, SHM, and rollback-journal files before opening them for
writes. Newly created database directories use `0700`; existing directories and
unrelated logs keep their permissions. Run database clients under the same UID
(or root); the updater's configured `OUT_UID`/`OUT_GID` ownership handoff remains
supported. Database files must be regular files without symbolic or hard links.
If a database directory or its ancestors allow group/other writes, set
`WUD_DB_PATH` to a private directory instead of changing shared log permissions.
Sticky ancestors such as `/tmp` are allowed above a private database directory.
Every directory and symbolic link in the path, including alias targets, must be
owned by root, the running WUDup UID, or the explicitly configured `OUT_UID`.
Database and sidecar owners must also be one of those trusted UIDs. A private
directory beneath an untrusted user's directory is rejected because that user
could replace it. WUDup checks owners without changing existing ownership.
The filesystem must enforce these modes. Remove any inherited or named-user ACL
grants that independently allow other accounts to access the database; WUDup
does not manage platform-specific ACL policies.

Security prioritization applies only after WUD detects an update. It never
applies the container update automatically. Existing Docker Hub deployments
should adopt `WUD_REGISTRY_HUB_PUBLIC_WATCHDIGEST=true` from the current Compose
examples so WUD can detect same-tag digest changes. No additional WUDup setting
is required for security prioritization.

## TrueNAS Status Helper

| Variable | Default | Purpose |
|---|---|---|
| `TRUENAS_STATUS_CHECK` | unset | For the Python/container `updates` wrapper, set to `true` to run the short-lived local `midclt` status helper. |
| `TRUENAS_STATUS_TIMEOUT` | `5` | Seconds to wait for each helper `midclt` call before skipping it. The parent wrapper derives a longer Docker helper timeout from this value. |
| `TRUENAS_API_CLIENT_REF` | TrueNAS example: `TS-26.0.0-BETA.1`; Dockerfile default: unset | Build arg used by the TrueNAS Compose example to install a compatible TrueNAS API client. |

## Legacy Aliases

Legacy `WUD_UPDATER`, `WUD_UPDATER_CONFIG`, `WUD_UPDATER_USE_SUDO`,
`WUD_UPDATER_BANNER`, `WUD_UPDATER_RELEASE_CHECK`, `WUD_UPDATER_SELF_UPDATE`,
and `WUD_UPDATER_VENV` variables remain accepted as fallbacks. Prefer the
`WUDUP_*` names for new configuration.
