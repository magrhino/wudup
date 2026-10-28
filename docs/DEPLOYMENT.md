# Deployment

This is the start-here guide for running WUDup. The recommended deployment is
the long-running WebUI container. It starts read-only, serves the API and
browser UI from one image, and keeps Docker mutations behind explicit settings
and confirmation.

WUDup controls the Docker daemon it is pointed at. Review the Docker socket or
socket-proxy access, stack-directory mounts, and output mounts before applying
updates.

## Choose A Path

| Path | Use when | Continue with |
|---|---|---|
| WebUI container (recommended) | You want the local dashboard, Doctor checks, run history, logs, diagnostics, and plan-first apply flow. | [Start The WebUI](#start-the-webui), then [WebUI Container](wiki/webui-container.md) |
| Docker script runner | You want short-lived `docker compose run` commands for `doctor`, dry runs, and applies without a persistent WebUI. | [Command Runner](COMMAND_RUNNER.md) |
| Configuration reference | You need environment variables, WUD API auth, scan settings, notification settings, or legacy aliases. | [Configuration](CONFIGURATION.md) |

The WebUI/API is the primary supported workflow. The `docker-update-from-wud`
CLI path remains a legacy file-mode convenience; API mode and CLI/WebUI feature
parity are not project goals.

## Requirements

- Docker with the Compose plugin on the host. The Compose examples need Compose
  2.24 or newer for their optional `env_file` entry.
- A Compose stack root mounted where WUDup and the Docker daemon can both see
  the same paths.
- `openssl` (or another way to generate a random password) on the host for
  WUD's administrator password.

## Start The WebUI

Run WUDup from one directory that holds three things: `docker-compose.yml`,
`.env`, and `secrets/`. These steps use the hardened example, which reaches
Docker through a socket-proxy sidecar instead of mounting the raw Docker socket
into WUDup.

```bash
mkdir -p wudup && cd wudup
curl -fsSLo docker-compose.yml \
  https://raw.githubusercontent.com/magrhino/wudup/main/docs/examples/docker-compose.hardened.yml
curl -fsSLo .env \
  https://raw.githubusercontent.com/magrhino/wudup/main/docs/examples/webui.env.example
chmod 600 .env
mkdir -m 700 secrets
openssl rand -hex 24 > secrets/wud_api_password
chmod 644 secrets/wud_api_password
```

Edit `.env` and set `HOST_DOCKER_BASE` to the host directory that contains your
Compose stacks. Keep the other defaults for the first start: browser access
stays on loopback and the WebUI starts read-only. Then start WUD and WUDup and
read the one-time setup link from the logs:

```bash
docker compose up -d
docker compose logs -f wudup
```

The setup link appears once WUDup finishes starting; press Ctrl+C to stop
following the logs. Open the printed `/#/setup?claim=...` link, create the
first admin username and a password with at least 12 characters, then sign in
at `http://127.0.0.1:7417`. Open **Doctor** in the WebUI to confirm that WUDup can
reach Docker, your stacks, and WUD's API.

What each file does:

- `docker-compose.yml` runs WUD, WUDup, and the socket proxy.
- `.env` holds your settings. Compose fills `${VAR}` references from it, and
  the `wudup` service loads it with `env_file`, which needs Compose 2.24 or
  newer. Every variable is listed in [Configuration](CONFIGURATION.md).
- `secrets/wud_api_password` holds WUD's administrator password. WUD creates its
  administrator from it, and WUDup signs in to WUD's API with it. See
  [WUD API authentication](wiki/webui-container.md#wud-api-authentication).

Run every `docker compose` command in these guides from this directory.

### Next Steps

- Open the WebUI on your LAN or behind a reverse proxy:
  [Network exposure](wiki/webui-container.md#network-exposure).
- Apply updates from the browser:
  [Read-only and mutations](wiki/webui-container.md#read-only-and-mutations).
- Pin a release instead of `latest`: [Image Tags](#image-tags).
- Recover a lost WebUI login, keep SQLite state, or turn on candidate security
  scans: [WebUI Container](wiki/webui-container.md#admin-recovery).

### Other Ways To Start

- **Raw Docker socket:** download
  [`docker-compose.webui.yml`](examples/docker-compose.webui.yml) instead of
  the hardened example. It mounts `/var/run/docker.sock` into WUDup, which gives
  it root-equivalent control of the Docker daemon. The other steps are the same.
- **Portainer or another stack manager:** set `WUDUP_ENV_FILE=stack.env` in the
  stack's environment, and set `WUDUP_SECRETS_DIR` to the absolute host path of
  the directory holding `wud_api_password`.
- **Env file kept elsewhere:** set `WUDUP_ENV_FILE` in it to the file's own path.
  `--env-file` alone only fills in `${VAR}` references.
- **Generated config:** `wudup init` can write the env file for you; see
  [Init Wizard](COMMAND_RUNNER.md#init-wizard).

## Doctor

Run `doctor` after changing container mounts, Docker access, stack paths, or
helper environment variables:

```bash
docker compose run --rm wudup doctor
```

Doctor is read-only except for short-lived permission probe files that it
creates and removes in writable runtime directories. It checks Docker CLI access,
Compose rendering, `DOCKER_BASE`, `HOST_DOCKER_BASE`, `WUD_OUT_FILE`,
`WUD_LOG_DIR`, and common helper-only path mistakes.

The authenticated WebUI Doctor page runs the same deployment checks from the
browser and adds WebUI database, auth, host/origin, secure-cookie, static asset,
and mutation-gate checks.

## Safety Defaults

- The WebUI starts read-only. Browser-initiated Docker mutations, candidate scan
  refresh jobs, and Settings container restart require
  `WUD_WEB_MUTATIONS_ENABLED=true`.
- `docker-update-from-wud --dry-run` must not pull
  images, recreate containers, remove WUD lines, or otherwise mutate host state.
- Mutating Docker operations require interactive confirmation or `--yes`.
- Mounting `/var/run/docker.sock` gives a container root-equivalent control over
  the host Docker daemon. Use trusted images and keep mounts scoped.
- Prefer the hardened Compose example when you want WUDup to access Docker
  through a socket proxy sidecar.
- Secrets such as GitHub tokens, Discord webhooks, and WUD API credentials should
  come from environment variables, `_FILE` variables, WebUI-managed settings, or
  host-local secret stores.

## Image Tags

Release images are published to GitHub Container Registry:

```bash
docker pull ghcr.io/magrhino/wudup:latest
```

Use an exact `vX.Y.Z` release tag for reproducible deployments. Release images
are also published as `X.Y.Z`, `X.Y`, and `latest`. Tags support `linux/amd64`
and `linux/arm64`; Docker selects the host platform automatically.

Deployments that previously used `ghcr.io/magrhino/wud-updater` must update
their Compose image reference to `ghcr.io/magrhino/wudup`; new releases are
published only under the `wudup` image name.

Candidate security scanning deployments can use matching Trivy-enabled suffixes:
`vX.Y.Z-trivy`, `X.Y.Z-trivy`, `X.Y-trivy`, and `latest-trivy`. Those images
include the Trivy CLI on `PATH`; the default image does not.

The `edge` and `edge-<sha>` tags (plus `edge-trivy` and `edge-<sha>-trivy`) are
unreleased builds of `main` for testing changes before a stable release. They
pass the same image scan as releases but may change or break at any time, so
use `latest` or a release tag for normal deployments. To run edge, set
`WUDUP_IMAGE=ghcr.io/magrhino/wudup:edge` (or `edge-trivy`) in your env file.
WebUI self-update is skipped on `edge` images, so pull new `edge` builds with
the [Maintenance](#maintenance) commands. Set `WUDUP_IMAGE` back to `latest` or
a release tag to receive stable releases again.

On an `edge` image, the WebUI sidebar shows `edge <sha>` linked to the running
commit. From the deployment directory, read the same commit from the running container:

```bash
docker compose exec wudup printenv WUDUP_BUILD_VERSION
```

Build a local helper image from this repository only for development or smoke
testing:

```bash
docker build -t wudup:local .
```

## Maintenance

For container-first deployments, pull the new image and recreate WUDup:

```bash
docker compose pull wudup
docker compose up -d --force-recreate wudup
```

For local image development, see [Command Runner](COMMAND_RUNNER.md). For every supported
environment variable, see [Configuration](CONFIGURATION.md).

## Upgrading

Notes for deployments set up from earlier examples or releases.

### Removed Features

WUDup no longer ships WUD shell callbacks (`on-update.sh`,
`append-updates.sh`), the shell release-note notifier scripts, WUD script sync,
the host `updates` wrapper and `install.sh`, or TrueNAS status checks. Use the
WebUI with WUD's API pending source and the built-in release-note notifier
instead. The removed container commands (`updates`, `sync-wud-scripts`,
`truenas-status-export`) exit with an error, and so do options passed without a
command (for example `docker run <image> --yes`), which used to run `updates`.
The WebUI no longer reads or edits `images.todo`: its file and `auto` pending
sources, the pending cleanup and selected-entry removal actions, and the
recovery of failed WUD checks from that file were removed. WUD's API is the only
pending source, and an update whose WUD check failed shows as unknown until WUD
reports it again. The `legacy-file-mode` git tag (v0.65.2) keeps the old code
for reference.

Existing deployments can delete the `wud-scripts` volume, its `/wud` and
`/managed-wud` mounts, WUD command triggers that call `/wud/*.sh`, and the
`WUD_SYNC_SCRIPTS`, `WUD_SCRIPTS_DIR`, `WUD_PENDING_SOURCE` and
`WUDUP_LEGACY_SCRIPTS` settings, which are now ignored with a startup warning.

### Moving To The env_file Compose Examples

The Compose examples now load `.env` into the `wudup` service with `env_file`
instead of listing each variable under `environment:`. Variable names and
defaults are unchanged, and a Compose file copied from an earlier example keeps
working as it is.

When you switch to the current examples:

- Put the env file at `.env` next to `docker-compose.yml`. If you keep it
  elsewhere and pass it with `--env-file`, add
  `WUDUP_ENV_FILE=<absolute path to that file>` to it; `--env-file` alone only
  fills in `${VAR}` references. `wudup init` writes this line for you.
- Values exported only in the shell, or set in a Portainer stack's
  environment, no longer reach WUDup unless the `environment:` block
  interpolates them (as it does `HOST_DOCKER_BASE`, `WUD_WEB_PORT`,
  `WUD_API_STARTUP_WAIT_SECONDS`, and `OUT_UID`/`OUT_GID`). Put the rest in the
  env file the `wudup` service loads. In Portainer, set
  `WUDUP_ENV_FILE=stack.env` in the stack's environment.
- Settings the earlier examples never passed to WUDup now take effect when they
  are in `.env`, for example `WUD_COMPOSE_IGNORE_PATHS`,
  `WUD_RELEASE_NOTES_ENABLED`, `WUD_WEB_RESTART_CONTAINER`, and
  `WUD_UPDATE_MODE`. Remove any you do not want before recreating WUDup.
- Every variable in `.env` also reaches the `docker compose` commands WUDup runs
  for your stacks, where it overrides the same name in a stack's own `.env`.
  Keep only WUDup and WUD settings in this file. WUDup removes
  `COMPOSE_PROJECT_NAME`, `COMPOSE_FILE`, `COMPOSE_PROFILES`,
  `COMPOSE_ENV_FILES`, `COMPOSE_PATH_SEPARATOR`, `COMPOSE_DISABLE_ENV_FILE`,
  and WUD's
  `WUD_AUTH_ADMIN_USER`/`WUD_AUTH_ADMIN_PASSWORD` first, so those apply only to
  the WUDup deployment.
- The hardened example now fixes `WUD_OUT_FILE` at `/out/images.todo`, like the
  WebUI example. If you set another path, edit the `wudup` service's
  `environment:` block instead.

### Moving To The WUD Password Secret

The Compose examples now pass WUD's administrator password to WUD and WUDup as
one Compose secret instead of `WUD_AUTH_ADMIN_PASSWORD` in `.env` plus a
separately mounted password file. A Compose file copied from an earlier example
keeps working as it is. When you switch to the current examples:

- Write WUD's current administrator password to `secrets/wud_api_password`
  next to the Compose file (mode `0644`, directory mode `0700`), or set
  `WUDUP_SECRETS_DIR` in `.env` to the directory of the password file you
  already mount. WUD resets its administrator to the file's password on every
  start, so a new password in the file replaces the old one.
- Remove `WUD_AUTH_ADMIN_PASSWORD`, `WUD_API_AUTH_BASIC_USER`, and
  `WUD_API_AUTH_BASIC_PASSWORD_FILE` from `.env`. The examples set the WUDup
  values in the `wudup` service, where they win over `.env`, and no longer pass
  `WUD_AUTH_ADMIN_PASSWORD` to WUD.
- If WUDup signs in another way, remove the two basic-auth lines and the
  secret from the `wudup` service, and keep your setting in `.env`: a WUD API
  token (`WUD_API_AUTH_BEARER_TOKEN_FILE` or `WUD_API_AUTH_BEARER_TOKEN`), a
  direct `WUD_API_AUTH_BASIC_PASSWORD`, or an `Authorization` header in
  `WUD_API_HEADERS_FILE`. WUDup refuses to start when those are combined with
  the example's basic-auth lines.
- The secret needs WUD 9.3.0 or newer; the examples use `getwud/wud:latest`.
