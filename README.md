# WUDup

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/magrhino/wudup/badge)](https://scorecard.dev/viewer/?uri=github.com/magrhino/wudup)

<p align="center">
  <img src="docs/assets/wudup-mark.png" alt="WUDup logo" width="128">
</p>

WUDup turns image update notices from What's Up Docker (WUD) into a
reviewable Docker Compose update workflow. The recommended deployment is the
long-running WebUI container, which provides a local browser dashboard,
read-only safety defaults, a pending-update review queue, Doctor checks, run
history, logs, diagnostics, and optional scheduled automatic updates.

**Project status:** Public beta. WUDup is maintainer-tested with Docker Compose,
and early users are welcome. Start in read-only mode, try the
[public demo](https://magrhino.github.io/wudup/), and use
[GitHub Discussions](https://github.com/magrhino/wudup/discussions) for setup
help or feedback.

![WUDup dashboard](docs/assets/wudup-dashboard.jpg)

## Why WUDup?

WUD tells you that an image changed; WUDup queues the update for review before
your Compose services change. Check readiness and apply updates manually, or
configure policies to apply eligible updates automatically on your schedule.
WUDup records each run in History and provides diagnostics when something needs
attention.

## Web Deployment

Try the [public, fixture-backed demo](https://magrhino.github.io/wudup/).

New WebUI deployments read available updates from WUD's API over a private
Compose network and queue them on the Pending page. The callback todo file
remains an explicit fallback/import source, and the host CLI stays file-based.

```text
WUD detects an image update
-> WUDup queues it on the Pending page
-> review and apply it manually, or let an auto-update policy handle it
-> check the result in History
```

The WebUI deployment starts read-only. Manual and automatic updates stay
disabled unless `WUD_WEB_MUTATIONS_ENABLED=true` is set intentionally.

### Start The WebUI

If the `wudup` CLI is available, generate first-run hardened WebUI config
instead of downloading the env template:

```bash
wudup init --profile hardened --stack-root /srv/docker --non-interactive
```

That writes a hardened env file and Compose override under
`$HOME/.config/wudup/`. Use them with the hardened Compose example before
starting the service.

To start without the CLI, download the published hardened WebUI Compose example
plus an env template in your deployment directory:

```bash
curl -fsSLo docker-compose.yml \
  https://raw.githubusercontent.com/magrhino/wudup/main/docs/examples/docker-compose.hardened.yml
curl -fsSLo .env \
  https://raw.githubusercontent.com/magrhino/wudup/main/docs/examples/webui.env.example
```

Review `.env` before starting: set `HOST_DOCKER_BASE` to your Compose stack
root, then keep loopback-only browser access or set `WUD_WEB_PUBLIC_ORIGIN` for
LAN or reverse-proxy exposure. Start the service, then read the one-time setup
link from the logs:

```bash
docker compose up -d
docker compose logs wudup
```

Open the printed `/#/setup?claim=...` link, create the first admin username and
a password with at least 12 characters, then sign in at
`http://127.0.0.1:7417`. The example binds browser access to loopback by
default.

See the [WebUI container guide](docs/wiki/webui-container.md) for the full
Compose walkthrough, login, admin recovery, LAN or reverse-proxy exposure,
SQLite persistence, managed preferences, and mutation mode.

## Other Deployment Paths

The WebUI container is recommended for new deployments. The non-web paths remain
supported when you want a smaller command-runner workflow or host-installed
commands:

| Path | Use when | Docs |
|---|---|---|
| Docker script runner | You want short-lived `docker compose run` commands for `doctor`, dry runs, and applies without a persistent WebUI. | [Command runner](docs/COMMAND_RUNNER.md) |
| Host install | You want `updates` and `docker-update-from-wud` on the host `PATH` with host-managed WUD script mounts. | [Host install](docs/COMMAND_RUNNER.md#host-install) |

The WebUI/API is the primary supported workflow. The `updates` and
`docker-update-from-wud` CLI paths are retained as legacy file-mode conveniences
for host and helper-container operators; API mode and CLI/WebUI feature parity
are not project goals. New review and interactive features should go to the
WebUI/API first.

## Documentation

| Topic | Where |
|---|---|
| Public WebUI demo | [magrhino.github.io/wudup](https://magrhino.github.io/wudup/) and [demo notes](docs/wiki/demo.md) |
| Deployment start guide | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| WebUI container guide | [docs/wiki/webui-container.md](docs/wiki/webui-container.md) |
| Command runner and host install | [docs/COMMAND_RUNNER.md](docs/COMMAND_RUNNER.md) |
| Configuration reference | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Digest verification and digest-pin updates | [docs/wiki/digest-verification.md](docs/wiki/digest-verification.md) |
| Complete documentation index | [docs/README.md](docs/README.md) |
| Security policy and private vulnerability reporting | [SECURITY.md](SECURITY.md) |
| Release notes | [CHANGELOG.md](CHANGELOG.md) |

## Support the project

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-orange?logo=buy-me-a-coffee)](https://www.buymeacoffee.com/magrhino)

or

BTC: `bc1q3r9g3k8fyzxr29njgfjdqs53z9tuezwuaagx0h`
ETH: `0x118c1b3b927b870a0cf0bd692e06cd769e5af6d9`
