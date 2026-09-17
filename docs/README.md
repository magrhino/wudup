# Documentation

This is the full documentation index for WUDup. The root README stays
short; detailed setup and behavior notes live here.

## Start Here

| Topic | Where |
|---|---|
| Project overview and quick commands | [../README.md](../README.md) |
| Public static WebUI demo | [magrhino.github.io/wudup](https://magrhino.github.io/wudup/) and [demo notes](wiki/demo.md) |
| Security policy and private vulnerability reporting | [../SECURITY.md](../SECURITY.md) |
| Deployment start guide | [DEPLOYMENT.md](DEPLOYMENT.md) |
| WebUI container guide | [wiki/webui-container.md](wiki/webui-container.md) |
| Command runner and host install | [COMMAND_RUNNER.md](COMMAND_RUNNER.md) |
| Configuration reference | [CONFIGURATION.md](CONFIGURATION.md) |
| Generated host/helper configuration | [COMMAND_RUNNER.md#init-wizard](COMMAND_RUNNER.md#init-wizard) |
| Development, CI, and release automation | [DEVELOPMENT.md](DEVELOPMENT.md) |
| SonarQube installation findings and gate policy | [SONAR_TRIAGE.md](SONAR_TRIAGE.md) |
| Docker Compose example | [examples/docker-compose.example.yml](examples/docker-compose.example.yml) |
| Long-running WebUI Docker Compose example | [examples/docker-compose.webui.yml](examples/docker-compose.webui.yml) |
| Long-running WebUI env example | [examples/webui.env.example](examples/webui.env.example) |
| Hardened Docker Compose example | [examples/docker-compose.hardened.yml](examples/docker-compose.hardened.yml) |
| Experimental TrueNAS Docker Compose example | [examples/docker-compose.truenas.yml](examples/docker-compose.truenas.yml) |
| Local Docker Compose build artifact | [examples/docker-compose.build.yml](examples/docker-compose.build.yml) |
| Environment template | [examples/template.env](examples/template.env) |
| Changelog | [../CHANGELOG.md](../CHANGELOG.md) |

## Feature Explainers

| Topic | Where |
|---|---|
| WUD callback flow, todo-file format, digest updates, and tag updates | [wiki/wud-update-flow.md](wiki/wud-update-flow.md) |
| Digest verification behavior and trust policy | [wiki/digest-verification.md](wiki/digest-verification.md) |
| Candidate security scanning approach | [wiki/security-scanning-signals.md](wiki/security-scanning-signals.md) |
| Public static WebUI demo shape | [wiki/demo.md](wiki/demo.md) |
| Managed WUD script volume sync behavior | [wiki/container-script-sync.md](wiki/container-script-sync.md) |
| Long-running WebUI container setup, login, pending sources, exposure, and mutation notes | [wiki/webui-container.md](wiki/webui-container.md) |
| GitHub and Discord release-note notifications | [wiki/release-note-notifications.md](wiki/release-note-notifications.md) |

## Repository Areas

| Area | Purpose |
|---|---|
| `bin/` | Host-facing wrapper commands. |
| `src/wudup/` | Python updater package and CLI entrypoints. |
| `wud/` | Scripts mounted into the WUD container. |
| `docs/examples/` | Copyable deployment examples. |
| `tests/` | Shell and Python validation using fakes and temp directories. |
