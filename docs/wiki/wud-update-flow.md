# WUD Update Flow

WUDup is built around line-oriented pending entries. The legacy CLI reads those
entries from `images.todo`. The WebUI never reads that file; it renders WUD API
container metadata into the same pending-line format before planning, applying,
or previewing release notes.

## File Mode Flow

This is the deprecated file-mode flow used by `docker-update-from-wud`. WUDup no
longer ships the WUD callback (`append-updates.sh`) that wrote `images.todo`, so
file mode needs an external writer that produces the
[todo file format](#todo-file-format).

1. Something outside WUDup appends pending lines to `${WUD_OUT_FILE}`.
2. `docker-update-from-wud` discovers Compose projects under `DOCKER_BASE`,
   pulls matching images, recreates matching services or stacks, waits for
   health, and removes successful entries.

The default output path is `/out/images.todo` inside the WUDup container.

The WebUI never reads the todo file. It derives the same pending-line format
from WUD `/api/containers` over the private Compose app network, and each apply
job runs the updater against a private copy of the selected lines. When WUD
reports a failed update check for a container and WUDup has no last successful
result for it, the update's status is reported as unknown instead of being
recovered from `WUD_OUT_FILE`.

The **Rescan WUD** button requests a full scan through `POST /api/containers/watch`.
WUD runs all configured watchers, discovering eligible containers even when its
stored container list is empty or contains obsolete IDs. WUD's configured watcher
filters still apply. Selected rescans refresh only the selected WUD container IDs.
WUD's configured triggers may run during the scan. Full-scan audit counts represent one global watch request, rather than
the number of containers discovered. Registry errors remain visible as a partial
result, and existing rate-limit cooldowns can temporarily block a full scan.

WUDup polls WUD's API directly for WebUI release-note notifications.

## Review updates in the WebUI

On Pending, select stack updates, then choose **Review selected**. The review
bar stays available while scrolling and shows the eligible update count. Each
stack also has a **Review … plan** action. Review changes nothing; **Apply** stays
inside the plan dialog, subject to read-only mode, readiness and fresh-plan checks.
The dialog scrolls its contents separately from Close and Apply. The plan summary
shows the proposed image transition for one service, or service and stack totals
for larger selections. Expand **Inspect image change** (or **Inspect all service
changes**) to compare exact references grouped by stack.

The review also summarizes **Operational impact**, **Supporting evidence**, and
**Unresolved before apply**. Impact follows the planned pull, pause/stop,
recreate, dependency, orphan-removal, and health-check steps, including
stack-wide recreation. Supporting evidence distinguishes the planned digest
from a running-image verification result. Release information is matched to
the planned version, and scan comparisons are shown only for a matching image
digest. Missing evidence, breaking-change notices, skipped updates, and plan
issues retain their reasons. Longer selections expand within each section.
These summaries do not change apply eligibility: release and scan evidence
remains advisory, and existing readiness and fresh-plan checks still control
Apply.

The Dashboard leads with pending work by stack, distinguishing version decisions
and targets needing attention from stopped, unverified, and snoozed work. These
are review cues, not permission to apply. Recent update results reuse History's
image and health evidence; routine settings and login events remain in History.
System health and management links follow the update work. Failed queue reads
remain unavailable or explicitly show the last loaded queue, never a clear queue.

Run detail leads with the same service/stack scope and separate image and health
evidence. History reuses a compact result with relative time; hover the time for
the exact timestamp, or open **Exact timestamps** in run detail with touch or
keyboard to see the recorded start and finish times. Failed, skipped, and missing checks remain explicit, and
command success alone does not imply verification. Dry runs are labeled as
unapplied plans. Preserved stopped services show skipped health checks.
History includes recorded verification for runs with up to 200 update records.
For larger runs it shows the exact record count and asks you to open the run for
verification, without claiming that a partial set was verified. Full run detail
retains all records. These reads do not initiate new Docker or health checks.

**Queue details and actions** explains count scopes and contains WUD rescans.
The WebUI does not remove pending entries. Unmatched entries stay listed with
diagnostics until WUD stops reporting them.
Search narrows the visible queue; hidden selections still
participate in review. **Select all** replaces the selection with the currently
visible selectable updates; **Clear selection** clears hidden selections too.
Stopped/unverified services are excluded from bulk selection. Stale metadata can
still block a selected update from review. Counts can overlap or use different
units, so detected, pending and selectable counts are not additive.

Stack summaries prioritize services, current and target versions, operational
impact and actionable risks. Details retains full image references, routine
metadata and the full evidence.
Release advisory cues describe release security evidence; candidate scan cues
describe image scan results. Neither is a guarantee that an update is safe.

**Refresh current view status** reads current state. **Rescan WUD** asks WUD to
check registries. **Scan candidate images**, beside **Select all**, scans every
candidate image in the queue and shows progress and failures next to the button.
When no candidate has been scanned, the queue says so once there instead of on
every row.
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
interpolation, inherited image values, and WUD labels listed more than once on
a service (Compose uses only the last one) fail closed.

## Locking

`docker-update-from-wud` takes a directory lock at `${WUD_OUT_FILE}.lock` while
it rewrites the file and waits up to `WUD_LOCK_TIMEOUT` seconds, defaulting to
`30`. External writers should use the same lock. Rewrites preserve the file's
owner and mode unless `OUT_UID` and `OUT_GID` are set.

## Applying Updates

Review pending entries without mutation:

```bash
docker-update-from-wud --dry-run
```

Apply all pending entries:

```bash
docker-update-from-wud --yes
```

Apply tag updates explicitly:

```bash
docker-update-from-wud --yes --allow-tag-updates
```

Apply approved tag updates as digest-pinned Compose references:

```bash
WUD_DIGEST_PIN_UPDATES=true docker-update-from-wud --yes --allow-tag-updates
```

By default, matched Compose services are recreated without their dependencies.
If updating a service should recreate the whole Compose project, label that
service with `WUD-UPDATER-RECREATE-STACK=true`. When a matched running service
container has that label, the updater uses stack-level pull/recreate behavior
instead of service-scoped stop/up: it stops the project services and runs
`docker compose up -d --remove-orphans` without tearing down Compose networks.

The updater runs Compose with the single discovered Compose file for each
project. If any container in the project was started with other Compose files,
such as an automatically loaded `docker-compose.override.yml`, extra `-f`
files, or only a different file (or the same file reached through a different
path) that uses the same project name, the updater and WebUI retag refuse to
update or recreate that project instead of dropping those settings. Merge the
extra settings into the discovered Compose file and recreate the stack from it,
fix a mismatched `DOCKER_BASE` or `HOST_DOCKER_BASE` path, or update that
stack manually. Tag exclusions that would need such a recreate are refused
before the `wud.tag.exclude` label is written. When another discovered stack
shares the project name while this stack still runs from its own file, the
update goes ahead without `--remove-orphans`, so the other stack's containers
are kept; give each stack a unique project name to avoid this.

When you exclude a tag, the updater writes WUD's native
`wud.tag.exclude` label into the matched Compose service definition. If every
service using the same image repository can be updated cleanly, the exclusion is
applied repo-wide; otherwise it falls back to the selected service. Existing
user-authored exclude regexes are preserved and the updater stores managed exact
tag exclusions in SQLite. An existing exclude label that uses a Compose variable
is left unchanged and the line stays pending for a manual edit. Add
`--recreate-excluded-services` to recreate affected services immediately so WUD
sees the new container labels before its next scan; stopped services are
recreated without being started. A running service that uses the network of a
stopped `network_mode: service:...` provider is not recreated, because it could
not start again; the line is marked failed so you can recreate it by hand.

Override a WUD-proposed tag directly:

```bash
docker-update-from-wud --yes --allow-tag-updates --tag-override 1=5.2.0
```

Exclude a WUD-proposed tag directly:

```bash
docker-update-from-wud --yes --exclude-tag-lines 1 --recreate-excluded-services
```

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
