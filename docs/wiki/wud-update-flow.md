# WUD Update Flow

WUDup is built around shared, line-oriented pending entries. The legacy CLI reads
those entries from `images.todo` only. The WebUI can either read that file or
render WUD API container metadata into the same pending-line format before
planning, applying, or previewing release notes.

## Callback Flow

This is the legacy file-mode flow used by `updates` and `docker-update-from-wud`.

1. WUD detects an available image update.
2. WUD calls `/wud/append-updates.sh`.
3. `append-updates.sh` appends or replaces one line in `${WUD_OUT_FILE}`.
4. Legacy `/wud/on-update.sh` remains available when shell release-note
   notifications are intentional; it delegates to `append-updates.sh` first.
5. `updates` displays the file and asks whether to run `docker-update-from-wud`.
6. `docker-update-from-wud` discovers Compose projects under `DOCKER_BASE`,
   pulls matching images, recreates matching services or stacks, waits for
   health, and removes successful entries.

The default WUD output path is `/out/images.todo` inside the WUD container. Host
installs commonly map that to `$HOME/docker/wud/out/images.todo`.

WebUI deployments default to `WUD_PENDING_SOURCE=api`, which skips the callback
file and derives the same pending lines from WUD `/api/containers` over the
private Compose app network. `auto` uses that API when available and falls back
to `WUD_OUT_FILE`; `file` keeps the legacy callback-only behavior.

When legacy scripts remain enabled, API mode may read `WUD_OUT_FILE` as a
read-only cold-start recovery hint if WUD returns a degraded container row and
no last-known-good observation exists. Recovery only considers pending lines
with a desired `tag=` or `sha256=` value and requires the registry, current
image, platform, and installed digest or tag to match. Explicit Docker Hub
aliases are equivalent to the default unqualified registry; other registries
must match exactly. This prevents already-applied, metadata-free, and unrelated
lines from being revived. The API remains the active pending source, and an
explicit selected rescan targets only the recovered WUD container IDs. Recovery
never triggers a global WUD watch or changes the file.

The **Rescan WUD** button requests a full scan through `POST /api/containers/watch`.
WUD runs all configured watchers, discovering eligible containers even when its
stored container list is empty or contains obsolete IDs. WUD's configured watcher
filters still apply. Selected rescans refresh only the selected WUD container IDs.
WUDup leaves the pending file unchanged; WUD's configured triggers may run during
the scan. Full-scan audit counts represent one global watch request, rather than
the number of containers discovered. Registry errors remain visible as a partial
result, and existing rate-limit cooldowns can temporarily block a full scan.

WUDup polls WUD's API directly for WebUI release-note notifications. Set
`WUDUP_LEGACY_SCRIPTS=false` only after removing legacy WUD command triggers and
recreating the stack. In that mode, managed script sync installs no WUD command
scripts.

## Review updates in the WebUI

On Pending, select stack updates, then choose **Review selected**. The review
bar stays available while scrolling and shows the eligible update count. Each
stack also has a **Review … plan** action. Review changes nothing; **Apply** stays
inside the plan dialog, subject to read-only mode, readiness and fresh-plan checks.
The dialog scrolls its contents separately from Close and Apply. The plan summary
shows the proposed image transition for one service, or service and stack totals
for larger selections. Expand **Inspect image change** (or **Inspect all service
changes**) to compare exact references grouped by stack.

Run detail leads with the same service/stack scope and separate image and health
evidence. History reuses a compact result with relative time; hover the time for
the exact timestamp, or open **Exact timestamps** in run detail with touch or
keyboard to see the recorded start and finish times. Failed, skipped, and missing checks remain explicit, and
command success alone does not imply verification. Dry runs are labeled as
unapplied plans. The run list includes the same recorded verification as run
detail, without initiating new Docker or health checks.

**Queue details and actions** explains count scopes and contains rescans and
selected-entry removal. Search narrows the visible queue; hidden selections still
participate in review. **Select all** replaces the selection with the currently
visible selectable updates; **Clear selection** clears hidden selections too.
Stopped/unverified services are excluded from bulk selection. Stale metadata can
still block a selected update from review. Counts can overlap or use different
units, so detected, pending and selectable counts are not additive.

Stack summaries prioritize services, current and target images, operational impact
and actionable risks. Details retains routine metadata and the full evidence.
Release advisory cues describe release security evidence; candidate scan cues
describe image scan results. Neither is a guarantee that an update is safe.

**Refresh current view status** reads current state. **Rescan WUD** asks WUD to
check registries; **Refresh security scans** requests candidate-image scans.
After applying, use **History** to inspect the result.

## Todo File Format

Blank lines and lines beginning with `#` are ignored. Each actionable line starts
with an image or container target:

```text
repo/app:latest
repo/app@sha256:abc123
repo/app:1.0 tag=2.0
repo/app:1.0 tag=2.0 platform=linux/amd64 sha256=abc123
```

Digest-pinned references use the `image@sha256:...` form. The updater validates
that the pulled image resolves to the requested digest before treating the update
as successful. See [Digest Verification](digest-verification.md) for registry
trust behavior and live verification notes.

Tag updates use a `tag=<new-tag>` token after a tagged source image. They stay
pending unless the updater is run with `--allow-tag-updates`.

Optional trailing metadata can include `platform=<os>/<arch>[/variant]` and
`sha256=<digest>`. WUDup preserves these tokens when rewriting the todo file.
The security scan prototype uses them to resolve a candidate image to an exact
platform manifest digest before scanning. Unknown trailing tokens are preserved
as raw line metadata for compatibility.

Manual tag overrides can be supplied for a single updater run with
`--tag-override LINE=TAG`, where `LINE` is the original WUD file line number.
Overrides require `--allow-tag-updates` and do not rewrite the todo file. Before
rewriting Compose files, the updater validates each final tag with
`docker manifest inspect`.

Compose tag rewrites only update direct `services.<name>.image` scalar values.
Interpolated image values such as `repo/app:${TAG}`, inherited image values, and
ambiguous Compose source layouts fail closed and leave the WUD entry pending for
manual review.

When `WUD_DIGEST_PIN_UPDATES=true`, approved tag updates are planned as
digest-pin rewrites. During dry-run planning, the updater resolves the proposed
tag to a target digest and records planned actions (the planned resolved tag and
planned `wud.tag.include` regex) without mutating Compose or performing pulls.
During the apply/execution phase, the updater temporarily rewrites Compose to
the resolved tag for `docker compose pull`, verifies the pulled local image
against the planned digest, then writes the final image as
`repo/app@sha256:<digest>`. The Compose edit adds
`# wudup.resolved-tag=<tag>` above `image:` and sets `wud.tag.include` to
an exact regex for the resolved tag. Dry-run remains non-mutating; Compose edits
and final digest writes happen only during apply. Lines without a safe resolved
tag, custom compound `wud.tag.include` regexes, YAML anchors/aliases,
interpolation, and inherited image values fail closed.

## Appends And Locking

`append-updates.sh` writes only when WUD sets `update_available=true`. It uses a
directory lock at `${WUD_OUT_FILE}.lock` and waits up to `WUD_LOCK_TIMEOUT`
seconds, defaulting to `30`.

When creating the todo file for the first time, the script uses mode `0660`. If
the file already exists, rewrites preserve its owner and mode unless `OUT_UID`
and `OUT_GID` are set.

For tag updates, WUD values such as `update_kind_remote_value` or `result_tag`
are converted into `tag=<new-tag>` when the tag value is safe.
When WUD also supplies a valid candidate digest and platform fields, the script
adds `sha256=<digest>` and `platform=<os>/<arch>[/variant]` metadata.

## Applying Updates

Review pending entries without mutation:

```bash
updates --dry-run
docker-update-from-wud --dry-run
```

Apply all pending entries:

```bash
updates --yes
```

Apply tag updates explicitly:

```bash
updates --yes --allow-tag-updates
```

Apply approved tag updates as digest-pinned Compose references:

```bash
WUD_DIGEST_PIN_UPDATES=true updates --yes --allow-tag-updates
```

By default, matched Compose services are recreated without their dependencies.
If updating a service should recreate the whole Compose project, label that
service with `WUD-UPDATER-RECREATE-STACK=true`. When a matched running service
container has that label, the updater uses stack-level pull/recreate behavior
instead of service-scoped stop/up: it stops the project services and runs
`docker compose up -d --remove-orphans` without tearing down Compose networks.

Interactive `updates` runs show selected tag update entries and ask whether to
apply them as shown, skip them, change the tag, or exclude the proposed exact
tag before handing off to `docker-update-from-wud`.

When you choose to exclude a tag, the updater writes WUD's native
`wud.tag.exclude` label into the matched Compose service definition. If every
service using the same image repository can be updated cleanly, the exclusion is
applied repo-wide; otherwise it falls back to the selected service. Existing
user-authored exclude regexes are preserved and the updater stores managed exact
tag exclusions in SQLite. You can also let the wrapper recreate affected
services immediately so WUD sees the new container labels before its next scan.

Override a WUD-proposed tag directly:

```bash
docker-update-from-wud --yes --allow-tag-updates --tag-override 1=5.2.0
```

Exclude a WUD-proposed tag directly:

```bash
docker-update-from-wud --yes --exclude-tag-lines 1 --recreate-excluded-services
```

Interactive `updates` runs can apply all entries, select numbered entries,
exclude numbered entries, or skip. Unselected entries stay pending unless you
choose to remove them before running the selected updates.

## Planning a Rollback

For a completed updater run, open **History**, select the run, and choose
**Check rollback plan**. The check is read-only: it does not pull or tag images,
edit Compose, restart services, change the WUD output file, or write audit data.

A service is marked ready only when all of the following still hold:

- no later successful updater run superseded the recorded event;
- the current Compose service still uses the recorded target image;
- every running replica uses the recorded new image ID; and
- the exact previous digest-pinned image still resolves locally to the recorded
  previous image ID.

Ready entries show the exact digest-pinned rollback target and a conservative
recovery sequence. Blocked entries retain the recorded evidence and explain
what could not be proven. WUDup does not pull a missing previous image or
generate host-specific rollback commands; recover or verify that image manually
before changing Compose.
