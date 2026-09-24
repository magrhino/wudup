from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from tests.web_test_helpers import (
    _client,
    _insert_run,
)

from wudup import web_runs as runs_module
from wudup.db import (
    init_db,
    insert_pending_update,
    insert_update_event,
    insert_update_run,
    open_db,
)
from wudup.digest_provenance import DigestTagProvenance
from wudup.updater_models import STALE_PENDING_DIGEST_REASON
from wudup.web_models import LogTail

EMPTY_VERIFICATION = {
    "status": "verified",
    "total_count": 0,
    "verified_count": 0,
    "needs_review_count": 0,
    "items": [],
}


def test_runs_list_returns_empty_without_creating_missing_database(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    db_path = root / "wud.sqlite"
    client = _client(
        tmp_path,
        {"WUD_WEB_DEV_NO_AUTH": "true"},
        create_root=False,
    )

    response = client.get("/api/v1/runs")

    assert response.status_code == 200
    assert response.json() == []
    assert not root.exists()
    assert not db_path.exists()


def test_runs_database_errors_are_sanitized(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "github-secret-token"
    leaked_path = tmp_path / "state" / "wud.sqlite"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "GITHUB_TOKEN": secret,
        },
    )

    def fail_connect(_settings: object) -> object:
        raise OSError(f"cannot open {leaked_path} with {secret}")

    monkeypatch.setattr(runs_module, "_connect_readonly_db", fail_connect)

    response = client.get("/api/v1/runs")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail.startswith("could not read database: ")
    assert str(leaked_path) not in detail
    assert secret not in detail
    assert "[REDACTED_PATH]" in detail
    assert "<redacted>" in detail


def test_run_detail_returns_not_found_without_creating_missing_database(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    db_path = root / "wud.sqlite"
    client = _client(
        tmp_path,
        {"WUD_WEB_DEV_NO_AUTH": "true"},
        create_root=False,
    )

    response = client.get("/api/v1/runs/1")

    assert response.status_code == 404
    assert response.json()["detail"] == "run not found"
    assert not root.exists()
    assert not db_path.exists()


def test_runs_endpoints_read_existing_sqlite_state(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        init_db(conn)
        run_id = insert_update_run(
            conn,
            started_at="2026-05-27T12:00:00+00:00",
            status="success",
            dry_run=True,
            mode="stop",
            wud_file="/out/images.todo",
            log_file="/logs/run.log",
            metadata_json='{"source":"test"}',
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=1,
            raw="nginx:1.25",
            image="nginx:1.25",
            status="success",
        )
        insert_update_event(
            conn,
            run_id=run_id,
            service_name="web",
            image="nginx:1.25",
            status="success",
        )
        run_id_2 = insert_update_run(
            conn,
            started_at="2026-05-27T13:00:00+00:00",
            status="success",
            dry_run=False,
            mode="auto-update",
            wud_file="/out/images.todo",
            log_file="/logs/run2.log",
        )
        insert_update_event(
            conn,
            run_id=run_id_2,
            service_name="db",
            image="postgres:15",
            status="success",
        )

    runs_response = client.get("/api/v1/runs")
    detail_response = client.get(f"/api/v1/runs/{run_id}")

    assert runs_response.status_code == 200
    runs_data = runs_response.json()
    assert len(runs_data) == 2

    # Verify the batched event mapping works correctly
    run_2 = next(r for r in runs_data if r["id"] == run_id_2)
    assert run_2["events"][0]["service_name"] == "db"
    run_1 = next(r for r in runs_data if r["id"] == run_id)
    assert run_1["events"][0]["service_name"] == "web"

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["metadata"] == {"source": "test"}
    assert detail["pending_updates"][0]["image"] == "nginx:1.25"
    assert detail["events"][0]["service_name"] == "web"
    assert detail["verification"] == EMPTY_VERIFICATION
    listed = client.get("/api/v1/runs").json()
    assert next(run for run in listed if run["id"] == detail["id"])["verification"] == EMPTY_VERIFICATION


def test_run_detail_derives_persistent_verification_summary(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        init_db(conn)
        run_id = insert_update_run(
            conn,
            started_at="2026-05-27T12:00:00+00:00",
            status="failure",
            dry_run=False,
            mode="stop",
            wud_file="/out/images.todo",
            log_file="/logs/run.log",
        )
        cases = [
            {
                "line_no": 1,
                "service": "verified",
                "pending_status": "resolved",
                "pending_reason": "updated",
                "event_status": "success",
                "event_metadata": {"reason": "updated"},
                "old_image_id": "sha256:old-verified",
                "new_image_id": "sha256:new-verified",
                "old_digest": "sha256:old-verified",
                "new_digest": "sha256:new-verified",
            },
            {
                "line_no": 2,
                "service": "current",
                "pending_status": "resolved",
                "pending_reason": "already-current",
                "event_status": "success",
                "event_metadata": {"reason": "already-current"},
            },
            {
                "line_no": 3,
                "service": "timeout",
                "pending_status": "failed",
                "pending_reason": "health-failed",
                "event_status": "failure",
                "event_metadata": {"reason": "health-failed"},
            },
            {
                "line_no": 4,
                "service": "disappeared",
                "pending_status": "failed",
                "pending_reason": "health-failed",
                "event_status": "failure",
                "event_metadata": {
                    "reason": "health-failed",
                    "health_evidence": "service_disappeared",
                },
            },
            {
                "line_no": 5,
                "service": "stale",
                "pending_status": "failed",
                "pending_reason": STALE_PENDING_DIGEST_REASON,
                "event_status": "",
                "event_metadata": {},
            },
            {
                "line_no": 6,
                "service": "restored",
                "pending_status": "failed",
                "pending_reason": "pull-failed",
                "event_status": "failure",
                "event_metadata": {"reason": "pull-failed"},
            },
        ]
        for case in cases:
            service = case["service"]
            line_no = int(case["line_no"])
            insert_pending_update(
                conn,
                run_id=run_id,
                line_no=line_no,
                raw=f"repo/{service}:1.0",
                image=f"repo/{service}:1.0",
                service_key=f"media/{service}",
                stack_name="media",
                service_name=service,
                status=str(case["pending_status"]),
                status_reason=str(case["pending_reason"]),
            )
            if case["event_status"]:
                insert_update_event(
                    conn,
                    run_id=run_id,
                    service_name=service,
                    stack_name="media",
                    image=f"repo/{service}:1.0",
                    target_image=f"repo/{service}:1.1",
                    old_image_id=str(case.get("old_image_id", "")),
                    new_image_id=str(case.get("new_image_id", "")),
                    old_digest=str(case.get("old_digest", "")),
                    new_digest=str(case.get("new_digest", "")),
                    status=str(case["event_status"]),
                    metadata_json=json.dumps(case["event_metadata"], sort_keys=True),
                )

    response = client.get(f"/api/v1/runs/{run_id}")

    assert response.status_code == 200
    verification = response.json()["verification"]
    listed = client.get("/api/v1/runs").json()
    assert next(run for run in listed if run["id"] == run_id)["verification"] == verification
    assert verification["status"] == "needs_review"
    assert verification["total_count"] == 6
    assert verification["verified_count"] == 2
    assert verification["needs_review_count"] == 4
    items = {item["line_no"]: item for item in verification["items"]}
    assert items[1]["image_status"] == "new_image_running"
    assert items[1]["container_status"] == "recreated"
    assert items[1]["health_status"] == "passed"
    assert items[1]["wud_status"] == "removed"
    assert items[1]["follow_up_needed"] is False
    assert items[2]["image_status"] == "already_current"
    assert items[2]["container_status"] == "skipped"
    assert items[2]["health_status"] == "skipped"
    assert items[2]["follow_up_needed"] is False
    assert items[3]["health_status"] == "timed_out"
    assert items[3]["wud_status"] == "restored"
    assert items[4]["health_status"] == "service_disappeared"
    assert items[5]["wud_status"] == "stale_removed"
    assert items[6]["wud_status"] == "restored"


@pytest.mark.parametrize(
    ("pending_stack", "pending_service", "event_identities", "expected_health", "sibling_stack"),
    [
        ("media", "db", [("home", "db")], "unknown", ""),
        ("media", "db", [("home", "other")], "unknown", ""),
        ("media", "db", [("media", "other")], "unknown", ""),
        ("media", "db", [("home", "db"), ("media", "db")], "passed", ""),
        ("media", "db", [("", "db")], "passed", ""),
        ("media", "db", [("", "")], "passed", ""),
        ("", "db", [("home", "db"), ("media", "db")], "unknown", ""),
        ("", "", [("home", "db"), ("media", "other")], "unknown", ""),
        ("home", "db", [("", "db")], "unknown", "media"),
        ("home", "db", [("", "")], "unknown", "media"),
        ("home", "db", [("", "db")], "passed", "home"),
    ],
)
def test_run_verification_keeps_evidence_with_its_service(
    tmp_path: Path,
    pending_stack: str,
    pending_service: str,
    event_identities: list[tuple[str, str]],
    expected_health: str,
    sibling_stack: str,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        run_id = insert_update_run(
            conn,
            started_at="2026-05-27T12:00:00+00:00",
            status="success",
            dry_run=False,
            mode="stop",
            wud_file="/out/images.todo",
            log_file="",
        )
        insert_pending_update(
            conn, run_id=run_id, line_no=1, raw="postgres:16", image="postgres:16",
            stack_name=pending_stack, service_name=pending_service,
            status="resolved", status_reason="updated",
        )
        if sibling_stack:
            insert_pending_update(
                conn, run_id=run_id, line_no=2, raw="postgres:16", image="postgres:16",
                stack_name=sibling_stack, service_name=pending_service,
                status="resolved", status_reason="updated",
            )
        for stack, service in event_identities:
            insert_update_event(
                conn, run_id=run_id, stack_name=stack, service_name=service,
                image="postgres:16", target_image="postgres:17", status="success",
                metadata_json='{"reason":"updated"}',
            )

    detail_response = client.get(f"/api/v1/runs/{run_id}")
    history_response = client.get("/api/v1/runs")
    assert detail_response.status_code == history_response.status_code == 200
    verification = detail_response.json()["verification"]
    assert history_response.json()[0]["verification"] == verification
    assert all(item["health_status"] == expected_health for item in verification["items"])
    item = verification["items"][0]
    assert item["health_status"] == expected_health
    if expected_health == "passed":
        matched_event = next(event for event in detail_response.json()["events"] if event["id"] == item["event_id"])
        assert matched_event["stack_name"] in ("", pending_stack)
        assert matched_event["service_name"] in ("", pending_service)
    else:
        assert item["event_id"] is None
    assert item["image_status"] == ("new_image_running" if expected_health == "passed" else "unknown")
    assert item["target_image"] == ("postgres:17" if expected_health == "passed" else "")
    assert item["follow_up_needed"] is (expected_health == "unknown")


@pytest.mark.parametrize("runtime_after,expected_health", [("not-running", "skipped"), ("running", "passed"), ("", "passed")])
def test_run_verification_preserves_stopped_service_health_evidence(
    tmp_path: Path, runtime_after: str, expected_health: str,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        run_id = insert_update_run(conn, status="success", dry_run=False, mode="stop", wud_file="", log_file="")
        for line_no, service in enumerate(("stopped", "running"), start=1):
            insert_pending_update(
                conn, run_id=run_id, line_no=line_no, raw="app:1", image="app:1",
                stack_name="home", service_name=service, status="resolved", status_reason="updated",
            )
            metadata = {"reason": "updated", "stopped_services_before": ["stopped"], "stopped_services_after": ["stopped"]}
            if service == "stopped" and runtime_after:
                metadata.update(runtime_state_before="not-running", runtime_state_after=runtime_after)
            insert_update_event(
                conn, run_id=run_id, stack_name="home", service_name=service,
                image="app:1", target_image="app:2", status="success", metadata_json=json.dumps(metadata),
            )
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    history = client.get("/api/v1/runs").json()[0]
    assert history["verification"] == detail["verification"]
    assert [item["health_status"] for item in detail["verification"]["items"]] == [expected_health, "passed"]


@pytest.mark.parametrize("record_count", [200, 201, 10000])
def test_history_bounds_verification_reads_and_preserves_full_run_detail(
    tmp_path: Path, monkeypatch, record_count: int,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        run_id = insert_update_run(conn, status="failure", dry_run=False, mode="stop", wud_file="", log_file="")
        with conn:
            conn.executemany(
                "INSERT INTO pending_updates (run_id,line_no,raw,image,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                [(run_id, line, "app:1", "app:1", "pending", "", "") for line in range(record_count)],
            )
        small_id = insert_update_run(conn, status="success", dry_run=False, mode="stop", wud_file="", log_file="")
        insert_pending_update(conn, run_id=small_id, line_no=1, raw="small:1", image="small:1")

    converted_run_ids = []
    original = runs_module._pending_update_from_row

    def track_pending_read(row):
        converted_run_ids.append(row["run_id"])
        return original(row)

    monkeypatch.setattr(runs_module, "_pending_update_from_row", track_pending_read)
    response = client.get("/api/v1/runs")
    assert response.status_code == 200
    listed = {run["id"]: run for run in response.json()}
    history = listed[run_id]
    assert listed[small_id]["verification_omitted_count"] == 0
    assert len(listed[small_id]["verification"]["items"]) == 1
    assert history["verification"]["total_count"] == record_count
    if record_count > runs_module.MAX_HISTORY_VERIFICATION_ITEMS:
        assert converted_run_ids == [small_id]
        assert history["verification_omitted_count"] == record_count
        assert history["verification"] == {
            "status": "needs_review", "total_count": record_count,
            "verified_count": 0, "needs_review_count": record_count, "items": [],
        }
        assert len(response.content) < 2000
    else:
        assert history["verification_omitted_count"] == 0
        assert len(history["verification"]["items"]) == record_count

    detail_response = client.get(f"/api/v1/runs/{run_id}")
    assert detail_response.status_code == 200
    assert len(detail_response.json()["verification"]["items"]) == record_count


@pytest.mark.parametrize("record_empty_target_event", [False, True])
def test_run_verification_leaves_unrecorded_target_unknown(
    tmp_path: Path,
    record_empty_target_event: bool,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        run_id = insert_update_run(
            conn, started_at="2026-05-27T12:00:00+00:00", status="failure",
            dry_run=False, mode="stop", wud_file="/out/images.todo", log_file="",
        )
        insert_pending_update(
            conn, run_id=run_id, line_no=1,
            raw="repo/app:1.0 tag=1.1", image="repo/app:1.0", desired_tag="1.1",
            target_digest="sha256:requested", stack_name="home", service_name="app",
            status="failed", status_reason="preflight-failed",
        )
        if record_empty_target_event:
            insert_update_event(
                conn, run_id=run_id, stack_name="home", service_name="app",
                image="repo/app:1.0", target_image="", status="failure",
            )

    detail_response = client.get(f"/api/v1/runs/{run_id}")
    history_response = client.get("/api/v1/runs")
    assert detail_response.status_code == history_response.status_code == 200
    detail = detail_response.json()
    assert detail["pending_updates"][0]["desired_tag"] == "1.1"
    assert detail["pending_updates"][0]["target_digest"] == "sha256:requested"
    assert history_response.json()[0]["verification"] == detail["verification"]
    item = detail["verification"]["items"][0]
    assert item["image"] == "repo/app:1.0"
    assert item["target_image"] == ""


def test_run_detail_skips_verification_for_non_updater_audit_modes(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
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
            log_file="",
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=1,
            raw="repo/old:latest",
            image="repo/old:latest",
            service_key="",
            stack_name="",
            service_name="",
            status="resolved",
            status_reason="removed-unmatched",
        )
        insert_update_event(
            conn,
            run_id=run_id,
            service_name="repo/old:latest",
            image="repo/old:latest",
            status="success",
            metadata_json='{"operation":"remove_unmatched_pending"}',
        )

    response = client.get(f"/api/v1/runs/{run_id}")

    assert response.status_code == 200
    detail = response.json()
    assert detail["mode"] == "web-pending-cleanup"
    assert detail["verification"] == EMPTY_VERIFICATION
    listed = client.get("/api/v1/runs").json()
    assert next(run for run in listed if run["id"] == detail["id"])["verification"] == EMPTY_VERIFICATION


def test_runs_endpoints_serialize_digest_provenance(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    db_path = tmp_path / "state" / "wud.sqlite"
    provenance = DigestTagProvenance(
        source_image="repo/app:latest",
        resolved_tag="latest",
        watch_tag="latest",
        target_digest="sha256:new",
        final_image="repo/app@sha256:new",
        provenance_source="apply",
        provenance_confidence="verified",
    )
    expected_provenance = {
        "source_image": "repo/app:latest",
        "resolved_tag": "latest",
        "watch_tag": "latest",
        "target_digest": "sha256:new",
        "final_image": "repo/app@sha256:new",
        "provenance_source": "apply",
        "provenance_confidence": "verified",
    }
    with open_db(db_path) as conn:
        init_db(conn)
        run_id = insert_update_run(
            conn,
            started_at="2026-05-27T12:00:00+00:00",
            status="success",
            dry_run=False,
            mode="apply",
            wud_file="/out/images.todo",
            log_file="/logs/run.log",
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=1,
            raw="repo/app:latest@sha256:new",
            image="repo/app:latest",
            target_digest="sha256:new",
            service_key="stack/app",
            stack_name="stack",
            service_name="app",
            status="success",
            digest_provenance=provenance,
        )
        insert_update_event(
            conn,
            run_id=run_id,
            service_name="app",
            stack_name="stack",
            image="repo/app:latest",
            target_image="repo/app@sha256:new",
            new_digest="sha256:new",
            status="success",
            digest_provenance=provenance,
        )

    runs_response = client.get("/api/v1/runs")
    detail_response = client.get(f"/api/v1/runs/{run_id}")

    assert runs_response.status_code == 200
    assert detail_response.status_code == 200
    run = runs_response.json()[0]
    detail = detail_response.json()
    assert run["events"][0]["digest_provenance"] == expected_provenance
    assert detail["events"][0]["digest_provenance"] == expected_provenance
    assert detail["pending_updates"][0]["digest_provenance"] == expected_provenance


def test_runs_endpoints_sanitize_run_and_event_metadata(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    db_path = tmp_path / "state" / "wud.sqlite"
    wud_file = tmp_path / "state" / "images.todo"
    log_file = tmp_path / "state" / "logs" / "run.log"
    compose_file = tmp_path / "docker" / "media" / "compose.yml"
    with open_db(db_path) as conn:
        init_db(conn)
        run_id = insert_update_run(
            conn,
            started_at="2026-05-27T12:00:00+00:00",
            status="success",
            dry_run=False,
            mode="web-settings",
            wud_file=str(wud_file),
            log_file=str(log_file),
            metadata_json=json.dumps(
                {
                    "source": "webui",
                    "path": str(wud_file),
                    "nested": {"stack": str(compose_file.parent)},
                }
            ),
        )
        insert_pending_update(
            conn,
            run_id=run_id,
            line_no=1,
            raw="nginx:1.25",
            image="nginx:1.25",
            status="success",
            metadata_json=json.dumps(
                {
                    "log": str(log_file),
                    "target": {"compose_file": str(compose_file)},
                }
            ),
        )
        insert_update_event(
            conn,
            run_id=run_id,
            service_name="settings",
            stack_name="webui",
            image="managed-settings",
            status="success",
            metadata_json=json.dumps(
                {
                    "operation": "update_managed_settings",
                    "before": {"compose_file": str(compose_file)},
                }
            ),
        )

    runs_response = client.get("/api/v1/runs")
    detail_response = client.get(f"/api/v1/runs/{run_id}")

    assert runs_response.status_code == 200
    assert detail_response.status_code == 200
    run = runs_response.json()[0]
    detail = detail_response.json()

    for payload in (run, detail):
        assert payload["metadata"]["path"] == "<WUD_OUT_FILE>"
        assert payload["metadata"]["nested"]["stack"] == "<DOCKER_BASE>/media"
        assert (
            payload["events"][0]["metadata"]["before"]["compose_file"]
            == "<DOCKER_BASE>/media/compose.yml"
        )
    assert detail["pending_updates"][0]["metadata"]["log"] == "<WUD_LOG_DIR>/run.log"
    assert (
        detail["pending_updates"][0]["metadata"]["target"]["compose_file"]
        == "<DOCKER_BASE>/media/compose.yml"
    )


def test_run_log_endpoint_tails_configured_log_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "state" / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "run.log"
    log_file.write_text("0123456789", encoding="utf-8")
    run_id = _insert_run(tmp_path, log_file=str(log_file))
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get(f"/api/v1/runs/{run_id}/log?tail_bytes=4")

    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert body["content"] == "6789"
    assert body["truncated"] is True
    assert body["max_bytes"] == 4


def test_run_log_endpoint_caps_tail_size(tmp_path: Path) -> None:
    log_dir = tmp_path / "state" / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "run.log"
    log_file.write_text("log", encoding="utf-8")
    run_id = _insert_run(tmp_path, log_file=str(log_file))
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get(f"/api/v1/runs/{run_id}/log?tail_bytes=9999999")

    assert response.status_code == 200
    assert response.json()["max_bytes"] == 1_048_576


def test_run_log_endpoint_uses_runs_module_tail_reader_seam(
    tmp_path: Path,
    monkeypatch,
) -> None:
    log_dir = tmp_path / "state" / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "run.log"
    log_file.write_text("original", encoding="utf-8")
    run_id = _insert_run(tmp_path, log_file=str(log_file))
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    def fake_read_log_tail(
        _log_path: Path,
        _max_bytes: int,
    ) -> LogTail:
        return LogTail(
            exists=True,
            content="web tail seam used",
            truncated=False,
        )

    monkeypatch.setattr(runs_module, "_read_log_tail", fake_read_log_tail)

    response = client.get(f"/api/v1/runs/{run_id}/log?tail_bytes=4")

    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert body["content"] == "web tail seam used"
    assert body["truncated"] is False
    assert body["max_bytes"] == 4


def test_run_log_endpoint_rejects_missing_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "state" / "logs" / "missing.log"
    run_id = _insert_run(tmp_path, log_file=str(log_file))
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get(f"/api/v1/runs/{run_id}/log")

    assert response.status_code == 404
    assert response.json()["detail"] == "log file not found"


def test_run_log_endpoint_rejects_logs_outside_configured_dir(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.log"
    outside.write_text("secret", encoding="utf-8")
    run_id = _insert_run(tmp_path, log_file=str(outside))
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get(f"/api/v1/runs/{run_id}/log")

    assert response.status_code == 403
    assert response.json()["detail"] == "log file is outside WUD_LOG_DIR"


def test_safe_log_path_uses_resolved_path_after_symlink_swap(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    log_dir = tmp_path / "state" / "logs"
    log_dir.mkdir(parents=True)
    allowed = log_dir / "allowed.log"
    outside = tmp_path / "outside.log"
    link = log_dir / "run.log"
    allowed.write_text("allowed", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    link.symlink_to(allowed)

    log_path = runs_module._safe_log_path(client.app.state.web_settings, "run.log")
    link.unlink()
    link.symlink_to(outside)
    tail = runs_module._read_log_tail(log_path, 1024)

    assert log_path == allowed.resolve()
    assert tail.exists is True
    assert tail.content == "allowed"


def test_read_log_tail_hides_os_error_detail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    log_path = tmp_path / "state" / "logs" / "run.log"

    def fail_is_file(_path: Path) -> bool:
        raise OSError(f"permission denied: {tmp_path / 'private.log'}")

    monkeypatch.setattr(Path, "is_file", fail_is_file)

    try:
        runs_module._read_log_tail(log_path, 1024)
    except HTTPException as exc:
        assert exc.status_code == 500
        assert exc.detail == "could not read log file"
    else:
        raise AssertionError("expected log tail read to fail")


def test_run_log_endpoint_returns_not_found_for_unknown_run(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get("/api/v1/runs/404/log")

    assert response.status_code == 404
    assert response.json()["detail"] == "run not found"
