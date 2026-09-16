# Deployment

This is the start-here guide for running WUDup. The recommended deployment is
the long-running WebUI container. It starts read-only, serves the API and
browser UI from one image, and keeps Docker mutations behind explicit settings
and confirmation.

WUDup controls the Docker daemon it is pointed at. Review the Docker socket or
socket-proxy access, stack-directory mounts, script mounts, and output mounts
before applying updates.

## Choose A Path

| Path | Use when | Continue with |
|---|---|---|
| WebUI container | You want the recommended local dashboard, Doctor checks, run history, logs, diagnostics, and plan-first apply flow. | [WebUI Container](wiki/webui-container.md) |
| Hardened WebUI container | You want the WebUI behind a Docker socket proxy instead of mounting the raw socket into WUDup. | [WebUI Container](wiki/webui-container.md) and [`docker-compose.hardened.yml`](examples/docker-compose.hardened.yml) |
| Docker script runner | You want short-lived `docker compose run` commands for `doctor`, dry runs, and applies without a persistent WebUI. | [Command Runner And Host Install](COMMAND_RUNNER.md) |
| Host install | You want `updates` and `docker-update-from-wud` on the host `PATH` with host-managed WUD script mounts. | [Command Runner And Host Install](COMMAND_RUNNER.md) |
| Configuration reference | You need environment variables, WUD API auth, scan settings, notification settings, or legacy aliases. | [Configuration](CONFIGURATION.md) |

The WebUI/API is the primary supported workflow. The `updates` and
`docker-update-from-wud` CLI paths remain supported legacy file-mode
conveniences; API mode and CLI/WebUI feature parity are not project goals.

## Requirements

- Docker with the Compose plugin on the host.
- A Compose stack root mounted where WUDup and the Docker daemon can both see
  the same paths.
- Bash and Python 3.10 or newer for host-installed commands.
- Standard shell tools used by wrappers and callbacks: `awk`, `sort`, `sed`,
  `perl`, `find`, `grep`, `cut`, `column`, and `mktemp`.
- `curl` and `jq` for release-note helper scripts.
- `midclt` only for local TrueNAS status checks.

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

Build a local helper image from this repository only for development or smoke
testing:

```bash
docker build -t wudup:local .
```

## Start The WebUI

WUD 9 requires an administrator and authenticated API requests, even on the
private Compose network. Before starting a generated or copied configuration,
set WUD's bootstrap password and configure WUDup's outbound credentials and any
secret-file mount as described in
[WUD API authentication](wiki/webui-container.md#wud-api-authentication).
The bootstrap password is not automatically passed to WUDup's API client.

If the `wudup` CLI is available, generate first-run WebUI config:

```bash
wudup init --profile webui --stack-root /srv/docker --non-interactive
docker compose --env-file "$HOME/.config/wudup/webui.env" \
  -f docs/examples/docker-compose.webui.yml up -d
```

For a socket-proxy deployment, generate hardened config and run Doctor before
starting the service:

```bash
wudup init --profile hardened --stack-root /srv/docker --non-interactive
docker compose --env-file "$HOME/.config/wudup/hardened.env" \
  -f docs/examples/docker-compose.hardened.yml \
  -f "$HOME/.config/wudup/docker-compose.hardened.override.yml" \
  run --rm wudup doctor
docker compose --env-file "$HOME/.config/wudup/hardened.env" \
  -f docs/examples/docker-compose.hardened.yml \
  -f "$HOME/.config/wudup/docker-compose.hardened.override.yml" \
  up -d
```

Without the CLI, copy the env example, review `HOST_DOCKER_BASE` and browser
exposure settings, then start the WebUI:

```bash
WEBUI_ENV="$HOME/.config/wudup/webui.env"
mkdir -p "$HOME/.config/wudup"
test -f "$WEBUI_ENV" || cp docs/examples/webui.env.example "$WEBUI_ENV"
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml up -d
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml logs wudup
```

Open the printed `/#/setup?claim=...` link, create the first admin username and
a password with at least 12 characters, then sign in at
`http://127.0.0.1:7417`. The example binds browser access to loopback by
default.

See [WebUI Container](wiki/webui-container.md) for WUD API/file pending modes,
login recovery, SQLite persistence, LAN or reverse-proxy exposure, candidate
security scans, and browser mutation mode.

## Doctor

Run `doctor` after changing container mounts, Docker access, stack paths, script
sync settings, or helper environment variables:

```bash
WEBUI_ENV="${WEBUI_ENV:-$HOME/.config/wudup/webui.env}"
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml run --rm wudup doctor
```

Doctor is read-only except for short-lived permission probe files that it
creates and removes in writable runtime directories. It checks Docker CLI access,
Compose rendering, `DOCKER_BASE`, `HOST_DOCKER_BASE`, `WUD_OUT_FILE`,
`WUD_LOG_DIR`, packaged WUD scripts, managed script-sync destinations, and
common helper-only path mistakes.

The authenticated WebUI Doctor page runs the same deployment checks from the
browser and adds WebUI database, auth, host/origin, secure-cookie, static asset,
and mutation-gate checks.

## Safety Defaults

- The WebUI starts read-only. Browser-initiated Docker mutations, candidate scan
  refresh jobs, and Settings container restart require
  `WUD_WEB_MUTATIONS_ENABLED=true`.
- `updates --dry-run` and `docker-update-from-wud --dry-run` must not pull
  images, recreate containers, remove WUD lines, or otherwise mutate host state.
- Mutating Docker operations require interactive confirmation or `--yes`.
- Mounting `/var/run/docker.sock` gives a container root-equivalent control over
  the host Docker daemon. Use trusted images and keep mounts scoped.
- Prefer the hardened Compose example when you want WUDup to access Docker
  through a socket proxy sidecar.
- Secrets such as GitHub tokens, Discord webhooks, and WUD API credentials should
  come from environment variables, `_FILE` variables, WebUI-managed settings, or
  host-local secret stores.

## Maintenance

For container-first deployments, pull the new image and recreate WUDup so
startup sync refreshes the managed WUD script volume:

```bash
WEBUI_ENV="${WEBUI_ENV:-$HOME/.config/wudup/webui.env}"
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml pull wudup
docker compose --env-file "$WEBUI_ENV" -f docs/examples/docker-compose.webui.yml up -d --force-recreate wudup
```

For local image development and host installs, see
[Command Runner And Host Install](COMMAND_RUNNER.md). For every supported
environment variable, see [Configuration](CONFIGURATION.md).
