from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest
from tests.web_test_helpers import _client, _csrf_headers

from wudup import web_release_skips
from wudup.db import insert_update_event, insert_update_run, open_db
from wudup.release_skips import skip_from_row, skips_by_service

QBIT_IMAGE = "lscr.io/linuxserver/qbittorrent:5.2.0-r0-ls400"


def _mutating_client(tmp_path: Path):
    return _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )


def _create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "create_release_skip",
        "service_keys": ["media/qbittorrent"],
        "image": QBIT_IMAGE,
        "target_tag": "5.3.0-r0-ls412",
        "digest": "sha256:abc",
        "scope": "release",
        "reason": "stalls on magnet links",
    }
    payload.update(overrides)
    return payload


def test_release_skip_create_lists_and_deletes_with_audit(tmp_path: Path) -> None:
    client = _mutating_client(tmp_path)
    headers = _csrf_headers(client)

    created = client.post(
        "/api/v1/state/operations",
        json=_create_payload(target_tag=" 5.3.0-r0-ls412 ", reason=" stalls on magnet links "),
        headers=headers,
    )
    listed = client.get("/api/v1/snoozes?state=active")
    skip_id = created.json()["resource"]["id"]
    deleted = client.post(
        "/api/v1/state/operations",
        json={"kind": "delete_release_skip", "snooze_id": skip_id},
        headers=headers,
    )
    missing = client.post(
        "/api/v1/state/operations",
        json={"kind": "delete_release_skip", "snooze_id": skip_id},
        headers=headers,
    )

    assert created.status_code == 200
    resource = created.json()["resource"]
    assert resource["kind"] == "release"
    assert resource["release_label"] == "5.3.0"
    assert resource["skipped_tag"] == "5.3.0-r0-ls412"
    assert resource["reason"] == "stalls on magnet links"
    assert resource["active"] is True
    assert [record["kind"] for record in listed.json()] == ["release"]
    assert deleted.status_code == 200
    assert missing.status_code == 404

    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        skips = conn.execute("SELECT * FROM release_skips").fetchall()
        events = conn.execute("SELECT * FROM update_events ORDER BY id").fetchall()
    metadata = [json.loads(row["metadata_json"]) for row in events]
    assert skips == []
    assert [item["operation"] for item in metadata] == [
        "create_release_skip",
        "delete_release_skip",
    ]
    stored = metadata[0]["after"]
    assert stored["basis"] == "version"
    assert stored["value"] == "5.3.0"
    assert stored["image_repo"] == "linuxserver/qbittorrent"
    assert metadata[1]["before"]["value"] == "5.3.0"
    assert [(row["stack_name"], row["service_name"]) for row in events] == [
        ("media", "qbittorrent"),
        ("media", "qbittorrent"),
    ]


def test_release_skip_derives_the_release_server_side(tmp_path: Path) -> None:
    client = _mutating_client(tmp_path)
    headers = _csrf_headers(client)

    line = client.post(
        "/api/v1/state/operations",
        json=_create_payload(
            service_keys=["media/qbittorrent", "media/qbittorrent-test"],
            scope="line",
        ),
        headers=headers,
    )
    digest = client.post(
        "/api/v1/state/operations",
        json=_create_payload(
            service_keys=["media/nginx"],
            image="nginx:latest",
            target_tag="",
            digest="sha256:0123456789abcdef0123",
        ),
        headers=headers,
    )

    assert line.status_code == 200
    assert line.json()["resource"]["release_label"] == "5.3.x"
    assert line.json()["resource_id"].count(",") == 1
    assert digest.status_code == 200
    assert digest.json()["resource"]["release_label"] == "build sha256:0123456789ab"
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        rows = conn.execute(
            "SELECT service_key, basis, value, scope FROM release_skips ORDER BY id"
        ).fetchall()
        audited = conn.execute(
            "SELECT stack_name, service_name, target_image FROM update_events ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("media/qbittorrent", "version", "5.3.0", "line"),
        ("media/qbittorrent-test", "version", "5.3.0", "line"),
        ("media/nginx", "digest", "sha256:0123456789abcdef0123", "release"),
    ]
    assert [tuple(row) for row in audited] == [
        ("media", "qbittorrent", "1"),
        ("media", "qbittorrent-test", "2"),
        ("media", "nginx", "3"),
    ]
    assert line.json()["audit_run_id"] != digest.json()["audit_run_id"]


def test_release_skip_rejects_duplicates_and_unnamed_releases(tmp_path: Path) -> None:
    client = _mutating_client(tmp_path)
    headers = _csrf_headers(client)

    first = client.post("/api/v1/state/operations", json=_create_payload(), headers=headers)
    rebuild = client.post(
        "/api/v1/state/operations",
        json=_create_payload(target_tag="5.3.0-r0-ls413"),
        headers=headers,
    )
    invalid = [
        _create_payload(target_tag="", digest=""),
        _create_payload(service_keys=["  "]),
        _create_payload(service_keys=[]),
        _create_payload(image="redis:7", target_tag="8", scope="line"),
        _create_payload(scope="minor"),
    ]
    invalid_responses = [
        client.post("/api/v1/state/operations", json=payload, headers=headers)
        for payload in invalid
    ]

    assert first.status_code == 200
    assert rebuild.status_code == 409
    assert rebuild.json()["detail"] == "media/qbittorrent already skips 5.3.0"
    assert [response.status_code for response in invalid_responses] == [422] * 5
    assert invalid_responses[3].json()["detail"] == (
        "8 has no major.minor version, so only this release can be skipped"
    )


def test_release_skip_requires_mutations(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.post(
        "/api/v1/state/operations",
        json=_create_payload(),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "mutations are disabled"


def test_release_skip_clears_after_a_successful_service_update(tmp_path: Path) -> None:
    client = _mutating_client(tmp_path)
    headers = _csrf_headers(client)
    client.post("/api/v1/state/operations", json=_create_payload(), headers=headers)
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        run_id = insert_update_run(conn, status="success", mode="live")
        insert_update_event(
            conn,
            run_id=run_id,
            service_name="qbittorrent",
            stack_name="media",
            image=QBIT_IMAGE,
            status="success",
            created_at="2999-01-01T00:00:00+00:00",
        )

    active = client.get("/api/v1/snoozes?state=active").json()
    expired = client.get("/api/v1/snoozes?state=expired").json()

    assert active == []
    assert [(record["kind"], record["active"]) for record in expired] == [
        ("release", False)
    ]


def test_release_skip_survives_settings_changes_and_dry_runs(tmp_path: Path) -> None:
    client = _mutating_client(tmp_path)
    headers = _csrf_headers(client)
    client.post("/api/v1/state/operations", json=_create_payload(), headers=headers)
    other = client.post(
        "/api/v1/state/operations",
        json=_create_payload(target_tag="5.4.0-r0-ls420"),
        headers=headers,
    )
    for payload in (
        {
            "kind": "upsert_service_policy",
            "service_key": "media/qbittorrent",
            "update_mode": "stop",
        },
        {
            "kind": "create_dependency_snooze",
            "service_key": "media/qbittorrent",
            "wait_for_service_key": "media/vpn",
        },
        {"kind": "delete_release_skip", "snooze_id": other.json()["resource"]["id"]},
    ):
        assert client.post(
            "/api/v1/state/operations", json=payload, headers=headers
        ).status_code == 200
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        run_id = insert_update_run(conn, status="success", mode="live", dry_run=True)
        insert_update_event(
            conn,
            run_id=run_id,
            service_name="qbittorrent",
            stack_name="media",
            image=QBIT_IMAGE,
            status="success",
            created_at="2999-01-01T00:00:00+00:00",
        )

    active = client.get("/api/v1/snoozes?state=active").json()

    assert [(record["kind"], record["release_label"]) for record in active] == [
        ("dependency", ""),
        ("release", "5.3.0"),
    ]


def test_release_skip_clears_after_a_retag(tmp_path: Path) -> None:
    client = _mutating_client(tmp_path)
    client.post(
        "/api/v1/state/operations",
        json=_create_payload(),
        headers=_csrf_headers(client),
    )
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        run_id = insert_update_run(conn, status="success", mode="web-retag")
        insert_update_event(
            conn,
            run_id=run_id,
            service_name="qbittorrent",
            stack_name="media",
            image=QBIT_IMAGE,
            status="success",
            created_at="2999-01-01T00:00:00+00:00",
        )

    assert client.get("/api/v1/snoozes?state=active").json() == []


def test_pending_release_skips_warns_when_the_database_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(_settings: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(web_release_skips.web_database, "connect_readonly_db", fail)

    with caplog.at_level(logging.WARNING, logger="wudup.web_release_skips"):
        skips = web_release_skips.pending_release_skips(
            object(),  # type: ignore[arg-type]
            ["media/qbittorrent"],
        )

    assert skips == {}
    assert "Could not read release skips" in caplog.text
    assert "database is locked" in caplog.text


def test_pending_release_fields_hide_matches_and_remind_on_the_next_release() -> None:
    skips = skips_by_service(
        [
            skip_from_row(
                {
                    "id": 7,
                    "service_key": "media/qbittorrent",
                    "basis": "version",
                    "value": "5.3.0",
                    "scope": "release",
                    "skipped_tag": "5.3.0-r0-ls412",
                    "reason": "stalls on magnet links",
                    "created_at": "2026-10-26T10:00:00+00:00",
                }
            )
        ]
    )

    def fields(tag: str, digest: str = ""):
        return web_release_skips.pending_release_fields(
            skips,
            service_keys=["media/qbittorrent"],
            image_repo="linuxserver/qbittorrent",
            desired_tag=tag,
            digest=digest,
        )

    rebuild_key, rebuild_skip = fields("5.3.0-r0-ls413")
    next_key, next_skip = fields("5.3.1-r0-ls420")
    digest_key, digest_skip = fields("", "sha256:abc")

    assert rebuild_key is not None
    assert rebuild_key.label == "5.3.0"
    assert rebuild_skip is not None
    assert rebuild_skip.matches is True
    assert next_key is not None
    assert next_key.line == "5.3"
    assert next_skip is not None
    assert next_skip.matches is False
    assert next_skip.reason == "stalls on magnet links"
    assert digest_key is not None
    assert digest_key.basis == "digest"
    assert digest_skip is None
