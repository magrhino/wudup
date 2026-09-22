"""Discord notification copy, payload limits/batching, and webhook delivery.

The immutable target record is shared with source selection; mutable scheduler,
reservation, delivery history and audit state remain in web_release_notifications.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import web_wud_api
from .images import image_repo_ref, image_tag
from .web_auth import _redact_sensitive_text
from .web_models import (
    ReleaseNoteInfo,
    ReleaseNoteLink,
    ReleaseNotificationItem,
    ReleaseNotificationTrigger,
    ReleaseSecurityAssessment,
    WebSettings,
)
from .web_settings import effective_release_notification_webhook
from .wud_file import WudTarget, is_digest_target_line

DISCORD_MESSAGE_CONTENT_LIMIT = 2000


DISCORD_EMBED_DESCRIPTION_LIMIT = 4096


DISCORD_DIGEST_ROW_LIMIT = 1500


DISCORD_DIGEST_SUBJECT_LIMIT = 160


DISCORD_DIGEST_VERSION_LIMIT = 128


DISCORD_DIGEST_REASON_LIMIT = 160


DISCORD_WEBHOOK_TIMEOUT_SECONDS = 10.0


DISCORD_WEBHOOK_USER_AGENT = "wudup-webui-release-notifications/1.0"


DISCORD_WEBHOOK_USERNAME = "WUDup Release Notes"


DISCORD_SUPPRESS_EMBEDS_FLAG = 1 << 2


DISCORD_COLOR = 0x57F287


DISCORD_SECURITY_COLOR = 0xED4245


DISCORD_DIGEST_FOOTER = "Open WUDup for full notes, digests, and apply plan."


DISCORD_DIGEST_CATEGORIES = (
    ("security_urgent", "🛡️ Critical/High security"),
    ("needs_review", "⚠️ Needs review"),
    ("worth_noting", "🟡 Worth noting"),
    ("routine", "🟢 Routine"),
)


SEMVER_PARTS_RE = re.compile(
    r"(?<![\dA-Za-z.])v?(\d{1,10})[.](\d{1,10})(?:[.](\d{1,10}))?",
    re.ASCII,
)


@dataclass(frozen=True)
class _NotificationTarget:
    target: WudTarget
    service_key: str = ""
    wud_container_id: str = ""


def _notification_versions(
    target: _NotificationTarget,
    note: ReleaseNoteInfo,
    metadata: web_wud_api.WudApiContainer | None,
) -> tuple[str, str]:
    current_version = str(getattr(metadata, "local_tag", "") or "")
    target_version = str(getattr(metadata, "remote_tag", "") or "")
    digest_update = _is_digest_update(target, metadata)
    if not current_version:
        current_version = image_tag(target.target.first) or target.target.tag_token
    if not target_version:
        target_version = (
            "new digest"
            if digest_update
            else target.target.desired_tag or note.release_tag
        )
    if (
        _should_annotate_latest_release(current_version, target_version)
        and note.release_tag
        and note.release_tag.lower() != "latest"
    ):
        target_version = f"latest (release {note.release_tag})"
    if not current_version:
        current_version = "current image"
    if not target_version:
        target_version = "updated image"
    return current_version, target_version


def _is_digest_update(
    target: _NotificationTarget,
    metadata: web_wud_api.WudApiContainer | None,
) -> bool:
    return (
        str(getattr(metadata, "update_kind", "") or "") == "digest"
        or is_digest_target_line(target.target)
    )


def _should_annotate_latest_release(
    current_version: str,
    target_version: str,
) -> bool:
    return target_version.lower() == "latest" or (
        current_version.lower() == "latest" and target_version == "new digest"
    )


def _notification_digest_reason(
    target: _NotificationTarget,
    note: ReleaseNoteInfo,
    metadata: web_wud_api.WudApiContainer | None,
    *,
    current_version: str,
    target_version: str,
) -> tuple[str, str, str]:
    security_reason = _security_digest_reason(note.security)
    if security_reason is not None:
        return security_reason
    semver_diff = str(getattr(metadata, "semver_diff", "") or "").lower()
    if not semver_diff:
        semver_diff = _semver_diff(current_version, target_version)
    classification = getattr(note, "classification", None)
    change_type = str(getattr(classification, "change_type", "") or "")
    provider_prefix = "LSIO image update: " if note.provider == "lsio" else ""

    if note.breaking:
        return (
            "needs_review",
            "breaking_change",
            f"{provider_prefix}possible breaking change",
        )
    if semver_diff == "major":
        return "needs_review", "major_bump", f"{provider_prefix}major version bump"
    status_reasons = {
        "error": ("release_notes_error", "release-note lookup failed"),
        "unsupported": ("release_notes_unsupported", "release notes unsupported"),
        "missing": ("release_notes_missing", "release notes unavailable"),
        "not_found": ("release_notes_not_found", "matching release not found"),
    }
    if note.status in status_reasons:
        code, label = status_reasons[note.status]
        return "needs_review", code, f"{provider_prefix}{label}"
    lsio_reason = _lsio_digest_reason(note.provider, change_type)
    mutable_latest_reason = _mutable_latest_digest_reason(
        current_version,
        target_version,
        note.provider,
        lsio_reason,
    )
    if mutable_latest_reason is not None:
        return mutable_latest_reason
    if not _has_release_or_changelog_link(note.links):
        return (
            "needs_review",
            "release_link_missing",
            f"{provider_prefix}release or changelog link unavailable",
        )
    if lsio_reason is not None:
        return "worth_noting", *lsio_reason
    if note.provider == "lsio":
        return "worth_noting", "lsio_update", "LSIO image update"
    if semver_diff == "minor":
        return "worth_noting", "minor_bump", "minor update with release notes"
    if semver_diff == "patch":
        return "routine", "patch_bump", "patch update with release notes"
    if _is_digest_update(target, metadata):
        return "routine", "routine_digest", "image digest update"
    return "routine", "routine_update", "update metadata available"


def _security_digest_reason(
    security: ReleaseSecurityAssessment,
) -> tuple[str, str, str] | None:
    if security.outcome == "verified_critical_high":
        advisory_label = ", ".join(security.advisory_ids[:3])
        label = f"{security.severity.title()} security update"
        if advisory_label:
            label = f"{label} ({advisory_label})"
        return "security_urgent", "verified_security", label
    if security.outcome == "needs_review":
        return "needs_review", "security_needs_review", "security update needs review"
    return None


def _mutable_latest_digest_reason(
    current_version: str,
    target_version: str,
    provider: str,
    lsio_reason: tuple[str, str] | None,
) -> tuple[str, str, str] | None:
    normalized_target = target_version.lower()
    if (
        current_version.lower() != "latest"
        and normalized_target != "latest"
        and not normalized_target.startswith("latest (")
    ):
        return None
    if provider != "lsio":
        return "needs_review", "mutable_latest", "mutable latest tag"
    if lsio_reason is None:
        return (
            "needs_review",
            "lsio_latest",
            "LSIO image update via mutable latest",
        )
    code, label = lsio_reason
    return "needs_review", code, f"{label} via mutable latest"


def _lsio_digest_reason(provider: str, change_type: str) -> tuple[str, str] | None:
    if provider != "lsio":
        return None
    return {
        "upstream_update": ("lsio_upstream", "LSIO/upstream release"),
        "image_rebuild": ("lsio_rebuild", "LSIO image rebuild"),
    }.get(change_type)


def _semver_diff(current_version: str, target_version: str) -> str:
    current_match = SEMVER_PARTS_RE.search(current_version)
    target_match = SEMVER_PARTS_RE.search(target_version)
    if current_match is None or target_match is None:
        return ""
    current = tuple(int(part or 0) for part in current_match.groups())
    target = tuple(int(part or 0) for part in target_match.groups())
    if target[0] != current[0]:
        return "major"
    if target[1] != current[1]:
        return "minor"
    if target[2] != current[2]:
        return "patch"
    return ""


def _has_release_or_changelog_link(links: Sequence[ReleaseNoteLink]) -> bool:
    return any(
        link.url
        and (
            "release" in link.kind.lower()
            or "changelog" in link.kind.lower()
            or "release" in link.label.lower()
            or "changelog" in link.label.lower()
        )
        for link in links
    )


def _notification_copy(
    settings: WebSettings,
    target: _NotificationTarget,
    note: ReleaseNoteInfo,
    triggers: Sequence[ReleaseNotificationTrigger],
    *,
    image_repo: str,
    upstream_repo: str,
    update_kind: str = "",
    verbosity: str = "summary",
) -> tuple[str, str]:
    classification = getattr(note, "classification", None)
    change_type = str(getattr(classification, "change_type", "") or "")
    build_suffix = str(
        getattr(getattr(classification, "target", None), "build_suffix", "") or ""
    )
    image_rebuild = change_type == "image_rebuild"
    upstream_update = change_type == "upstream_update"
    repo = image_repo
    tag = note.release_tag or target.target.desired_tag or target.target.tag_token
    title = _notification_title(
        target,
        repo=repo,
        upstream_repo=upstream_repo,
        tag=tag,
        build_suffix=build_suffix,
        image_rebuild=image_rebuild,
        upstream_update=upstream_update,
        update_kind=update_kind,
    )
    lines = _notification_description_lines(
        settings,
        target,
        note,
        triggers,
        repo=repo,
        upstream_repo=upstream_repo,
        tag=tag,
        build_suffix=build_suffix,
        image_rebuild=image_rebuild,
        upstream_update=upstream_update,
    )
    body = str(getattr(note, "body", "") or "").strip()
    if verbosity == "full" and body:
        lines.extend(("", body))
    return title, "\n".join(lines)[:DISCORD_EMBED_DESCRIPTION_LIMIT]


def _notification_title(
    target: _NotificationTarget,
    *,
    repo: str,
    upstream_repo: str,
    tag: str,
    build_suffix: str,
    image_rebuild: bool,
    upstream_update: bool,
    update_kind: str,
) -> str:
    if image_rebuild:
        return _lsio_image_rebuild_title(repo, tag, build_suffix)
    if upstream_update:
        title = _upstream_application_update_title(repo, upstream_repo, tag)
        if title:
            return title
    return (
        f"{_notification_title_subject(repo, target)} "
        f"{_notification_title_kind(target, update_kind)}"
    )


def _notification_title_subject(repo: str, target: _NotificationTarget) -> str:
    subject = image_repo_ref(repo or target.target.repo or target.target.first)
    return subject.rsplit("/", 1)[-1] or subject or target.target.first


def _notification_title_kind(target: _NotificationTarget, update_kind: str) -> str:
    if update_kind == "digest" or is_digest_target_line(target.target):
        return "Digest Update"
    return "Tag Update"


def _lsio_image_rebuild_title(repo: str, tag: str, build_suffix: str) -> str:
    title = "LSIO image rebuild"
    if repo:
        title = f"{title}: {repo}"
    if tag:
        title = f"{title} {tag}"
    elif build_suffix:
        title = f"{title} {build_suffix}"
    return title


def _upstream_application_update_title(
    repo: str,
    upstream_repo: str,
    tag: str,
) -> str:
    title_repo = upstream_repo or repo
    if not title_repo:
        return ""
    title = f"Upstream application update: {title_repo}"
    if tag:
        title = f"{title} {tag}"
    return title


def _notification_description_lines(
    settings: WebSettings,
    target: _NotificationTarget,
    note: ReleaseNoteInfo,
    triggers: Sequence[ReleaseNotificationTrigger],
    *,
    repo: str,
    upstream_repo: str,
    tag: str,
    build_suffix: str,
    image_rebuild: bool,
    upstream_update: bool,
) -> list[str]:
    lines = [f"`{target.target.first}`"]
    if target.service_key:
        lines.append(f"Service: `{target.service_key}`")
    if repo:
        lines.append(f"Repository: `{repo}`")
    if upstream_repo and upstream_repo != repo:
        lines.append(f"Upstream: `{upstream_repo}`")
    if tag:
        lines.append(f"Release: `{tag}`")
    if image_rebuild:
        lines.append("Update type: LinuxServer.io rebuild")
        if build_suffix:
            lines.append(f"LSIO build: `{build_suffix}`")
    elif upstream_update:
        lines.append("Update type: upstream application update")
    if note.security.outcome == "verified_critical_high":
        identifiers = ", ".join(note.security.advisory_ids[:4])
        security_line = f"Security: verified {note.security.severity.title()} advisory"
        if identifiers:
            security_line = f"{security_line} ({identifiers})"
        lines.append(security_line)
        lines.append(note.security.reason)
    elif note.security.outcome == "needs_review":
        lines.append("Security: release needs review")
        lines.append(note.security.reason)
    lines.append(_status_line(settings, note))
    if note.breaking:
        lines.append("Breaking-risk indicators were detected.")
        lines.extend(note.breaking_reasons[:3])
    if triggers:
        label = ", ".join(_trigger_label(trigger) for trigger in triggers[:5])
        lines.append(f"WUD triggers: {label}")
    return lines


def _status_line(settings: WebSettings, note: ReleaseNoteInfo) -> str:
    if note.status == "ready":
        return "Release metadata is ready in WUDup."
    if note.status == "not_found":
        return "A matching GitHub release was not found; project links are included."
    if note.status == "missing":
        return "Release metadata was not cached before this notification."
    if note.error:
        error = _redact_sensitive_text(settings, note.error)
        return f"Release-note status: {error or note.status}"
    return f"Release-note status: {note.status}"


def _trigger_label(trigger: ReleaseNotificationTrigger) -> str:
    if trigger.type and trigger.name:
        return f"{trigger.type}/{trigger.name}"
    return trigger.name or trigger.type or trigger.id


def _payload_batches(
    items: Sequence[ReleaseNotificationItem],
    mode: str = "digest",
) -> list[dict[str, object]]:
    sendable = [item for item in items if not item.skipped_reason]
    if mode == "digest":
        return _digest_payload_batches(sendable)
    batches: list[dict[str, object]] = []
    for item in sendable:
        payload = {
            "username": DISCORD_WEBHOOK_USERNAME,
            "allowed_mentions": {"parse": []},
            "embeds": [_discord_embed(item)],
        }
        batches.append(
            {
                "index": len(batches) + 1,
                "count": 1,
                "items": [item],
                "payload": payload,
            }
        )
    return batches


def _digest_payload_batches(
    items: Sequence[ReleaseNotificationItem],
) -> list[dict[str, object]]:
    if not items:
        return []
    total_header = f"🧾 WUDup batch — {len(items)} updates found"
    batches: list[dict[str, object]] = []
    lines: list[str] = []
    batch_items: list[ReleaseNotificationItem] = []
    current_category = ""

    def finish_batch() -> None:
        header = f"🧾 WUDup batch — {len(batch_items)} updates found"
        content = "\n\n".join((header, "\n".join(lines), DISCORD_DIGEST_FOOTER))
        batches.append(
            {
                "index": len(batches) + 1,
                "count": len(batch_items),
                "items": list(batch_items),
                "payload": {
                    "username": DISCORD_WEBHOOK_USERNAME,
                    "allowed_mentions": {"parse": []},
                    "flags": DISCORD_SUPPRESS_EMBEDS_FLAG,
                    "content": content,
                },
            }
        )

    for category, category_label in DISCORD_DIGEST_CATEGORIES:
        for item in (item for item in items if item.category == category):
            row = _digest_row(item)
            prefix = _digest_category_prefix(
                lines,
                current_category=current_category,
                category=category,
                category_label=category_label,
            )
            candidate_lines = [*lines, *prefix, row]
            candidate = "\n\n".join(
                (total_header, "\n".join(candidate_lines), DISCORD_DIGEST_FOOTER)
            )
            if batch_items and len(candidate) > DISCORD_MESSAGE_CONTENT_LIMIT:
                finish_batch()
                lines = [category_label, row]
                batch_items = [item]
            else:
                lines = candidate_lines
                batch_items.append(item)
            current_category = category
    finish_batch()
    return batches


def _digest_category_prefix(
    lines: Sequence[str],
    *,
    current_category: str,
    category: str,
    category_label: str,
) -> list[str]:
    if current_category == category:
        return []
    return ([""] if lines else []) + [category_label]


def _payload_messages(batches: Sequence[Mapping[str, object]]) -> list[str]:
    messages: list[str] = []
    for batch in batches:
        payload = batch.get("payload")
        if not isinstance(payload, Mapping):
            continue
        content = str(payload.get("content") or "")
        if content:
            messages.append(content)
    return messages


def _digest_row(item: ReleaseNotificationItem) -> str:
    subject = item.service_key or image_repo_ref(
        item.image_repo or item.image
    ).rsplit("/", 1)[-1]
    row = (
        f"• {_discord_inline(subject)[:DISCORD_DIGEST_SUBJECT_LIMIT]} "
        f"{_discord_code(item.current_version[:DISCORD_DIGEST_VERSION_LIMIT])} → "
        f"{_discord_code(item.target_version[:DISCORD_DIGEST_VERSION_LIMIT])} — "
        f"{item.reason_label[:DISCORD_DIGEST_REASON_LIMIT]}"
    )
    selected_links: list[str] = []
    for link in _digest_links(item.links):
        candidate = f"{row} — {' | '.join([*selected_links, link])}"
        if len(candidate) > DISCORD_DIGEST_ROW_LIMIT:
            break
        selected_links.append(link)
    return f"{row} — {' | '.join(selected_links)}" if selected_links else row


def _compact_digest_link_label(link: ReleaseNoteLink) -> str:
    kind = link.kind.lower()
    label = link.label.lower()
    if "advisory" in kind or "ghsa" in label or "cve" in label:
        return link.label
    if "changelog" in kind or "changelog" in label:
        return "changelog"
    if kind == "lsio_release" or "lsio release" in label:
        return "LSIO release"
    if "upstream" in kind or "upstream" in label:
        return "upstream"
    if "release" in kind or "release" in label:
        return "release"
    if "project" in kind or "project" in label:
        return "project"
    return ""


def _digest_links(links: Sequence[ReleaseNoteLink]) -> list[str]:
    selected: list[str] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        compact_label = _compact_digest_link_label(link)
        if not compact_label:
            continue
        key = (compact_label, link.url)
        if not link.url or key in seen:
            continue
        seen.add(key)
        selected.append(_discord_link(compact_label, link.url))
    return selected


def _discord_inline(value: str) -> str:
    return value.replace("`", "'").replace("\n", " ").strip()


def _discord_code(value: str) -> str:
    return f"`{_discord_inline(value)}`"


def _discord_embed(item: ReleaseNotificationItem) -> dict[str, object]:
    links = [_discord_link(link.label, link.url) for link in item.links if link.url]
    fields: list[dict[str, object]] = [
        {"name": "Image", "value": f"`{item.image}`", "inline": False},
    ]
    if item.service_key:
        fields.append({"name": "Service", "value": f"`{item.service_key}`", "inline": True})
    if item.image_repo:
        fields.append(
            {"name": "Repository", "value": f"`{item.image_repo}`", "inline": True}
        )
    if item.upstream_repo and item.upstream_repo != item.image_repo:
        fields.append(
            {"name": "Upstream", "value": f"`{item.upstream_repo}`", "inline": True}
        )
    if links:
        fields.append({"name": "Links", "value": " - ".join(links)[:1024], "inline": False})
    if item.security.outcome != "ordinary":
        fields.append(
            {
                "name": "Security",
                "value": item.security.reason[:1024],
                "inline": False,
            }
        )
    if item.triggers:
        fields.append(
            {
                "name": "WUD triggers",
                "value": ", ".join(_trigger_label(trigger) for trigger in item.triggers)[:1024],
                "inline": False,
            }
        )
    embed: dict[str, object] = {
        "title": item.title[:256],
        "description": item.description[:DISCORD_EMBED_DESCRIPTION_LIMIT],
        "color": (
            DISCORD_SECURITY_COLOR
            if item.category == "security_urgent"
            else DISCORD_COLOR
        ),
        "fields": fields[:25],
    }
    first_url = next((link.url for link in item.links if link.url), "")
    if first_url:
        embed["url"] = first_url
    return embed


def _discord_link(label: str, url: str) -> str:
    safe_label = (label or "Release link").replace("[", "").replace("]", "")
    safe_url = url.replace(")", "%29")
    return f"[{safe_label}]({safe_url})"


@dataclass(frozen=True)
class _WebhookConfig:
    value: str
    source: str


def _discord_webhook(settings: WebSettings) -> _WebhookConfig:
    value, source = effective_release_notification_webhook(settings)
    return _WebhookConfig(value=value, source=source)


def _post_discord_payload(webhook_url: str, payload: Mapping[str, object]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        method="POST",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": DISCORD_WEBHOOK_USER_AGENT,
        },
    )
    with urllib.request.urlopen(
        request,
        timeout=DISCORD_WEBHOOK_TIMEOUT_SECONDS,
    ) as response:
        if response.status < 200 or response.status >= 300:
            raise urllib.error.HTTPError(
                webhook_url,
                response.status,
                "Discord webhook request failed",
                response.headers,
                None,
            )


def _test_discord_payload() -> dict[str, object]:
    items = [
        ReleaseNotificationItem(
            line_no=1,
            image="ghcr.io/magrhino/wudup:latest",
            service_key="system/wudup",
            title="WUDup test notification",
            description="Representative mutable-tag digest row.",
            status="ready",
            image_repo="magrhino/wudup",
            upstream_repo="magrhino/wudup",
            current_version="latest",
            target_version="latest (release v1.2.3)",
            category="needs_review",
            reason_code="mutable_latest",
            reason_label="mutable latest tag",
            links=[
                ReleaseNoteLink(
                    label="GitHub release",
                    url="https://github.com/magrhino/wudup/releases",
                    kind="github_release",
                )
            ],
        ),
        ReleaseNotificationItem(
            line_no=2,
            image="lscr.io/linuxserver/jellyfin:latest",
            service_key="media/jellyfin",
            title="Jellyfin test notification",
            description="Representative LSIO digest row.",
            status="ready",
            image_repo="linuxserver/docker-jellyfin",
            upstream_repo="jellyfin/jellyfin",
            current_version="latest",
            target_version="latest (release v10.11.0)",
            category="needs_review",
            reason_code="lsio_latest",
            reason_label="LSIO image update via mutable latest",
            links=[
                ReleaseNoteLink(
                    label="LSIO release",
                    url="https://github.com/linuxserver/docker-jellyfin/releases",
                    kind="lsio_release",
                ),
                ReleaseNoteLink(
                    label="Upstream release",
                    url="https://github.com/jellyfin/jellyfin/releases",
                    kind="github_release",
                ),
            ],
        ),
    ]
    payload = _digest_payload_batches(items)[0]["payload"]
    assert isinstance(payload, dict)
    return payload
