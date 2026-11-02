from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.web_test_helpers import (
    TEST_FILE_PENDING_SOURCE_VALUE,
    _fake_docker_calls,
    _fake_docker_env,
    _make_fake_stack,
    _setup_admin,
    _web_env,
)

from wudup import web_integration_summary as summary_module
from wudup.db import init_db, insert_update_event, insert_update_run, open_db
from wudup.web import create_app
from wudup.web_models import (
    PendingDiagnostic,
    PendingGroupedItem,
    PendingGrouping,
    PendingReleaseSkip,
    PendingResponse,
    PendingSourceInfo,
    PendingStackGroup,
    ReleaseNoteInfo,
    ReleaseNoteLink,
    ReleaseNotesResponse,
    ReleaseSecurityAssessment,
    RunEventRecord,
    RunHistorySummary,
    RunVerificationItem,
    RunVerificationSummary,
    SecurityScanComparison,
    SecurityScanFinding,
    SecurityScanInfo,
    SecurityScansResponse,
    ServicePolicyRecord,
    TrackedContainersResponse,
    WudApiStatus,
    WudContainerMetadata,
)

TOKEN = "r" * 40
NOW = datetime.now(UTC).replace(microsecond=0)
RECENT = (NOW - timedelta(hours=1)).isoformat()
OLD = (NOW - timedelta(days=3)).isoformat()


def _wud_metadata(semver_diff: str = "minor") -> WudContainerMetadata:
    return WudContainerMetadata(
        id="cid",
        name="app",
        display_name="app",
        status="running",
        watcher="local",
        local_tag="1.0",
        local_digest="",
        remote_tag="1.1",
        remote_digest="",
        update_kind="tag",
        semver_diff=semver_diff,
        link="",
        error="",
    )


def _item(
    line_no: int,
    repo: str,
    *,
    services: list[str],
    current: str = "1.0",
    target: str = "1.1",
    release_skip: PendingReleaseSkip | None = None,
    diagnostic: PendingDiagnostic | None = None,
    metadata_status: str = "fresh",
) -> PendingGroupedItem:
    return PendingGroupedItem(
        line_no=line_no,
        raw=f"{repo}:{current} tag={target}",
        image=f"{repo}:{current}",
        key=repo,
        repo=repo,
        current_tag=current,
        has_tag=True,
        allow_repo=False,
        digest="",
        desired_tag=target,
        wud_metadata=_wud_metadata(),
        metadata_status=metadata_status,
        resolved_image=f"{repo}:{current}",
        target_image=f"{repo}:{target}",
        services=services,
        action="tag-update",
        runtime_state="running",
        release_skip=release_skip,
        diagnostic=diagnostic,
    )


def _pending(
    groups: dict[str, list[PendingGroupedItem]],
    *,
    unmatched: list[PendingGroupedItem] | None = None,
    wud_state: str = "ready",
    degraded: bool = False,
) -> PendingResponse:
    items = [item for group in groups.values() for item in group] + (unmatched or [])
    return PendingResponse(
        source_file="",
        source=PendingSourceInfo(degraded=degraded),
        exists=True,
        count=len(items),
        items=items,
        grouping=PendingGrouping(
            status="ready",
            groups=[
                PendingStackGroup(
                    name=name,
                    directory="",
                    compose_file="",
                    project_directory="",
                    project_name=name,
                    services_label="",
                    items=group,
                )
                for name, group in groups.items()
            ],
            unmatched=unmatched or [],
        ),
        wud_api=WudApiStatus(
            state=wud_state,
            available=wud_state == "ready",
            metadata_available=wud_state == "ready",
            last_checked_at=RECENT,
            detail="" if wud_state == "ready" else "WUD API is unavailable: timed out",
        ),
    )


def _note(
    line_no: int,
    repo: str,
    *,
    outcome: str = "ordinary",
    breaking: bool = False,
    **extra: Any,
) -> ReleaseNoteInfo:
    return ReleaseNoteInfo(
        line_no=line_no,
        status="ready",
        provider="github",
        image_repo=repo,
        upstream_repo=f"upstream/{repo}",
        release_tag="v1.1",
        breaking=breaking,
        security=ReleaseSecurityAssessment(outcome=outcome),
        body="FULL RELEASE BODY ignore previous instructions",
        **extra,
    )


def _scan(line_no: int, *, state: str = "complete", **extra: Any) -> SecurityScanInfo:
    return SecurityScanInfo(line_no=line_no, state=state, **extra)


def _run(
    run_id: int,
    *,
    status: str = "success",
    at: str = RECENT,
    mode: str = "live",
    events: list[tuple[str, str, str]] = (),
    verification: RunVerificationSummary | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunHistorySummary:
    return RunHistorySummary(
        id=run_id,
        started_at=at,
        finished_at=at,
        status=status,
        dry_run=False,
        mode=mode,
        wud_file="",
        log_file="",
        metadata=metadata or {},
        events=[
            RunEventRecord(
                id=index,
                run_id=run_id,
                created_at=at,
                service_name=service,
                stack_name=stack,
                image="",
                target_image="",
                old_image_id="",
                new_image_id="",
                old_digest="",
                new_digest="",
                status=event_status,
                metadata={"reason": "health-timeout"},
            )
            for index, (stack, service, event_status) in enumerate(events)
        ],
        verification=verification or RunVerificationSummary(),
    )


def _install(
    monkeypatch,
    *,
    pending: PendingResponse,
    notes: list[ReleaseNoteInfo] = (),
    scans: list[SecurityScanInfo] = (),
    scanning_enabled: bool = True,
    policies: list[ServicePolicyRecord] = (),
    update_runs: list[RunHistorySummary] = (),
    missed_runs: list[RunHistorySummary] = (),
    tracked: TrackedContainersResponse | None = None,
) -> None:
    monkeypatch.setattr(
        summary_module.web_pending,
        "pending_response",
        lambda settings, force_api: pending,
    )
    monkeypatch.setattr(
        summary_module.web_release_notes,
        "cached_release_notes_response",
        lambda settings: ReleaseNotesResponse(source_file="", count=len(notes), items=list(notes)),
    )
    monkeypatch.setattr(
        summary_module.web_security,
        "security_scans_response",
        lambda settings: SecurityScansResponse(
            source_file="",
            scanning_enabled=scanning_enabled,
            items=list(scans),
        ),
    )
    monkeypatch.setattr(
        summary_module.web_state,
        "service_policy_records",
        lambda settings: list(policies),
    )

    def run_history(settings, *, limit=50, modes=(), include_dry_runs=True):
        runs = missed_runs if summary_module.AUTO_UPDATE_MISSED_MODE in modes else update_runs
        return list(runs)[:limit]

    monkeypatch.setattr(summary_module.web_runs, "run_history", run_history)
    monkeypatch.setattr(
        summary_module.web_tracking,
        "tracked_containers",
        lambda settings: tracked or TrackedContainersResponse(status="ready", count=0),
    )


def _skip(*, reason: str = "") -> PendingReleaseSkip:
    return PendingReleaseSkip(
        id=1,
        service_key="media/app",
        basis="version",
        value="1.1",
        scope="release",
        label="1.1",
        reason=reason,
        created_at=OLD,
        matches=True,
    )


def _policy(service_key: str, *, auto: bool = True) -> ServicePolicyRecord:
    return ServicePolicyRecord(
        service_key=service_key,
        update_mode="live",
        auto_update=auto,
        snooze_default_seconds=None,
        auto_update_time="03:00",
        auto_update_days=["sat"],
        created_at=OLD,
        updated_at=OLD,
    )


SETTINGS: Any = object()


@pytest.fixture(autouse=True)
def _plain_sanitizer(monkeypatch) -> None:
    # The unit tests pass a placeholder settings object; keep text as-is.
    monkeypatch.setattr(
        summary_module,
        "sanitize_support_bundle_text",
        lambda settings, value: value,
    )


def test_summary_joins_release_notes_scans_and_policies(monkeypatch) -> None:
    fixed = [SecurityScanFinding(severity="critical"), SecurityScanFinding(severity="high")]
    _install(
        monkeypatch,
        pending=_pending(
            {
                "media": [_item(1, "repo/jellyfin", services=["jellyfin"])],
                "tools": [_item(2, "repo/zeta", services=["zeta"])],
            }
        ),
        notes=[_note(1, "repo/jellyfin", outcome="verified_critical_high")],
        scans=[
            _scan(
                1,
                comparison=SecurityScanComparison(status="improved", fixed_findings=fixed),
            ),
            _scan(2, state="not_scanned"),
        ],
        policies=[_policy("media/jellyfin")],
    )

    body = summary_module.integration_summary(SETTINGS).model_dump()

    assert body["v"] == 1
    assert body["updates"]["items"][0] == {
        "service": "media/jellyfin",
        "current": "1.0",
        "target": "1.1",
        "kind": "minor",
        "breaking": False,
        "security": "critical_high",
        "fixes": {"critical": 1, "high": 1},
        "scan": "complete",
        "auto": True,
        "skipped": False,
    }
    second = body["updates"]["items"][1]
    assert second["service"] == "tools/zeta"
    assert second["security"] == "unknown"
    assert second["fixes"] is None
    assert second["scan"] == "not_scanned"
    assert "FULL RELEASE BODY" not in json.dumps(body)


def test_summary_caps_updates_and_sorts_security_then_breaking(monkeypatch) -> None:
    items = [_item(index, f"repo/app{index:02}", services=["app"]) for index in range(1, 26)]
    groups = {f"stack{index:02}": [item] for index, item in enumerate(items, start=1)}
    _install(
        monkeypatch,
        pending=_pending(groups),
        notes=[
            _note(25, "repo/app25", breaking=True),
            _note(24, "repo/app24", outcome="verified_critical_high"),
        ],
    )

    updates = summary_module.integration_summary(SETTINGS).updates

    assert updates.total == 25
    assert len(updates.items) == summary_module.MAX_UPDATES
    assert updates.more == 5
    assert [item.service for item in updates.items[:3]] == [
        "stack24/app",
        "stack25/app",
        "stack01/app",
    ]


def test_release_note_for_a_different_image_is_ignored(monkeypatch) -> None:
    _install(
        monkeypatch,
        pending=_pending({"media": [_item(1, "repo/jellyfin", services=["jellyfin"])]}),
        notes=[_note(1, "repo/other", outcome="verified_critical_high", breaking=True)],
    )

    update = summary_module.integration_summary(SETTINGS).updates.items[0]

    assert update.security == "unknown"
    assert update.breaking is False


def test_security_and_breaking_issues_have_stable_ids(monkeypatch) -> None:
    skip = _skip()
    _install(
        monkeypatch,
        pending=_pending(
            {
                "media": [_item(1, "repo/jellyfin", services=["jellyfin"])],
                "web": [_item(2, "repo/nginx", services=["nginx"])],
                "skip": [_item(3, "repo/skipped", services=["app"], release_skip=skip)],
            }
        ),
        notes=[
            _note(1, "repo/jellyfin", outcome="verified_critical_high"),
            _note(2, "repo/nginx", breaking=True),
            _note(3, "repo/skipped", outcome="verified_critical_high", breaking=True),
        ],
    )

    first = summary_module.integration_summary(SETTINGS)
    second = summary_module.integration_summary(SETTINGS)

    issues = [(issue.id, issue.severity, issue.code) for issue in first.issues]
    assert issues == [
        ("wudup:security-release:media/jellyfin:1.1", "urgent", "security-release"),
        ("wudup:breaking-update:web/nginx:1.1", "digest", "breaking-update"),
    ]
    assert [issue.id for issue in second.issues] == [issue for issue, _, _ in issues]
    skipped = {item.service: item.skipped for item in first.updates.items}
    assert skipped == {"skip/app": True, "media/jellyfin": False, "web/nginx": False}


def test_failed_run_is_urgent_when_recent_and_digest_when_old(monkeypatch) -> None:
    failed = _run(
        812,
        status="failure",
        events=[("media", "jellyfin", "failure"), ("media", "db", "success")],
        verification=RunVerificationSummary(
            status="needs_review",
            items=[
                RunVerificationItem(
                    line_no=1,
                    service_key="media/jellyfin",
                    health_status="timed_out",
                    summary="health check timed out after 120s",
                )
            ],
        ),
    )
    _install(monkeypatch, pending=_pending({}), update_runs=[failed])

    recent = summary_module.integration_summary(SETTINGS)

    assert recent.last_run is not None
    assert recent.last_run.model_dump() == {
        "id": 812,
        "at": RECENT,
        "mode": "live",
        "status": "failure",
        "services": 2,
        "failed": ["media/jellyfin"],
        "reason": "health check timed out after 120s",
    }
    assert [(issue.id, issue.severity) for issue in recent.issues] == [
        ("wudup:run-failed:812", "urgent")
    ]
    assert recent.issues[0].summary == "media/jellyfin: health check timed out after 120s"

    _install(
        monkeypatch,
        pending=_pending({}),
        update_runs=[failed.model_copy(update={"started_at": OLD, "finished_at": OLD})],
    )
    assert summary_module.integration_summary(SETTINGS).issues[0].severity == "digest"


def test_successful_run_with_unhealthy_service_counts_as_failure(monkeypatch) -> None:
    run = _run(
        9,
        events=[("media", "app", "success")],
        verification=RunVerificationSummary(
            status="needs_review",
            items=[RunVerificationItem(line_no=1, service_key="media/app", health_status="failed")],
        ),
    )
    review = _run(10, verification=RunVerificationSummary(status="needs_review"))
    _install(monkeypatch, pending=_pending({}), update_runs=[run])
    assert summary_module.integration_summary(SETTINGS).last_run.status == "failure"

    _install(monkeypatch, pending=_pending({}), update_runs=[review])
    summary = summary_module.integration_summary(SETTINGS)
    assert summary.last_run.status == "needs_review"
    assert summary.issues == []


def test_wud_unreachable_is_urgent_and_stale_data_is_digest(monkeypatch) -> None:
    _install(monkeypatch, pending=_pending({}, wud_state="unavailable"))
    down = summary_module.integration_summary(SETTINGS)

    assert down.wud.model_dump() == {
        "state": "unavailable",
        "fresh": True,
        "detail": "WUD API is unavailable: timed out",
    }
    assert [(issue.id, issue.severity) for issue in down.issues] == [
        ("wudup:wud-unreachable", "urgent")
    ]

    stale_item = _item(1, "repo/app", services=["app"], metadata_status="retained")
    _install(monkeypatch, pending=_pending({"media": [stale_item]}))
    stale = summary_module.integration_summary(SETTINGS)
    assert [(issue.id, issue.severity) for issue in stale.issues] == [
        ("wudup:wud-stale", "digest")
    ]
    assert stale.wud.detail == ""


def test_missed_auto_update_unplaced_and_broken_scans_are_digest(monkeypatch) -> None:
    unmatched = _item(
        4,
        "repo/orphan",
        services=[],
        diagnostic=PendingDiagnostic(code="no-compose-match", message="No Compose service uses repo/orphan"),
    )
    _install(
        monkeypatch,
        pending=_pending({}, unmatched=[unmatched]),
        scans=[_scan(4, state="auth_required", error_message="registry login required")],
        missed_runs=[
            _run(
                30,
                status="missed",
                mode="auto-update-missed",
                metadata={
                    "service_keys": ["media/app"],
                    "slots": [{"reason": "another update job was running"}],
                },
            )
        ],
    )

    summary = summary_module.integration_summary(SETTINGS)

    assert summary.updates.unplaced == 1
    assert summary.updates.items[0].service == "repo/orphan"
    assert {issue.code: (issue.severity, issue.summary) for issue in summary.issues} == {
        "auto-update-missed": ("digest", "media/app: another update job was running"),
        "update-unplaced": (
            "digest",
            (
                "1 update(s) could not be matched to a Compose service: "
                "No Compose service uses repo/orphan"
            ),
        ),
        "scan-broken": ("digest", "1 security scan(s) failed: registry login required"),
    }

    _install(
        monkeypatch,
        pending=_pending({}),
        scans=[_scan(4, state="error")],
        scanning_enabled=False,
        missed_runs=[_run(31, status="missed", mode="auto-update-missed", at=OLD)],
    )
    assert summary_module.integration_summary(SETTINGS).issues == []


def test_issues_are_capped_with_urgent_first(monkeypatch) -> None:
    items = [_item(index, f"repo/app{index:02}", services=["app"]) for index in range(1, 15)]
    _install(
        monkeypatch,
        pending=_pending({f"s{index:02}": [item] for index, item in enumerate(items, 1)}),
        notes=[_note(index, f"repo/app{index:02}", breaking=True) for index in range(1, 14)]
        + [_note(14, "repo/app14", outcome="verified_critical_high")],
    )

    summary = summary_module.integration_summary(SETTINGS)

    assert len(summary.issues) == summary_module.MAX_ISSUES
    assert summary.issues_more == 4
    assert summary.issues[0].code == "security-release"


def test_service_detail_caps_and_cleans_untrusted_text(monkeypatch) -> None:
    long_title = "Big\x1b[31m release\n" + "x" * 200
    note = _note(
        1,
        "repo/jellyfin",
        outcome="verified_critical_high",
        breaking=True,
        title=long_title,
        breaking_reasons=[f"reason {index}" for index in range(6)],
        links=[
            ReleaseNoteLink(label="evil", url="javascript:alert(1)", kind="release"),
            ReleaseNoteLink(label="release", url="https://github.com/up/rel", kind="release"),
        ],
    )
    note.security.advisory_ids = [f"GHSA-{index}" for index in range(8)]
    skip = _skip(reason="waiting for fix")
    _install(
        monkeypatch,
        pending=_pending(
            {"media": [_item(1, "repo/jellyfin", services=["jellyfin"], release_skip=skip)]}
        ),
        notes=[note],
        scans=[_scan(1, comparison=SecurityScanComparison(status="worse"))],
        policies=[_policy("media/jellyfin")],
        update_runs=[_run(5, events=[("media", "jellyfin", "success")])],
    )

    detail = summary_module.integration_service(SETTINGS, "media", "jellyfin").model_dump()

    release = detail["release"]
    assert len(release["title"]) == summary_module.TITLE_LIMIT
    assert "\x1b" not in release["title"] and "\n" not in release["title"]
    assert release["title"].endswith("…")
    assert release["breaking_reasons"] == ["reason 0", "reason 1", "reason 2"]
    assert release["advisories"] == [f"GHSA-{index}" for index in range(5)]
    assert release["link"] == "https://github.com/up/rel"
    assert detail["scan"]["comparison"] == "worse"
    assert detail["policy"] == {"mode": "live", "auto": True, "time": "03:00", "days": ["sat"]}
    assert detail["snooze"] == {
        "kind": "release",
        "until": "",
        "label": "1.1",
        "reason": "waiting for fix",
    }
    assert detail["last_run"]["id"] == 5
    assert detail["update_available"] is True
    assert "FULL RELEASE BODY" not in json.dumps(detail)


def test_unknown_service_raises_not_found(monkeypatch) -> None:
    _install(monkeypatch, pending=_pending({}))

    with pytest.raises(summary_module.IntegrationServiceNotFound, match="media/nope"):
        summary_module.integration_service(SETTINGS, "media", "nope")


def test_summary_route_reads_real_state_without_mutating(tmp_path: Path) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    env = _web_env(
        tmp_path,
        {
            "WUD_PENDING_SOURCE": TEST_FILE_PENDING_SOURCE_VALUE,
            "WUD_WEB_READONLY_TOKEN": TOKEN,
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    (tmp_path / "state" / "images.todo").write_text("repo/app:1.0 tag=1.1\n", encoding="utf-8")
    _make_fake_stack(tmp_path, fake_root, "media", [("app", "repo/app:1.0", "cid-1")])
    app = create_app(environ=env)
    _setup_admin(TestClient(app))
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        init_db(conn)
        failed = insert_update_run(conn, started_at=RECENT, status="failure", mode="live")
        insert_update_event(
            conn,
            run_id=failed,
            service_name="app",
            stack_name="media",
            image="repo/app:1.0",
            status="failure",
            metadata_json='{"reason": "health-timeout"}',
        )
        # Newer dry runs and audit rows must not hide the last real update run.
        insert_update_run(conn, started_at=RECENT, status="success", mode="live", dry_run=True)
        insert_update_run(conn, started_at=RECENT, status="success", mode="web-auth")
    before = db_path.read_bytes()
    (fake_root / "calls.log").write_text("", encoding="utf-8")
    reader = TestClient(app)
    headers = {"Authorization": f"Bearer {TOKEN}"}

    summary = reader.get("/api/integrations/v1/summary", headers=headers)
    detail = reader.get("/api/integrations/v1/services/media/app", headers=headers)

    assert summary.status_code == 200
    body = summary.json()
    assert body["updates"]["items"][0]["service"] == "media/app"
    assert body["last_run"]["id"] == failed
    assert body["last_run"]["failed"] == ["media/app"]
    assert "wudup:run-failed:" + str(failed) in {issue["id"] for issue in body["issues"]}
    assert detail.status_code == 200
    assert detail.json()["last_run"]["id"] == failed
    # Readers may create empty WAL/shm files, but nothing may be written.
    assert db_path.read_bytes() == before
    wal = db_path.with_name("wud.sqlite-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    calls = _fake_docker_calls(fake_root)
    for verb in (" pull", " up ", " restart", " rm ", " stop ", " tag "):
        assert verb not in calls
