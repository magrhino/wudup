from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from tests.web_test_helpers import (
    _capture_discord_posts,
    _client,
    _csrf_headers,
    _fake_release_refresh,
    _install_wud_api,
    _store_web_setting,
    _wud_api_container,
)

from wudup import web_discord as discord_module
from wudup import web_release_notifications as notifications_module
from wudup.db import init_db, insert_pending_update, insert_update_run, open_db
from wudup.lsio_updates import LSIOTagParts, LSIOUpdateClassification
from wudup.release_notes import ReleaseNoteInfo as ReleaseNoteData
from wudup.release_notes import ReleaseNoteLink as ReleaseNoteLinkData
from wudup.release_notes import ReleaseSecurityAssessment
from wudup.web_models import ReleaseSecurityAssessment as WebReleaseSecurityAssessment

_RELEASE_NOTIFICATION_ENV = {
    "WUD_WEB_DEV_NO_AUTH": "true",
    "WUD_WEB_MUTATIONS_ENABLED": "true",
    "WUD_RELEASE_NOTES_ENABLED": "true",
    "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
}


def _release_notification_client(tmp_path: Path, monkeypatch):
    _fake_release_refresh(monkeypatch)
    posted = _capture_discord_posts(monkeypatch)
    return _client(tmp_path, _RELEASE_NOTIFICATION_ENV), posted


def _write_pending_lines(tmp_path: Path, lines: list[str]) -> None:
    (tmp_path / "state" / "images.todo").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def test_notification_identity_includes_wud_metadata() -> None:
    target = notifications_module.WudTarget(
        line_no=1,
        raw="ghcr.io/acme/app:1.0.0 tag=2.0.0",
        first="ghcr.io/acme/app:1.0.0",
        key="app",
        repo="ghcr.io/acme/app",
        has_tag=True,
        allow_repo=True,
        digest="",
        desired_tag="2.0.0",
        tag_token="1.0.0",
    )
    note = notifications_module.ReleaseNoteInfo(
        line_no=1,
        status="ready",
        provider="github",
        image_repo="ghcr.io/acme/app",
        upstream_repo="acme/app",
        release_tag="2.0.0",
        title="v2.0.0",
    )
    metadata = SimpleNamespace(
        local_digest="sha256:local",
        remote_tag="2.0.0",
        remote_digest="sha256:remote-a",
        link="https://github.com/acme/app",
        labels={
            "org.opencontainers.image.source": "https://github.com/acme/app",
            "ignored": "not persisted",
        },
    )

    first = notifications_module.web_release_notification_state.notification_identity(
        target,
        note,
        metadata,
    )

    assert first.metadata["metadata"] == {
        "local_digest": "sha256:local",
        "remote_tag": "2.0.0",
        "remote_digest": "sha256:remote-a",
        "link": "https://github.com/acme/app",
        "source_label": "https://github.com/acme/app",
    }
    mapped_metadata = notifications_module.web_release_notification_state.notification_identity(
        target,
        note,
        {"watcher": "docker.local", "container": "app-a", "update_kind": "tag"},
    )
    changed_mapped_metadata = notifications_module.web_release_notification_state.notification_identity(
        target,
        note,
        {"watcher": "docker.local", "container": "app-b", "update_kind": "tag"},
    )

    assert mapped_metadata.notification_key != changed_mapped_metadata.notification_key

    unresolved_target = notifications_module.WudTarget(
        line_no=1,
        raw="ghcr.io/acme/app:1.0.0",
        first="ghcr.io/acme/app:1.0.0",
        key="app",
        repo="ghcr.io/acme/app",
        has_tag=True,
        allow_repo=True,
        digest="",
        desired_tag="",
        tag_token="1.0.0",
    )
    unresolved_note = note.model_copy(update={"release_tag": ""})
    first_fallback = notifications_module.web_release_notification_state.notification_identity(
        unresolved_target,
        unresolved_note,
        metadata,
    )
    changed_fallback = notifications_module.web_release_notification_state.notification_identity(
        unresolved_target,
        unresolved_note,
        SimpleNamespace(**{**metadata.__dict__, "remote_digest": "sha256:remote-b"}),
    )

    assert first_fallback.notification_key != changed_fallback.notification_key

    verified_note = note.model_copy(
        update={
            "security": WebReleaseSecurityAssessment(
                outcome="verified_critical_high",
                severity="high",
                reason_code="verified_exposure",
                reason="Verified exposure.",
                advisory_ids=["GHSA-aaaa-bbbb-cccc"],
            )
        }
    )
    verified = notifications_module.web_release_notification_state.notification_identity(
        target,
        verified_note,
        metadata,
    )
    changed_advisory = notifications_module.web_release_notification_state.notification_identity(
        target,
        verified_note.model_copy(
            update={
                "security": verified_note.security.model_copy(
                    update={"advisory_ids": ["GHSA-dddd-eeee-ffff"]}
                )
            }
        ),
        metadata,
    )
    changed_local_digest = (
        notifications_module.web_release_notification_state.notification_identity(
            target,
            verified_note,
            SimpleNamespace(**{**metadata.__dict__, "local_digest": "sha256:local-b"}),
        )
    )
    changed_remote_digest = (
        notifications_module.web_release_notification_state.notification_identity(
            target,
            verified_note,
            SimpleNamespace(
                **{**metadata.__dict__, "remote_digest": "sha256:remote-b"}
            ),
        )
    )
    verified_without_metadata = (
        notifications_module.web_release_notification_state.notification_identity(
            target,
            verified_note,
        )
    )

    assert verified.notification_key != first.notification_key
    assert changed_advisory.notification_key != verified.notification_key
    assert changed_local_digest.notification_key != verified.notification_key
    assert changed_remote_digest.notification_key != verified.notification_key
    assert verified_without_metadata.notification_key != verified.notification_key
    assert verified.metadata["security"]["advisory_ids"] == [
        "GHSA-AAAA-BBBB-CCCC"
    ]


def test_failed_notification_history_with_prior_send_is_skipped() -> None:
    state = notifications_module.web_release_notification_state
    identity = state.NotificationIdentity(
        notification_key="notification-a",
        metadata={},
    )
    history = state.NotificationHistory(
        notification_key=identity.notification_key,
        mode="digest",
        status="failure",
        last_attempted_at="2026-01-02T00:00:00+00:00",
        last_sent_at="2026-01-01T00:00:00+00:00",
        send_count=1,
        last_audit_run_id=1,
        metadata={},
    )

    assert state.notification_decision(
        state.ReleaseNotificationConfig(),
        identity,
        history,
        resend=False,
    ) == ("skipped_duplicate", "Already sent for this update.")
    assert state.notification_decision(
        state.ReleaseNotificationConfig(),
        identity,
        history,
        resend=True,
    ) == ("manual_resend", "")


def test_sending_notification_history_skips_until_stale() -> None:
    state = notifications_module.web_release_notification_state
    identity = state.NotificationIdentity(
        notification_key="notification-a",
        metadata={},
    )
    history = state.NotificationHistory(
        notification_key=identity.notification_key,
        mode="digest",
        status="sending",
        last_attempted_at="2026-01-01T00:00:00+00:00",
        last_sent_at="",
        send_count=0,
        last_audit_run_id=1,
        metadata={},
    )

    assert state.notification_decision(
        state.ReleaseNotificationConfig(),
        identity,
        history,
        resend=False,
        now="2026-01-01T00:09:59+00:00",
    ) == ("sending", "Notification is already being sent.")
    assert state.notification_decision(
        state.ReleaseNotificationConfig(),
        identity,
        history,
        resend=False,
        now="2026-01-01T00:10:00+00:00",
    ) == ("new", "")


def test_reserve_notification_history_reclaims_stale_sending_row(tmp_path: Path) -> None:
    state = notifications_module.web_release_notification_state
    identity = state.NotificationIdentity(
        notification_key="notification-a",
        metadata={"image": "ghcr.io/acme/app:1.0.0"},
    )
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        init_db(conn)
        with conn:
            assert state.reserve_notification_history(
                conn,
                identity=identity,
                config=state.ReleaseNotificationConfig(),
                audit_run_id=1,
                now="2026-01-01T00:00:00+00:00",
            )
            assert not state.reserve_notification_history(
                conn,
                identity=identity,
                config=state.ReleaseNotificationConfig(),
                audit_run_id=2,
                now="2026-01-01T00:09:59+00:00",
            )
            assert state.reserve_notification_history(
                conn,
                identity=identity,
                config=state.ReleaseNotificationConfig(),
                audit_run_id=3,
                now="2026-01-01T00:10:00+00:00",
            )


def test_notification_history_by_key_binds_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "wud.sqlite"
    hostile_key = "abc') OR 1=1 --"
    with open_db(db_path) as conn:
        init_db(conn)
        with conn:
            for key in (hostile_key, "other-key"):
                notifications_module.web_release_notification_state.upsert_notification_history(
                    conn,
                    identity=notifications_module.web_release_notification_state.NotificationIdentity(
                        notification_key=key,
                        metadata={},
                    ),
                    config=notifications_module.web_release_notification_state.ReleaseNotificationConfig(),
                    status="sent",
                    audit_run_id=1,
                    now="2026-06-01T00:00:00+00:00",
                )

        histories = notifications_module.web_release_notification_state.notification_history_by_key(
            conn,
            {hostile_key},
        )

    assert set(histories) == {hostile_key}


def test_release_notification_preview_includes_wud_triggers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_release_refresh(monkeypatch)
    _install_wud_api(
        monkeypatch,
        containers=[
            _wud_api_container(
                image="ghcr.io/acme/app",
                tag="1.0.0",
                remote_tag="2.0.0",
            )
        ],
        triggers={
            "docker.local.app": (
                200,
                [
                    {
                        "id": "discord.release",
                        "type": "discord",
                        "name": "release",
                        "configuration": {"url": "https://discord.test/secret"},
                    }
                ],
            )
        },
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_RELEASE_NOTES_ENABLED": "true",
            "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
            "WUD_API_BASE_URL": "https://wud.release-notifications.test:3000",
        },
    )
    (tmp_path / "state" / "images.todo").write_text(
        "ghcr.io/acme/app:1.0.0 tag=2.0.0\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["destination"] == {
        "type": "discord",
        "configured": True,
        "source": "DISCORD_WEBHOOK",
    }
    assert body["sendable_count"] == 1
    assert body["items"][0]["triggers"] == [
        {"id": "discord.release", "type": "discord", "name": "release"}
    ]
    assert "webhook-secret" not in response.text
    assert "secret" not in json.dumps(body["items"][0]["triggers"])


def test_notification_items_cache_wud_trigger_lookup_by_container(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_container_triggers(_settings, container_id: str):
        calls.append(container_id)
        return (
            [
                notifications_module.ReleaseNotificationTrigger(
                    id="discord.release",
                    type="discord",
                    name="release",
                )
            ],
            "",
        )

    def target(line_no: int) -> notifications_module._NotificationTarget:
        return notifications_module._NotificationTarget(
            target=notifications_module.WudTarget(
                line_no=line_no,
                raw=f"ghcr.io/acme/app:{line_no}.0.0 tag=2.0.0",
                first=f"ghcr.io/acme/app:{line_no}.0.0",
                key=f"app-{line_no}",
                repo="ghcr.io/acme/app",
                has_tag=True,
                allow_repo=True,
                digest="",
                desired_tag="2.0.0",
                tag_token=f"{line_no}.0.0",
            ),
            service_key=f"app-{line_no}",
            wud_container_id="docker.local.app",
        )

    monkeypatch.setattr(
        notifications_module.web_wud_api,
        "container_triggers",
        fake_container_triggers,
    )
    source = notifications_module._NotificationSource(
        targets=(target(1), target(2)),
        source_file="images.todo",
        source=notifications_module.PendingSourceInfo(),
        wud_api=notifications_module.WudApiStatus(
            state="ready",
            available=True,
            metadata_available=True,
            last_checked_at="2026-06-01T00:00:00Z",
        ),
        metadata_by_line={},
    )
    notes = {
        line_no: notifications_module.ReleaseNoteInfo(
            line_no=line_no,
            status="ready",
            provider="github",
            image_repo="ghcr.io/acme/app",
            upstream_repo="acme/app",
            release_tag="2.0.0",
            title="v2.0.0",
        )
        for line_no in (1, 2)
    }

    items, warnings = notifications_module._notification_items(
        object(),
        source,
        notes,
        config=notifications_module.web_release_notification_state.ReleaseNotificationConfig(),
        resend=False,
    )

    assert calls == ["docker.local.app"]
    assert warnings == []
    assert [item.triggers[0].id for item in items] == [
        "discord.release",
        "discord.release",
    ]


def test_digest_reason_priority_uses_deterministic_metadata() -> None:
    target = notifications_module._NotificationTarget(
        target=notifications_module.WudTarget(
            line_no=1,
            raw="ghcr.io/acme/app:1.0.0 tag=2.0.0",
            first="ghcr.io/acme/app:1.0.0",
            key="app",
            repo="ghcr.io/acme/app",
            has_tag=True,
            allow_repo=True,
            digest="",
            desired_tag="2.0.0",
            tag_token="1.0.0",
        )
    )
    release_link = notifications_module.ReleaseNoteLink(
        label="GitHub release",
        url="https://github.com/acme/app/releases/tag/v2.0.0",
        kind="github_release",
    )

    def reason(
        *,
        status: str = "ready",
        breaking: bool = False,
        links: list | None = None,
        semver_diff: str = "",
        current_version: str = "1.0.0",
        target_version: str = "1.0.1",
        provider: str = "github",
        change_type: str = "unknown",
        update_kind: str = "tag",
        security: dict[str, object] | None = None,
    ) -> tuple[str, str, str]:
        note = notifications_module.ReleaseNoteInfo(
            line_no=1,
            status=status,
            provider=provider,
            image_repo="acme/app",
            upstream_repo="acme/app",
            breaking=breaking,
            links=[release_link] if links is None else links,
            classification={"change_type": change_type},
            **({"security": security} if security is not None else {}),
        )
        metadata = SimpleNamespace(
            semver_diff=semver_diff,
            update_kind=update_kind,
        )
        return discord_module._notification_digest_reason(
            target,
            note,
            metadata,
            current_version=current_version,
            target_version=target_version,
        )

    assert reason(
        breaking=True,
        semver_diff="major",
        security={
            "outcome": "verified_critical_high",
            "severity": "critical",
            "reason_code": "verified_exposure",
            "reason": "Verified exposure.",
            "advisory_ids": ["GHSA-aaaa-bbbb-cccc"],
            "lookup_truncated": False,
        },
    ) == (
        "security_urgent",
        "verified_security",
        "Critical security update (GHSA-aaaa-bbbb-cccc)",
    )
    assert reason(
        breaking=True,
        semver_diff="major",
        security={"outcome": "needs_review"},
    ) == ("needs_review", "security_needs_review", "security update needs review")
    assert reason(
        security={"outcome": "verified_critical_high", "severity": "high"},
    ) == ("security_urgent", "verified_security", "High security update")
    assert reason(breaking=True, semver_diff="major") == (
        "needs_review",
        "breaking_change",
        "possible breaking change",
    )
    assert reason(provider="lsio", breaking=True, semver_diff="major") == (
        "needs_review",
        "breaking_change",
        "LSIO image update: possible breaking change",
    )
    assert reason(semver_diff="major")[:2] == ("needs_review", "major_bump")
    assert reason(provider="lsio", semver_diff="major") == (
        "needs_review",
        "major_bump",
        "LSIO image update: major version bump",
    )
    assert reason(status="missing", semver_diff="minor")[:2] == (
        "needs_review",
        "release_notes_missing",
    )
    for status, code, label in (
        ("error", "release_notes_error", "release-note lookup failed"),
        ("unsupported", "release_notes_unsupported", "release notes unsupported"),
        ("missing", "release_notes_missing", "release notes unavailable"),
        ("not_found", "release_notes_not_found", "matching release not found"),
    ):
        assert reason(provider="lsio", status=status, semver_diff="minor") == (
            "needs_review",
            code,
            f"LSIO image update: {label}",
        )
    assert reason(
        provider="lsio",
        status="error",
        current_version="latest",
        target_version="latest (release v2.0.0)",
    ) == (
        "needs_review",
        "release_notes_error",
        "LSIO image update: release-note lookup failed",
    )
    assert reason(current_version="latest", semver_diff="minor")[:2] == (
        "needs_review",
        "mutable_latest",
    )
    assert reason(
        current_version="1.0.0",
        target_version="latest",
        semver_diff="minor",
    )[:2] == (
        "needs_review",
        "mutable_latest",
    )
    assert reason(
        provider="lsio",
        change_type="upstream_update",
        current_version="latest",
        target_version="latest (release v2.0.0)",
    ) == (
        "needs_review",
        "lsio_upstream",
        "LSIO/upstream release via mutable latest",
    )
    assert reason(
        provider="lsio",
        current_version="latest",
        target_version="latest (release v2.0.0)",
    )[:2] == ("needs_review", "lsio_latest")
    assert reason(links=[]) == (
        "needs_review",
        "release_link_missing",
        "release or changelog link unavailable",
    )
    assert reason(provider="lsio", links=[]) == (
        "needs_review",
        "release_link_missing",
        "LSIO image update: release or changelog link unavailable",
    )
    assert reason(semver_diff="minor")[:2] == ("worth_noting", "minor_bump")
    assert reason(
        provider="lsio",
        change_type="upstream_update",
        semver_diff="minor",
    )[:2] == (
        "worth_noting",
        "lsio_upstream",
    )
    assert reason(provider="lsio")[:2] == ("worth_noting", "lsio_update")
    assert reason(semver_diff="patch")[:2] == ("routine", "patch_bump")
    assert reason(
        current_version="current image",
        target_version="new digest",
        update_kind="digest",
    )[:2] == ("routine", "routine_digest")


def test_latest_digest_version_includes_resolved_release_tag_without_metadata() -> None:
    target = notifications_module._NotificationTarget(
        target=notifications_module.WudTarget(
            line_no=1,
            raw="ghcr.io/acme/app:latest sha256=new",
            first="ghcr.io/acme/app:latest",
            key="app",
            repo="ghcr.io/acme/app",
            has_tag=True,
            allow_repo=True,
            digest="sha256:new",
            desired_tag="",
            tag_token="latest",
        )
    )
    note = notifications_module.ReleaseNoteInfo(
        line_no=1,
        status="ready",
        provider="github",
        image_repo="acme/app",
        upstream_repo="acme/app",
        release_tag="v2.0.0",
    )

    assert discord_module._notification_versions(
        target,
        note,
        None,
    ) == ("latest", "latest (release v2.0.0)")
    assert discord_module._notification_versions(
        target,
        note,
        SimpleNamespace(local_tag="latest", remote_tag="latest", update_kind="digest"),
    ) == ("latest", "latest (release v2.0.0)")

    versioned_target = notifications_module._NotificationTarget(
        target=notifications_module.parse_wud_text(
            "ghcr.io/acme/app:1.0.0 sha256=new"
        ).targets[0]
    )
    assert discord_module._notification_versions(
        versioned_target,
        note,
        None,
    ) == ("1.0.0", "new digest")

    lsio_note = note.model_copy(
        update={
            "provider": "lsio",
            "classification": note.classification.model_copy(
                update={"change_type": "upstream_update"}
            ),
        }
    )
    current_version, target_version = discord_module._notification_versions(
        target,
        lsio_note,
        None,
    )

    assert (current_version, target_version) == (
        "latest",
        "latest (release v2.0.0)",
    )
    assert discord_module._notification_digest_reason(
        target,
        lsio_note,
        None,
        current_version=current_version,
        target_version=target_version,
    ) == (
        "needs_review",
        "lsio_upstream",
        "LSIO/upstream release via mutable latest",
    )


def test_semver_diff_rejects_unreasonably_long_numeric_parts() -> None:
    oversized = "1" * 100

    assert discord_module._semver_diff(
        f"{oversized}.0.0",
        f"{oversized}.1.0",
    ) == ""


def test_semver_diff_rejects_non_ascii_digits() -> None:
    assert discord_module._semver_diff("١.0.0", "٢.0.0") == ""


def test_digest_row_selects_compact_release_links() -> None:
    item = notifications_module.ReleaseNotificationItem(
        line_no=1,
        image="ghcr.io/acme/app:1.0.0",
        service_key="media/app",
        title="app Tag Update",
        description="",
        status="ready",
        current_version="1.0.0",
        target_version="1.1.0",
        category="worth_noting",
        reason_code="minor_bump",
        reason_label="minor update with release notes",
        links=[
            notifications_module.ReleaseNoteLink(
                label="GitHub release",
                url="https://example.test/release",
                kind="github_release",
            ),
            notifications_module.ReleaseNoteLink(
                label="Upstream release",
                url="https://example.test/upstream",
                kind="github_release",
            ),
            notifications_module.ReleaseNoteLink(
                label="Changelog",
                url="https://example.test/changelog",
                kind="changelog",
            ),
        ],
    )

    row = discord_module._digest_row(item)

    assert row.startswith("• media/app `1.0.0` → `1.1.0`")
    assert "[release](https://example.test/release)" in row
    assert "[upstream](https://example.test/upstream)" in row
    assert "[changelog](https://example.test/changelog)" in row
    assert "[release](https://example.test/release) | [upstream]" in row


def test_digest_links_classifies_supported_links_and_deduplicates() -> None:
    links = [
        notifications_module.ReleaseNoteLink(
            label="LinuxServer release",
            url="https://example.test/lsio",
            kind="lsio_release",
        ),
        notifications_module.ReleaseNoteLink(
            label="Project page",
            url="https://example.test/project",
            kind="project",
        ),
        notifications_module.ReleaseNoteLink(
            label="Project duplicate",
            url="https://example.test/project",
            kind="project",
        ),
        notifications_module.ReleaseNoteLink(
            label="Documentation",
            url="https://example.test/docs",
            kind="documentation",
        ),
    ]

    assert discord_module._digest_links(links) == [
        "[LSIO release](https://example.test/lsio)",
        "[project](https://example.test/project)",
    ]


def test_release_notification_preview_degrades_when_wud_triggers_require_auth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_release_refresh(monkeypatch)
    _install_wud_api(
        monkeypatch,
        containers=[
            _wud_api_container(
                image="ghcr.io/acme/app",
                tag="1.0.0",
                remote_tag="2.0.0",
            )
        ],
        triggers={"docker.local.app": (401, {"detail": "secret-token"})},
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_RELEASE_NOTES_ENABLED": "true",
            "WUD_API_BASE_URL": "https://wud.release-notifications.test:3000",
        },
    )
    (tmp_path / "state" / "images.todo").write_text(
        "ghcr.io/acme/app:1.0.0 tag=2.0.0\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["triggers"] == []
    assert any("requires authentication" in warning for warning in body["warnings"])
    assert "secret-token" not in response.text


def test_release_notification_send_requires_mutations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_release_refresh(monkeypatch)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_RELEASE_NOTES_ENABLED": "true",
            "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
        },
    )
    (tmp_path / "state" / "images.todo").write_text(
        "ghcr.io/acme/app:1.0.0 tag=2.0.0\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "mutations are disabled"


def test_release_notification_preview_disabled_does_not_refresh_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_refresh(*_args, **_kwargs):
        raise AssertionError("release metadata should not refresh when disabled")

    monkeypatch.setattr(notifications_module, "refresh_release_notes", fail_refresh)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    (tmp_path / "state" / "images.todo").write_text(
        "ghcr.io/acme/app:1.0.0 tag=2.0.0\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["count"] == 0
    assert body["wud_api"]["available"] is False
    assert body["source"]["detail"] == "Release-note notifications are disabled."
    assert body["wud_api"]["detail"] == "Release-note notifications are disabled."
    assert body["warnings"] == ["Release-note notifications are disabled."]


def test_release_notification_send_requires_webhook(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_refresh(*_args, **_kwargs):
        raise AssertionError("release metadata should not refresh without webhook")

    monkeypatch.setattr(notifications_module, "refresh_release_notes", fail_refresh)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_RELEASE_NOTES_ENABLED": "true",
        },
    )
    (tmp_path / "state" / "images.todo").write_text(
        "ghcr.io/acme/app:1.0.0 tag=2.0.0\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Discord release-note webhook is not configured"


def test_release_notification_test_webhook_guards(
    tmp_path: Path,
    monkeypatch,
) -> None:
    posted = _capture_discord_posts(monkeypatch)
    payload = {"confirmation": "send-test-webhook"}
    unauthenticated = _client(
        tmp_path,
        {
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
        },
    )
    read_only = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
        },
    )
    mutating = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
        },
    )
    no_webhook = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )

    unauthenticated_response = unauthenticated.post(
        "/api/v1/release-notifications/test",
        json=payload,
        headers=_csrf_headers(unauthenticated),
    )
    missing_csrf = mutating.post(
        "/api/v1/release-notifications/test",
        json=payload,
    )
    read_only_response = read_only.post(
        "/api/v1/release-notifications/test",
        json=payload,
        headers=_csrf_headers(read_only),
    )
    no_webhook_response = no_webhook.post(
        "/api/v1/release-notifications/test",
        json=payload,
        headers=_csrf_headers(no_webhook),
    )
    get_response = mutating.get("/api/v1/release-notifications/test")

    assert unauthenticated_response.status_code == 403
    assert unauthenticated_response.json()["detail"] == "setup required"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "origin header is required"
    assert read_only_response.status_code == 403
    assert read_only_response.json()["detail"] == "mutations are disabled"
    assert no_webhook_response.status_code == 422
    assert (
        no_webhook_response.json()["detail"]
        == "Discord release-note webhook is not configured"
    )
    assert get_response.status_code == 405
    assert posted == []


def test_release_notification_test_webhook_posts_payload_and_audits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    posted = _capture_discord_posts(monkeypatch)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
        },
    )

    response = client.post(
        "/api/v1/release-notifications/test",
        json={"confirmation": "send-test-webhook"},
        headers=_csrf_headers(client),
    )

    body = response.json()
    assert response.status_code == 200
    assert body == {
        "sent": True,
        "destination": {
            "type": "discord",
            "configured": True,
            "source": "DISCORD_WEBHOOK",
        },
        "audit_run_id": body["audit_run_id"],
    }
    assert body["audit_run_id"]
    assert len(posted) == 1
    assert posted[0][0] == "https://discord.test/webhook-secret"
    payload = posted[0][1]
    assert payload["flags"] == notifications_module.DISCORD_SUPPRESS_EMBEDS_FLAG
    assert "WUDup batch — 2 updates found" in payload["content"]
    assert "latest (release v1.2.3)" in payload["content"]
    assert "LSIO image update via mutable latest" in payload["content"]
    assert "https://github.com/jellyfin/jellyfin/releases" in payload["content"]
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        run = conn.execute(
            "SELECT * FROM update_runs WHERE id = ?",
            (body["audit_run_id"],),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM update_events WHERE run_id = ?",
            (body["audit_run_id"],),
        ).fetchone()

    run_metadata = json.loads(run["metadata_json"])
    event_metadata = json.loads(event["metadata_json"])
    assert run["mode"] == "web-release-notifications"
    assert run["status"] == "success"
    assert run_metadata["operation"] == "test_release_notification_webhook"
    assert run_metadata["destination"] == {
        "type": "discord",
        "source": "DISCORD_WEBHOOK",
    }
    assert event["status"] == "success"
    assert event_metadata["operation"] == "test_release_notification_webhook"
    serialized = json.dumps({"run": run_metadata, "event": event_metadata})
    assert "webhook-secret" not in serialized


def test_release_notification_test_webhook_audit_finish_failure_preserves_send(
    tmp_path: Path,
    monkeypatch,
) -> None:
    posted = _capture_discord_posts(monkeypatch)
    original_finish = notifications_module._finish_release_notification_test_audit

    def fake_finish_test_audit(*args, **kwargs):
        if kwargs["status"] == "success":
            raise sqlite3.OperationalError("audit finish failed")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(
        notifications_module,
        "_finish_release_notification_test_audit",
        fake_finish_test_audit,
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
        },
    )

    response = client.post(
        "/api/v1/release-notifications/test",
        json={"confirmation": "send-test-webhook"},
        headers=_csrf_headers(client),
    )

    body = response.json()
    assert response.status_code == 200
    assert body["sent"] is True
    assert body["audit_run_id"]
    assert len(posted) == 1
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        run = conn.execute(
            "SELECT * FROM update_runs WHERE id = ?",
            (body["audit_run_id"],),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM update_events WHERE run_id = ?",
            (body["audit_run_id"],),
        ).fetchone()

    assert run["status"] == "failure"
    assert event["status"] == "failure"
    error = json.loads(run["metadata_json"])["error"]
    assert error.startswith("could not finalize Discord test webhook audit:")
    assert "could not send Discord test webhook" not in error


def test_release_notification_test_webhook_failure_is_sanitized_and_audited(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _capture_discord_posts(monkeypatch, fail_on=1)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
        },
    )

    response = client.post(
        "/api/v1/release-notifications/test",
        json={"confirmation": "send-test-webhook"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 500
    assert response.json()["detail"].startswith("could not send Discord test webhook:")
    assert "webhook-secret" not in response.text
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        run = conn.execute(
            """
            SELECT *
            FROM update_runs
            WHERE mode = 'web-release-notifications'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM update_events WHERE run_id = ?",
            (run["id"],),
        ).fetchone()

    assert run["status"] == "failure"
    assert event["status"] == "failure"
    serialized = json.dumps(
        {
            "run": json.loads(run["metadata_json"]),
            "event": json.loads(event["metadata_json"]),
        }
    )
    assert "webhook-secret" not in serialized


def test_release_notification_preview_rejects_duplicate_pending_lines(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_refresh(*_args, **_kwargs):
        raise AssertionError("release metadata should not refresh for invalid input")

    monkeypatch.setattr(notifications_module, "refresh_release_notes", fail_refresh)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_RELEASE_NOTES_ENABLED": "true",
        },
    )
    (tmp_path / "state" / "images.todo").write_text(
        "ghcr.io/acme/app:1.0.0 tag=2.0.0\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1, 1]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "line_numbers line 1 was provided more than once"


def test_release_notification_preview_uses_completed_run_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_release_refresh(monkeypatch)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_RELEASE_NOTES_ENABLED": "true",
        },
    )
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        init_db(conn)
        run_id = insert_update_run(
            conn,
            started_at="2026-05-27T12:00:00+00:00",
            status="success",
            dry_run=False,
            mode="stop",
            wud_file="/out/images.todo",
            metadata_json='{"source":"test"}',
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=7,
            raw="ghcr.io/acme/app:1.0.0 tag=2.0.0",
            image="ghcr.io/acme/app:1.0.0",
            desired_tag="2.0.0",
            service_key="media/app",
            status="resolved",
            status_reason="updated",
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=8,
            raw="ghcr.io/acme/failed:1.0.0 tag=2.0.0",
            image="ghcr.io/acme/failed:1.0.0",
            desired_tag="2.0.0",
            service_key="media/failed",
            status="failed",
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=10,
            raw="ghcr.io/acme/removed:1.0.0 tag=2.0.0",
            image="ghcr.io/acme/removed:1.0.0",
            desired_tag="2.0.0",
            service_key="media/removed",
            status="resolved",
            status_reason="removed-before-run",
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=11,
            raw="ghcr.io/acme/excluded:1.0.0 tag=2.0.0",
            image="ghcr.io/acme/excluded:1.0.0",
            desired_tag="2.0.0",
            service_key="media/excluded",
            status="resolved",
            status_reason="tag-excluded",
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=9,
            raw="ghcr.io/acme/pending:1.0.0 tag=2.0.0",
            image="ghcr.io/acme/pending:1.0.0",
            desired_tag="2.0.0",
            service_key="media/pending",
            status="pending",
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=12,
            raw="ghcr.io/acme/latest:latest sha256=new",
            image="ghcr.io/acme/latest:latest",
            desired_tag="",
            service_key="media/latest",
            status="resolved",
            status_reason="updated",
        )

    response = client.post(
        "/api/v1/release-notifications/preview",
        json={"run_id": run_id},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source_file"] == f"Run #{run_id}"
    assert [item["line_no"] for item in body["items"]] == [7, 12]
    assert body["items"][0]["service_key"] == "media/app"
    assert body["items"][1]["current_version"] == "latest"
    assert body["items"][1]["target_version"] == "latest (release 2.0.0)"


def test_release_notification_preview_rejects_non_update_run_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_refresh(*_args, **_kwargs):
        raise AssertionError("release metadata should not refresh for non-update runs")

    monkeypatch.setattr(notifications_module, "refresh_release_notes", fail_refresh)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_RELEASE_NOTES_ENABLED": "true",
        },
    )
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        init_db(conn)
        run_id = insert_update_run(
            conn,
            started_at="2026-05-27T12:00:00+00:00",
            status="success",
            dry_run=False,
            mode="web-pending-cleanup",
            wud_file="/out/images.todo",
            metadata_json='{"source":"test"}',
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=7,
            raw="ghcr.io/acme/app:1.0.0 tag=2.0.0",
            image="ghcr.io/acme/app:1.0.0",
            desired_tag="2.0.0",
            service_key="media/app",
            status="resolved",
            status_reason="updated",
        )

    response = client.post(
        "/api/v1/release-notifications/preview",
        json={"run_id": run_id},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "release notifications require a successful update run"
    )


def test_release_notification_preview_and_send_redact_cached_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_refresh_release_notes(
        _conn,
        targets,
        _environ,
        **_kwargs,
    ):
        return [
            ReleaseNoteData(
                line_no=target.line_no,
                status="error",
                provider="github",
                image_repo="acme/app",
                upstream_repo="acme/app",
                error="GitHub lookup failed for https://discord.test/webhook-secret",
            )
            for target in targets
        ]

    monkeypatch.setattr(
        notifications_module,
        "refresh_release_notes",
        fake_refresh_release_notes,
    )
    posted = _capture_discord_posts(monkeypatch)
    client = _client(tmp_path, _RELEASE_NOTIFICATION_ENV)
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])

    preview = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )
    send = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=_csrf_headers(client),
    )

    assert preview.status_code == 200
    assert send.status_code == 200
    assert "Release-note status:" in preview.json()["items"][0]["description"]
    assert "webhook-secret" not in preview.text
    assert "webhook-secret" not in json.dumps([payload for _url, payload in posted])


def test_release_notification_preview_and_digest_distinguish_lsio_rebuild(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_refresh_release_notes(
        _conn,
        targets,
        _environ,
        **_kwargs,
    ):
        return [
            ReleaseNoteData(
                line_no=target.line_no,
                status="ready",
                provider="lsio",
                image_repo="linuxserver/docker-radarr",
                upstream_repo="Radarr/Radarr",
                release_tag="5.1.0-ls2",
                title="5.1.0-ls2",
                body="LinuxServer Changes:\n- Rebase to Alpine 3.20",
                links=[
                    ReleaseNoteLinkData(
                        label="LSIO release",
                        url=(
                            "https://github.com/linuxserver/docker-radarr/"
                            "releases/tag/5.1.0-ls2"
                        ),
                        kind="lsio_release",
                    )
                ],
                classification=LSIOUpdateClassification(
                    change_type="image_rebuild",
                    reason="same-upstream-image-layer-changed",
                    current=LSIOTagParts(
                        raw="5.1.0-ls1",
                        kind="build",
                        upstream_version="5.1.0",
                        build_suffix="ls1",
                    ),
                    target=LSIOTagParts(
                        raw="5.1.0-ls2",
                        kind="build",
                        upstream_version="5.1.0",
                        build_suffix="ls2",
                    ),
                ),
            )
            for target in targets
        ]

    monkeypatch.setattr(
        notifications_module,
        "refresh_release_notes",
        fake_refresh_release_notes,
    )
    posted = _capture_discord_posts(monkeypatch)
    client = _client(tmp_path, _RELEASE_NOTIFICATION_ENV)
    _write_pending_lines(tmp_path, ["linuxserver/radarr:5.1.0-ls1 tag=5.1.0-ls2"])
    headers = _csrf_headers(client)

    preview = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=headers,
    )
    send = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=headers,
    )

    assert preview.status_code == 200
    assert send.status_code == 200
    preview_item = preview.json()["items"][0]
    assert preview_item["title"].startswith("LSIO image rebuild:")
    assert "5.1.0-ls2" in preview_item["title"]
    assert preview_item["image_repo"] == "linuxserver/docker-radarr"
    assert preview_item["upstream_repo"] == "Radarr/Radarr"
    assert "Repository: `linuxserver/docker-radarr`" in preview_item["description"]
    assert "Repository: `Radarr/Radarr`" not in preview_item["description"]
    assert "Upstream: `Radarr/Radarr`" in preview_item["description"]
    assert "LinuxServer.io rebuild" in preview_item["description"]
    assert "LSIO build: `ls2`" in preview_item["description"]
    assert preview_item["category"] == "worth_noting"
    assert preview_item["reason_code"] == "lsio_rebuild"
    assert posted[0][1]["content"] == preview.json()["messages"][0]
    assert "🟡 Worth noting" in posted[0][1]["content"]
    assert "LSIO image rebuild" in posted[0][1]["content"]
    assert "[LSIO release]" in posted[0][1]["content"]


def test_release_notification_send_posts_discord_payload_and_audits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, posted = _release_notification_client(tmp_path, monkeypatch)
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])

    response = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=_csrf_headers(client),
    )

    body = response.json()
    assert response.status_code == 200
    assert body["sent"] is True
    assert body["audit_run_id"]
    assert len(posted) == 1
    assert posted[0][0] == "https://discord.test/webhook-secret"
    assert body["items"][0]["title"] == "app Tag Update"
    assert posted[0][1]["content"] == body["messages"][0]
    assert "major version bump" in posted[0][1]["content"]
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        run = conn.execute(
            "SELECT * FROM update_runs WHERE id = ?",
            (body["audit_run_id"],),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM update_events WHERE run_id = ?",
            (body["audit_run_id"],),
        ).fetchone()

    run_metadata = json.loads(run["metadata_json"])
    event_metadata = json.loads(event["metadata_json"])
    assert run["mode"] == "web-release-notifications"
    assert run_metadata["destination"] == {
        "type": "discord",
        "source": "DISCORD_WEBHOOK",
    }
    assert run_metadata["sent_count"] == 1
    assert event_metadata["items"][0]["line_no"] == 1
    serialized = json.dumps({"run": run_metadata, "event": event_metadata})
    assert "webhook-secret" not in serialized


def test_release_notification_send_describes_digest_update_title(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, posted = _release_notification_client(tmp_path, monkeypatch)
    _write_pending_lines(
        tmp_path,
        [f"ghcr.io/acme/app:1.0.0 sha256={'a' * 64}"],
    )

    response = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert len(posted) == 1
    assert response.json()["items"][0]["title"] == "app Digest Update"
    assert response.json()["items"][0]["reason_code"] == "routine_digest"
    assert "image digest update" in posted[0][1]["content"]
    assert "sha256:" not in posted[0][1]["content"]


def test_release_notification_send_uses_persisted_discord_webhook(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_release_refresh(monkeypatch)
    posted = _capture_discord_posts(monkeypatch)
    webhook = "https://discord.com/api/webhooks/123/webhook-secret"
    _store_web_setting(
        tmp_path,
        "release_notifications.discord_webhook",
        webhook,
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_RELEASE_NOTES_ENABLED": "true",
        },
    )
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])

    response = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=_csrf_headers(client),
    )
    body = response.json()

    assert response.status_code == 200
    assert body["destination"] == {
        "type": "discord",
        "configured": True,
        "source": "WebUI settings",
    }
    assert posted[0][0] == webhook
    assert "webhook-secret" not in response.text


def test_release_notification_full_verbosity_includes_release_body(
    tmp_path: Path,
    monkeypatch,
) -> None:
    release_body = "Full release notes body from GitHub."
    _fake_release_refresh(monkeypatch, body=release_body)
    posted = _capture_discord_posts(monkeypatch)
    client = _client(tmp_path, _RELEASE_NOTIFICATION_ENV)
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])
    headers = _csrf_headers(client)

    summary = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=headers,
    )
    _store_web_setting(tmp_path, "release_notifications.verbosity", "full")
    full = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=headers,
    )

    assert summary.status_code == 200
    assert release_body not in summary.json()["items"][0]["description"]
    assert full.status_code == 200
    assert release_body in full.json()["items"][0]["description"]
    assert release_body not in posted[0][1]["content"]


def test_release_notification_per_container_mode_keeps_detailed_embed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    release_body = "Full release notes body from GitHub."
    _fake_release_refresh(monkeypatch, body=release_body)
    posted = _capture_discord_posts(monkeypatch)
    _store_web_setting(tmp_path, "release_notifications.mode", "per_container")
    _store_web_setting(tmp_path, "release_notifications.verbosity", "full")
    client = _client(tmp_path, _RELEASE_NOTIFICATION_ENV)
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])

    response = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "per_container"
    assert response.json()["batch_count"] == 1
    assert response.json()["messages"] == []
    assert "flags" not in posted[0][1]
    assert posted[0][1]["embeds"][0]["title"] == "app Tag Update"
    assert release_body in posted[0][1]["embeds"][0]["description"]


def test_release_notification_send_records_history_and_skips_duplicate_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, posted = _release_notification_client(tmp_path, monkeypatch)
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])
    headers = _csrf_headers(client)

    sent = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=headers,
    )
    preview = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=headers,
    )
    duplicate_send = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=headers,
    )

    sent_body = sent.json()
    preview_body = preview.json()
    assert sent.status_code == 200
    assert sent_body["items"][0]["notification_status"] == "new"
    assert sent_body["items"][0]["notification_key"]
    assert preview.status_code == 200
    assert preview_body["sendable_count"] == 0
    assert preview_body["skipped_count"] == 1
    assert preview_body["items"][0]["notification_status"] == "skipped_duplicate"
    assert preview_body["items"][0]["notification_send_count"] == 1
    assert preview_body["items"][0]["skipped_reason"] == "Already sent for this update."
    assert duplicate_send.status_code == 422
    assert len(posted) == 1

    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        history = conn.execute(
            """
            SELECT *
            FROM release_notification_history
            WHERE notification_key = ?
            """,
            (sent_body["items"][0]["notification_key"],),
        ).fetchone()

    assert history["status"] == "sent"
    assert history["send_count"] == 1
    assert history["last_audit_run_id"] == sent_body["audit_run_id"]
    assert "webhook-secret" not in history["metadata_json"]


def test_release_notification_send_reserves_history_before_posting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_release_refresh(monkeypatch)
    client = _client(tmp_path, _RELEASE_NOTIFICATION_ENV)
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])
    headers = _csrf_headers(client)
    posted: list[tuple[str, object]] = []
    duplicate_responses = []

    def fake_post_discord_payload(webhook_url: str, payload: object) -> None:
        if not posted:
            duplicate_responses.append(
                client.post(
                    "/api/v1/release-notifications/send",
                    json={"line_numbers": [1], "confirmation": "send-release-notes"},
                    headers=headers,
                )
            )
        posted.append((webhook_url, payload))

    monkeypatch.setattr(
        discord_module,
        "_post_discord_payload",
        fake_post_discord_payload,
    )

    response = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=headers,
    )

    assert response.status_code == 200
    assert duplicate_responses[0].status_code == 422
    assert len(posted) == 1


def test_release_notification_duplicate_key_survives_missing_wud_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_release_refresh(monkeypatch)
    _install_wud_api(
        monkeypatch,
        containers=[
            _wud_api_container(
                image="ghcr.io/acme/app",
                tag="1.0.0",
                remote_tag="2.0.0",
            )
        ],
        triggers={"docker.local.app": (200, [])},
    )
    monkeypatch.setattr(
        discord_module,
        "_post_discord_payload",
        lambda _url, _payload: None,
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_RELEASE_NOTES_ENABLED": "true",
            "DISCORD_WEBHOOK": "https://discord.test/webhook-secret",
            "WUD_API_BASE_URL": "https://wud.release-notifications.test:3000",
        },
    )
    raw = "ghcr.io/acme/app:1.0.0 tag=2.0.0"
    (tmp_path / "state" / "images.todo").write_text(f"{raw}\n", encoding="utf-8")
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        init_db(conn)
        run_id = insert_update_run(
            conn,
            started_at="2026-05-27T12:00:00+00:00",
            status="success",
            dry_run=False,
            mode="stop",
            wud_file="/out/images.todo",
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=1,
            raw=raw,
            image="ghcr.io/acme/app:1.0.0",
            desired_tag="2.0.0",
            service_key="media/app",
            status="resolved",
            status_reason="updated",
        )
    headers = _csrf_headers(client)

    sent = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=headers,
    )
    run_preview = client.post(
        "/api/v1/release-notifications/preview",
        json={"run_id": run_id},
        headers=headers,
    )

    assert sent.status_code == 200
    assert run_preview.status_code == 200
    assert run_preview.json()["sendable_count"] == 0
    assert run_preview.json()["items"][0]["notification_key"] == sent.json()["items"][0][
        "notification_key"
    ]
    assert run_preview.json()["items"][0]["notification_status"] == "skipped_duplicate"


def test_release_notification_remote_change_gets_new_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, _ = _release_notification_client(tmp_path, monkeypatch)
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])
    headers = _csrf_headers(client)

    sent = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=headers,
    )
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.1.0"])
    preview = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=headers,
    )

    assert sent.status_code == 200
    assert preview.status_code == 200
    assert preview.json()["sendable_count"] == 1
    assert preview.json()["items"][0]["notification_status"] == "new"
    assert preview.json()["items"][0]["notification_key"] != sent.json()["items"][0][
        "notification_key"
    ]


def test_release_notification_cooldown_policy_allows_after_cooldown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, _ = _release_notification_client(tmp_path, monkeypatch)
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        init_db(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO web_settings (key, value, updated_at)
                VALUES
                    (
                        'release_notifications.resend_policy',
                        'cooldown',
                        '2026-06-01T00:00:00+00:00'
                    ),
                    (
                        'release_notifications.cooldown_seconds',
                        '60',
                        '2026-06-01T00:00:00+00:00'
                    )
                """
            )
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])
    headers = _csrf_headers(client)
    sent = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=headers,
    )
    notification_key = sent.json()["items"][0]["notification_key"]

    with open_db(db_path) as conn:
        with conn:
            conn.execute(
                """
                UPDATE release_notification_history
                SET last_sent_at = '2999-01-01T00:00:00+00:00'
                WHERE notification_key = ?
                """,
                (notification_key,),
            )
    blocked = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=headers,
    )
    with open_db(db_path) as conn:
        with conn:
            conn.execute(
                """
                UPDATE release_notification_history
                SET last_sent_at = '2026-01-01T00:00:00+00:00'
                WHERE notification_key = ?
                """,
                (notification_key,),
            )
    allowed = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=headers,
    )

    assert sent.status_code == 200
    assert blocked.json()["sendable_count"] == 0
    assert blocked.json()["items"][0]["notification_status"] == "skipped_cooldown"
    assert allowed.json()["sendable_count"] == 1
    assert allowed.json()["items"][0]["notification_status"] == "cooldown_ready"


def test_verified_security_notification_ignores_cooldown_resend_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def verified_release(_conn, targets, _environ, **_kwargs):
        return [
            ReleaseNoteData(
                line_no=target.line_no,
                status="ready",
                provider="github",
                image_repo="acme/app",
                upstream_repo="acme/app",
                release_tag="2.0.0",
                title="Security patch",
                security=ReleaseSecurityAssessment(
                    outcome="verified_critical_high",
                    severity="high",
                    reason_code="verified_exposure",
                    reason="Verified High advisory affects 1.0.0 and is patched by 2.0.0.",
                    advisory_ids=["GHSA-aaaa-bbbb-cccc"],
                ),
            )
            for target in targets
        ]

    monkeypatch.setattr(
        notifications_module,
        "refresh_release_notes",
        verified_release,
    )
    _capture_discord_posts(monkeypatch)
    client = _client(tmp_path, _RELEASE_NOTIFICATION_ENV)
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        init_db(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO web_settings (key, value, updated_at)
                VALUES ('release_notifications.resend_policy', 'cooldown', ?)
                """,
                ("2026-06-01T00:00:00+00:00",),
            )
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])
    headers = _csrf_headers(client)
    sent = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=headers,
    )
    notification_key = sent.json()["items"][0]["notification_key"]
    with open_db(db_path) as conn:
        with conn:
            conn.execute(
                """
                UPDATE release_notification_history
                SET last_sent_at = '2020-01-01T00:00:00+00:00'
                WHERE notification_key = ?
                """,
                (notification_key,),
            )
    preview = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1]},
        headers=headers,
    )

    assert sent.status_code == 200
    assert sent.json()["items"][0]["category"] == "security_urgent"
    assert preview.json()["sendable_count"] == 0
    assert preview.json()["items"][0]["notification_status"] == "skipped_duplicate"


def test_release_notification_manual_resend_increments_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, _ = _release_notification_client(tmp_path, monkeypatch)
    _write_pending_lines(tmp_path, ["ghcr.io/acme/app:1.0.0 tag=2.0.0"])
    headers = _csrf_headers(client)
    first = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "confirmation": "send-release-notes"},
        headers=headers,
    )
    preview = client.post(
        "/api/v1/release-notifications/preview",
        json={"line_numbers": [1], "resend": True},
        headers=headers,
    )
    second = client.post(
        "/api/v1/release-notifications/send",
        json={"line_numbers": [1], "resend": True, "confirmation": "send-release-notes"},
        headers=headers,
    )

    assert first.status_code == 200
    assert preview.status_code == 200
    assert preview.json()["sendable_count"] == 1
    assert preview.json()["items"][0]["notification_status"] == "manual_resend"
    assert second.status_code == 200
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        history = conn.execute(
            """
            SELECT send_count
            FROM release_notification_history
            WHERE notification_key = ?
            """,
            (first.json()["items"][0]["notification_key"],),
        ).fetchone()
    assert history["send_count"] == 2


def test_release_notification_send_batches_discord_digest_by_content_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, posted = _release_notification_client(tmp_path, monkeypatch)
    lines = [
        f"ghcr.io/acme/app{i}:1.0.0 tag=2.0.0"
        for i in range(1, 26)
    ]
    _write_pending_lines(tmp_path, lines)

    response = client.post(
        "/api/v1/release-notifications/send",
        json={
            "line_numbers": list(range(1, 26)),
            "confirmation": "send-release-notes",
        },
        headers=_csrf_headers(client),
    )

    body = response.json()
    assert response.status_code == 200
    assert body["sendable_count"] == 25
    assert body["batch_count"] == len(posted)
    assert body["batch_count"] > 1
    assert body["messages"] == [payload["content"] for _url, payload in posted]
    assert all(
        payload["flags"] == notifications_module.DISCORD_SUPPRESS_EMBEDS_FLAG
        for _url, payload in posted
    )
    assert all(len(payload["content"]) <= 2000 for _url, payload in posted)
    assert sum(payload["content"].count("\n• ") for _url, payload in posted) == 25
    for _url, payload in posted:
        row_count = payload["content"].count("\n• ")
        assert payload["content"].startswith(
            f"🧾 WUDup batch — {row_count} updates found"
        )


def test_release_notification_send_audits_partial_discord_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_release_refresh(monkeypatch)
    posted = _capture_discord_posts(monkeypatch, fail_on=2)
    client = _client(tmp_path, _RELEASE_NOTIFICATION_ENV)
    lines = [
        f"ghcr.io/acme/app{i}:1.0.0 tag=2.0.0"
        for i in range(1, 26)
    ]
    _write_pending_lines(tmp_path, lines)

    response = client.post(
        "/api/v1/release-notifications/send",
        json={
            "line_numbers": list(range(1, 26)),
            "confirmation": "send-release-notes",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 500
    assert len(posted) == 2
    assert "webhook-secret" not in response.text
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        run = conn.execute(
            """
            SELECT *
            FROM update_runs
            WHERE mode = 'web-release-notifications'
            """,
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM update_events WHERE run_id = ?",
            (run["id"],),
        ).fetchone()
        history_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM release_notification_history
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        history_metadata = [
            row["metadata_json"]
            for row in conn.execute("SELECT metadata_json FROM release_notification_history")
        ]

    run_metadata = json.loads(run["metadata_json"])
    event_metadata = json.loads(event["metadata_json"])
    assert run["status"] == "failure"
    assert event["status"] == "failure"
    assert 0 < run_metadata["sent_count"] < 25
    assert run_metadata["sent_batch_count"] == 1
    assert run_metadata["batch_count"] > 1
    assert event_metadata["items"][0]["line_no"] == 1
    assert [(row["status"], row["count"]) for row in history_rows] == [
        ("failure", 25 - run_metadata["sent_count"]),
        ("sent", run_metadata["sent_count"]),
    ]
    serialized = json.dumps({"run": run_metadata, "event": event_metadata})
    assert "webhook-secret" not in serialized
    assert "webhook-secret" not in json.dumps(history_metadata)
