# Release-Note Notifications

WUDup can post release information to Discord from the WebUI after WUD API
polling sees pending updates, after you preview selected pending updates, or
after a successful apply run. Legacy WUD shell helpers remain available for
existing callback setups. GitHub token values must come from the WUDup/WebUI
runtime environment, the WUD container environment for shell callbacks, or
another host-local secret store. Discord webhooks can come from those same
places or from the WebUI-managed webhook field.

## WebUI Workflow

Release-note notifications default disabled. Enable them from Settings, or set
`WUD_RELEASE_NOTES_ENABLED=true` in the WebUI runtime environment to force the
value and make the Settings toggle read-only. Sending notifications is a WebUI
mutation, so the server must also run with `WUD_WEB_MUTATIONS_ENABLED=true`.

Configure a Discord webhook from Settings, or set `DISCORD_WEBHOOK` in the
WUDup runtime environment. Environment webhooks override and disable the
WebUI-managed webhook field.

The Settings test-webhook action sends a representative summary notification
through the same formatter used for real notifications. Use a private Discord
test channel to verify the rendered message before enabling automatic delivery.
Confirm that the resolved release appears beside mutable `latest`, the Jellyfin
row is identified as an LSIO image update, Markdown links remain clickable
without expanding into separate link-preview cards, and the rows remain readable
in both desktop and narrow/mobile Discord clients.

WUDup polls WUD's API for pending updates and builds Discord payloads in Python
from the shared pending-line representation. It previews the notification
without the webhook URL, posts a categorized summary, and splits it only when
it reaches Discord's message limit. Send history ensures duplicates follow
WUDup's resend policy. The summary's categories and reason labels come from WUD
and release-note metadata; WUDup does not generate or summarize prose with AI.
Use the delivery mode setting to choose API polling on detection or on-demand
sends from preview/apply flows.

Release-note prioritization starts only after WUD reports an actionable update.
It does not discover candidates independently or change WUD's watch behavior.
Existing Docker Hub deployments should set
`WUD_REGISTRY_HUB_PUBLIC_WATCHDIGEST=true`, as the current Compose examples do,
so WUD can report same-tag digest changes.

If a legacy shell release-note callback is also configured, Discord can receive
duplicate notifications for the same WUD update. Keep only one notification path
enabled unless duplicate posts are intentional.

Example summary notification:

```text
🧾 WUDup batch — 4 updates found

🛡️ Critical/High security
• home/home-assistant `2026.5.1` → `2026.5.3` — High security update (GHSA-AAAA-BBBB-CCCC) — [GHSA-AAAA-BBBB-CCCC](https://example.invalid/advisory)

⚠️ Needs review
• media/qbittorrent `5.1.4` → `5.2.2` — release notes unavailable

🟡 Worth noting
• media/radarr `5.21.1` → `5.22.0` — minor update with release notes — [release](https://example.invalid/release)

🟢 Routine
• home/home-assistant `2026.5.1` → `2026.5.3` — patch update with release notes — [release](https://example.invalid/release)

Open WUDup for full notes, digests, and apply plan.
```

`Critical/High security`, `Needs review`, `Worth noting`, and `Routine` are
assigned by priority-ordered rules. Security evidence takes priority over
breaking and SemVer cues, which remain visible as secondary context.
Available release, upstream, changelog, or project links use compact labels.
Raw digests and full release bodies remain in WUDup details instead of the
summary notification.

## Security Evidence

WUDup scans the resolved release title and body for bounded urgency signals,
including `UPDATE ASAP`, `critical`, `security`, GHSA IDs, CVE IDs, and GitHub
advisory links. A keyword is only a reason to review or fetch structured
advisory data; it never proves that the running image is affected.

Each release has one of three outcomes:

- **Verified Critical/High**: a published, non-withdrawn GitHub advisory has
  Critical or High structured severity; its repository or package exactly
  matches the resolved image/upstream; the current version is in the structured
  vulnerable range; and the exact pending target is at or beyond a structured
  patched version and outside the vulnerable range.
- **Needs review**: release notes signal urgency, but severity, identity,
  current/target version evidence, or advisory lookup is missing or ambiguous.
  Mutable tags such as `latest` and digest-only evidence remain here even when
  the linked advisory is Critical.
- **Ordinary**: the release has no security urgency signal. No advisory request
  is made.

Version proof deliberately accepts only `v?MAJOR.MINOR[.PATCH]`, exact-version
lists, and comma-separated `<`, `<=`, `>`, `>=`, or `=` comparisons.
Prereleases, wildcards, branches, suffixes, malformed values, and compound
ranges are not treated as verified exposure. WUDup extracts at most eight
advisory IDs and resolves at most four advisories per release. If no fetched
advisory independently proves exposure, capped, rate-limited, timed-out, or
failed lookups become `Needs review`. `advisory_lookup_failed`,
`advisory_unresolved`, and `security_backfill_failed` use the 15-minute cache
interval; capped `advisory_lookup_truncated` results retain the normal six-hour
successful-release interval. If one advisory does prove exposure, the result
remains verified and notes that additional lookup was incomplete.

Verified Critical/High items are sent through the configured Discord webhook
on the next scheduler cycle even when delivery mode is `on_demand`, and they
are ordered before ordinary release notifications in `on_detection` mode.
Their stable history key includes the update identity, release, severity,
advisory IDs, current version/digest, and target version/digest. A successful
key does not resend after a cooldown; a materially changed exposure can notify
again, and failed or stale in-progress attempts remain retryable through the
existing notification history.

Snoozing an update affects Pending selection, not verified security delivery.
Release-note notification code never plans, applies, pulls, restarts, or
otherwise installs an update. Review and apply remain explicit manual actions
with the existing confirmation and safety checks.

Message grouping defaults to **Summary**, which sends one categorized batch and
is unrelated to container image digests. WUDup retains the stored value
`digest` for compatibility with existing WebUI settings, so no migration is
required.

Choose **Per container** for one detailed notification per update. Its stored
value remains `per_container`; full verbosity appends the release body and
truncates it to Discord's embed limits.

## Legacy Shell Callback

`/wud/on-update.sh` remains available for existing shell-notification
deployments. It always calls `/wud/append-updates.sh` first. When
`update_available=true`, it also calls:

```bash
/wud/release-notes-to-discord.sh "$IMAGE" "$CONTAINER_NAME" "$CURRENT_TAG"
```

That helper is the single shell release-note router for WUD callbacks. It
requires:

| Variable | Purpose |
|---|---|
| `DISCORD_WEBHOOK` | Discord webhook for release-note embeds. |

It also expects `docker`, `curl`, and `jq` to be available in the runtime
environment.

The helper tries to discover a GitHub repository from the image's
`org.opencontainers.image.source` label. It also handles fully qualified GitHub
Container Registry references that start with `ghcr.io/`, and LinuxServer.io
images through the legacy `linuxserver/docker-<image>` GitHub release fallback.
If no source can be found, it posts a minimal update notice.

## Direct Shell Use

The same canonical helper can post a GitHub release directly:

```bash
/wud/release-notes-to-discord.sh --repo Owner/Repo --tag latest --webhook "$DISCORD_WEBHOOK"
```

It also supports LinuxServer.io releases that need an upstream project lookup:

```bash
/wud/release-notes-to-discord.sh --provider lsio --lsio linuxserver/docker-radarr --upstream Radarr/Radarr --webhook "$DISCORD_WEBHOOK"
```

Common variables:

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_WEBHOOK` | empty | Discord webhook for release embeds. |
| `ADMIN_WEBHOOK` | selected release webhook | Webhook for missing upstream mapping alerts. |
| `GITHUB_TOKEN` | empty | Optional token for GitHub API rate limits. |
| `MAX_COMMITS` | `3` | Maximum representative PRs or commits to include. |
| `COLOR_HEX` | `0x57F287` | Discord embed color. |
| `UPSTREAM_MAP` | `/wud/upstreams.txt` | LinuxServer.io image to upstream repository map used by explicit LSIO mode and `tag-manager.sh`. |
| `RELEASE_EMBED` | `/wud/github-release-embed.sh` | Compatibility hook used by `tag-manager.sh`. |
| `LOG_DIR` | `/out` | Compatibility log directory used by `tag-manager.sh`. |

The default `github` provider fetches GitHub Releases for `--repo Owner/Repo`.
The legacy provider name `generic` is still accepted as an alias.

Compatibility entrypoints remain available for existing WUD configurations:

```bash
/wud/github-release-embed.sh --repo Owner/Repo --webhook "$DISCORD_WEBHOOK"
/wud/tag-manager.sh
```

New WebUI-focused configurations should not use these shell notification
wrappers. Keep them only for existing WUD callback setups that intentionally
send release notes outside the WebUI.

WUDup polls WUD's API directly for WebUI release-note notifications.
Once WUD API metadata is healthy, set `WUDUP_LEGACY_SCRIPTS=false`, then remove
WUD command triggers that call `/wud/append-updates.sh`, `/wud/on-update.sh`, or
`/wud/tag-manager.sh` before recreating the stack.

## LinuxServer.io Mapping

For default WUD callbacks, LinuxServer.io images keep the legacy behavior:
`linuxserver/xyz` resolves to the GitHub repository
`linuxserver/docker-xyz`.

For explicit LSIO mode or existing `tag-manager.sh` configurations,
`linuxserver/docker-xyz` maps to an upstream `Owner/Repo` entry in
`upstreams.txt`. Missing mappings are sent to `ADMIN_WEBHOOK` and the embed is
skipped.

For containers without a separate upstream release source, map the image
repository to itself to use only its GitHub image releases. Socket-proxy is
configured this way, including when pulled from GHCR:

```text
# Image-only releases; preserve this manual override during map refreshes.
linuxserver/docker-socket-proxy: linuxserver/docker-socket-proxy
```

To enable another container, add a sorted entry in `wud/upstreams.txt` with the
same `linuxserver/docker-<name>` repository on both sides. Keep an explanatory
comment immediately above it so the refresh script preserves the override.
The WebUI and legacy callbacks skip upstream enrichment for these entries.

The LinuxServer.io release is authoritative for LSIO image updates. LSIO-only
image updates and rebuilds stop after that release is resolved and do not
require an upstream release match. Confirmed upstream application updates can
include an upstream release as optional enrichment; a missing match does not
downgrade the LSIO release-note status.

The WebUI also treats LinuxServer.io `version-*` tags, including branch-specific
tags such as `libtorrentv1-version-*`, as upstream-tracking aliases that still
receive LinuxServer.io image rebuilds.

## WebUI Release Links

The WebUI uses a separate Python service for structured release-note metadata.
It does not call the shell helper or parse Discord embeds. The WebUI cache uses
the same `GITHUB_TOKEN` for rate limits and can use `WUD_WEB_UPSTREAM_MAP` to
point at a custom LinuxServer.io upstream map. WebUI-sent Discord notifications
use `DISCORD_WEBHOOK` first. When it is not set, the Settings page can save a
Discord webhook URL in SQLite. Settings responses only report whether that
stored webhook is configured; the raw URL is not returned to the browser.

## Secrets And Logs

Do not commit webhook URLs, GitHub tokens, or private service URLs. Use
environment variables supplied by WUD, Compose secrets, host-local config, or
the WebUI-managed webhook field. Treat the WebUI SQLite database as
secret-bearing if you store a webhook there.

The legacy tag manager redacts webhook values when logging helper commands, and
shared HTTP errors do not print webhook URLs. WebUI Settings responses, audit
records, support bundles, and send errors also redact webhook values. Avoid
copying raw environment dumps or SQLite rows into issues or pull requests.
