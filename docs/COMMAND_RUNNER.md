# Command Runner

The WebUI container is recommended for new deployments. Use this path when you
want short-lived helper commands against a `WUD_OUT_FILE`/`images.todo` file.

The command runner reads `WUD_OUT_FILE`/`images.todo` only. WUD API pending
sources are a WebUI feature. WUDup no longer ships the WUD callback that wrote
`images.todo`, so this file-based path needs an external writer and is
deprecated.

## Docker Script Runner

The repository example is
[`docs/examples/docker-compose.example.yml`](examples/docker-compose.example.yml).
Run it from the repository root:

```bash
docker compose -f docs/examples/docker-compose.example.yml run --rm wudup doctor
docker compose -f docs/examples/docker-compose.example.yml run --rm wudup docker-update-from-wud --dry-run
```

Apply every pending entry:

```bash
docker compose -f docs/examples/docker-compose.example.yml run --rm wudup docker-update-from-wud --yes
```

Tag rewrites are explicit opt-in:

```bash
docker compose -f docs/examples/docker-compose.example.yml run --rm wudup docker-update-from-wud --yes --allow-tag-updates
```

To write approved tag updates as digest-pinned Compose references, set
`WUD_DIGEST_PIN_UPDATES=true` in the helper environment. The updater resolves
the planned tag digest during dry run and applies `repo/app@sha256:<digest>`
only after pull verification succeeds.

Correct a bad WUD-proposed tag for one run with the original WUD file line
number:

```bash
docker compose -f docs/examples/docker-compose.example.yml run --rm wudup docker-update-from-wud --yes --allow-tag-updates --tag-override 1=5.2.0
```

Reject a WUD-proposed tag durably by excluding the original WUD file line. The
updater writes WUD's native `wud.tag.exclude` label to the matching Compose
service, stores the managed exact-tag rule in SQLite, and removes the WUD line
after the label is written:

```bash
docker compose -f docs/examples/docker-compose.example.yml run --rm wudup docker-update-from-wud --yes --exclude-tag-lines 1
```

Add `--recreate-excluded-services` when you want Compose to recreate affected
services immediately so WUD sees the new labels before its next scan. Services
that were stopped are recreated without being started, as in a normal update.

Automatic Compose tag rewrites and exclusion labels only support direct service
`image:` values; image values provided through interpolation or inherited YAML
snippets are left pending for manual review. An existing `wud.tag.exclude`
label that uses a Compose variable such as `${APP_TAG_EXCLUDE}` is not
rewritten either; add the tag to that label or variable by hand.

By default, the updater recreates only the Compose service that owns the matched
image. For services whose update should restart the whole Compose project, add
this label to the matched service:

```yaml
services:
  vpn:
    image: example/vpn:latest
    labels:
      - WUD-UPDATER-RECREATE-STACK=true
```

When the matched running service container has
`WUD-UPDATER-RECREATE-STACK=true`, `docker-update-from-wud` uses stack-level
pull/recreate behavior for that Compose project.

## Helper Mounts

The script runner example mounts:

| Mount | Purpose |
|---|---|
| `/var/run/docker.sock:/var/run/docker.sock` | Lets the helper inspect, pull, and recreate host Docker workloads. |
| `${HOST_DOCKER_BASE:-/srv/docker}:${HOST_DOCKER_BASE:-/srv/docker}` | Makes host Compose stacks visible inside the helper at the same absolute path the Docker daemon uses. |
| `./logs:/logs` | Stores updater logs outside the Docker stack root. |
| `wud-out:/out` | Shared WUD todo-file output volume. |

Set `DOCKER_BASE` to the path that contains the Compose projects. For
containerized updater runs, mount that path at the same absolute location inside
the helper; otherwise relative Compose bind mounts such as `./config:/config`
can resolve to helper-only paths that the host Docker daemon cannot create. Set
`WUD_OUT_FILE` to the todo file shared with WUD and `WUD_LOG_DIR` to the mounted
log directory used by the updater.

Compose discovery searches every directory by default. To exclude archived
stacks without moving `DOCKER_BASE`, set `WUD_COMPOSE_IGNORE_PATHS` to a
comma-separated list of relative directory names or paths, such as
`old,archive/disabled`. Set it to an empty value to disable archive ignores.

Existing deployments that mount stacks at a helper-only path can keep that
layout only if the daemon-visible host root is also readable inside the helper.
For example, with `/srv/docker:/host/docker`, either switch to
`/srv/docker:/srv/docker`, or add a second `/srv/docker:/srv/docker` mount, set
`DOCKER_BASE=/host/docker`, and set `HOST_DOCKER_BASE=/srv/docker`.

The updater passes the mapped stack path to Compose as `--project-directory`.
Relative bind mounts, `.env`, `env_file`, build contexts, and similar
project-relative files must exist under `HOST_DOCKER_BASE` and be readable from
inside the helper.

## Init Wizard

`wudup init` generates local first-run configuration without creating a separate
runtime config model. It writes env files and optional Compose overrides that
use the same variables documented in [Configuration](CONFIGURATION.md), refuses
to overwrite existing files unless `--backup-existing` is set, and keeps
`WUD_WEB_MUTATIONS_ENABLED=false` unless `--enable-web-mutations` is supplied.

Helper-only container setup generates a Compose override by default:

```bash
wudup init --profile helper --stack-root /srv/docker --non-interactive
docker compose --env-file "$HOME/.config/wudup/helper.env" \
  -f docs/examples/docker-compose.example.yml \
  -f "$HOME/.config/wudup/docker-compose.helper.override.yml" \
  run --rm wudup doctor
```

## Local Image Development

For local image development and smoke tests, use
[`docs/examples/docker-compose.build.yml`](examples/docker-compose.build.yml).
That file keeps the repository-local `build` stanza separate from deployment
examples:

```bash
docker compose -f docs/examples/docker-compose.build.yml build wudup
docker compose -f docs/examples/docker-compose.build.yml up -d --force-recreate wudup
```
