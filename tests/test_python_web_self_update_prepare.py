from __future__ import annotations

import json
import shutil
from pathlib import Path

from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _make_fake_stack,
    _manifest_index_digest,
    _self_update_payload,
    _write_fake_image_after_pull,
    _write_fake_manifest,
)

from wudup import web_self_update as self_update_module
from wudup.db import (
    open_db,
)


def test_self_update_get_reports_prepare_strategy_for_pinned_tag_rewrite_targets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(self_update_module, "current_tag", lambda: "v0.24.2")
    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.25.0")
    monkeypatch.setattr(
        self_update_module,
        "current_container_image",
        lambda _env: "ghcr.io/magrhino/wudup:v0.24.2",
    )
    monkeypatch.setattr(
        self_update_module,
        "_fetch_self_update_release_notes",
        lambda *_args, **_kwargs: ([], False, []),
    )
    response = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
        },
    ).get("/api/v1/self-update")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["strategy"] == "prepare_tag_update"
    assert body["target_image"] == "ghcr.io/magrhino/wudup:v0.25.0"
    assert body["can_update"] is True
    assert body["disabled_reason"] == ""
    assert body["external_recreate_required"] is True


def test_self_update_pull_endpoint_rejects_pinned_tag_prepare_targets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    monkeypatch.setattr(self_update_module, "current_tag", lambda: "v0.24.2")
    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.25.0")
    monkeypatch.setattr(
        self_update_module,
        "current_container_image",
        lambda _env: "ghcr.io/magrhino/wudup:v0.24.2",
    )
    monkeypatch.setattr(
        self_update_module,
        "_fetch_self_update_release_notes",
        lambda *_args, **_kwargs: ([], False, []),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
            **fake_env,
        },
    )

    response = client.post(
        "/api/v1/self-update",
        json=_self_update_payload(
            target_image="ghcr.io/magrhino/wudup:v0.25.0",
        ),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "self-update target requires tag update preparation"
    assert " pull " not in _fake_docker_calls(fake_root)


def test_self_update_prepare_endpoint_rewrites_tag_pulls_and_audits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    monkeypatch.setattr(self_update_module, "current_tag", lambda: "v0.24.2")
    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.25.0")
    monkeypatch.setattr(
        self_update_module,
        "current_container_image",
        lambda _env: "ghcr.io/magrhino/wudup:v0.24.2",
    )
    monkeypatch.setattr(
        self_update_module,
        "_fetch_self_update_release_notes",
        lambda *_args, **_kwargs: ([], False, []),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
            **fake_env,
        },
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "wud",
        [
            (
                "wudup",
                "ghcr.io/magrhino/wudup:v0.24.2",
                "wudup",
            ),
        ],
    )
    plan = client.post(
        "/api/v1/self-update/plan",
        headers=_csrf_headers(client),
    ).json()

    response = client.post(
        "/api/v1/self-update/prepare",
        json={
            "confirmation": "prepare_tag_update",
            "plan_id": plan["plan"]["plan_id"],
            "current_tag": "v0.24.2",
            "latest_tag": "v0.25.0",
            "target_image": "ghcr.io/magrhino/wudup:v0.25.0",
            "restart_container": "wudup",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "tag_prepared"
    assert body["external_recreate_required"] is True
    assert "image: ghcr.io/magrhino/wudup:v0.25.0" in (
        compose_dir / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    calls = _fake_docker_calls(fake_root)
    assert "inspect wudup" in calls
    assert "compose -f docker-compose.yml pull wudup" in calls
    assert " up -d " not in calls
    assert "restart " not in calls
    assert client.app.state.web_self_update_running is False

    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        row = conn.execute(
            "SELECT mode, status, finished_at, metadata_json FROM update_runs WHERE id = ?",
            (body["audit_run_id"],),
        ).fetchone()
        event = conn.execute(
            "SELECT status, metadata_json FROM update_events WHERE run_id = ?",
            (body["audit_run_id"],),
        ).fetchone()
    assert row["mode"] == "web-self-update"
    assert row["status"] == "tag_prepared"
    assert row["finished_at"]
    assert event["status"] == "tag_prepared"
    metadata = json.loads(row["metadata_json"])
    assert metadata["operation"] == "self_update"
    assert metadata["strategy"] == "prepare_tag_update"
    assert metadata["status"] == "tag_prepared"
    assert metadata["external_recreate_required"] is True
    assert metadata["services"] == ["wudup"]
    assert metadata["tag_updates"][0]["new_image"] == (
        "ghcr.io/magrhino/wudup:v0.25.0"
    )


def test_self_update_prepare_endpoint_digest_pins_after_verification(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    target_image = "ghcr.io/magrhino/wudup:v0.25.0"
    _write_fake_manifest(
        fake_root,
        target_image,
        _manifest_index_digest("sha256:index", "sha256:child"),
    )
    _write_fake_image_after_pull(
        fake_root,
        target_image,
        "sha256:config",
        "sha256:index",
    )
    monkeypatch.setattr(self_update_module, "current_tag", lambda: "v0.24.2")
    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.25.0")
    monkeypatch.setattr(
        self_update_module,
        "current_container_image",
        lambda _env: "ghcr.io/magrhino/wudup:v0.24.2",
    )
    monkeypatch.setattr(
        self_update_module,
        "_fetch_self_update_release_notes",
        lambda *_args, **_kwargs: ([], False, []),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
            "WUD_DIGEST_PIN_UPDATES": "true",
            **fake_env,
        },
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "wud",
        [
            (
                "wudup",
                "ghcr.io/magrhino/wudup:v0.24.2",
                "wudup",
            ),
        ],
    )
    plan = client.post(
        "/api/v1/self-update/plan",
        headers=_csrf_headers(client),
    ).json()

    response = client.post(
        "/api/v1/self-update/prepare",
        json={
            "confirmation": "prepare_tag_update",
            "plan_id": plan["plan"]["plan_id"],
            "current_tag": "v0.24.2",
            "latest_tag": "v0.25.0",
            "target_image": target_image,
            "restart_container": "wudup",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    content = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert "# wudup.resolved-tag=v0.25.0" in content
    assert "image: ghcr.io/magrhino/wudup@sha256:index" in content
    assert "wud.tag.include=^v0\\.25\\.0$$" in content

    body = response.json()
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        row = conn.execute(
            "SELECT metadata_json FROM update_runs WHERE id = ?",
            (body["audit_run_id"],),
        ).fetchone()
    metadata = json.loads(row["metadata_json"])
    assert metadata["digest_pin_updates"][0]["final_image"] == (
        "ghcr.io/magrhino/wudup@sha256:index"
    )


def test_self_update_prepare_endpoint_rejects_moved_digest_pin_after_pull(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    target_image = "ghcr.io/magrhino/wudup:v0.25.0"
    _write_fake_manifest(
        fake_root,
        target_image,
        _manifest_index_digest("sha256:planned", "sha256:child"),
    )
    _write_fake_image_after_pull(
        fake_root,
        target_image,
        "sha256:config",
        "sha256:planned",
    )
    monkeypatch.setattr(self_update_module, "current_tag", lambda: "v0.24.2")
    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.25.0")
    monkeypatch.setattr(
        self_update_module,
        "current_container_image",
        lambda _env: "ghcr.io/magrhino/wudup:v0.24.2",
    )
    monkeypatch.setattr(
        self_update_module,
        "_fetch_self_update_release_notes",
        lambda *_args, **_kwargs: ([], False, []),
    )
    original_pull = self_update_module.ComposeCli.pull

    def moving_pull(self, *args, **kwargs):
        result = original_pull(self, *args, **kwargs)
        _write_fake_manifest(
            fake_root,
            target_image,
            _manifest_index_digest("sha256:moved", "sha256:child"),
        )
        return result

    monkeypatch.setattr(self_update_module.ComposeCli, "pull", moving_pull)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
            "WUD_DIGEST_PIN_UPDATES": "true",
            **fake_env,
        },
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "wud",
        [
            (
                "wudup",
                "ghcr.io/magrhino/wudup:v0.24.2",
                "wudup",
            ),
        ],
    )
    compose_before = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    plan = client.post(
        "/api/v1/self-update/plan",
        headers=_csrf_headers(client),
    ).json()

    response = client.post(
        "/api/v1/self-update/prepare",
        json={
            "confirmation": "prepare_tag_update",
            "plan_id": plan["plan"]["plan_id"],
            "current_tag": "v0.24.2",
            "latest_tag": "v0.25.0",
            "target_image": target_image,
            "restart_container": "wudup",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 500
    assert (compose_dir / "docker-compose.yml").read_text(encoding="utf-8") == compose_before
    assert client.app.state.web_self_update_running is False


def test_self_update_prepare_endpoint_restores_compose_when_pull_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    monkeypatch.setattr(self_update_module, "current_tag", lambda: "v0.24.2")
    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.25.0")
    monkeypatch.setattr(
        self_update_module,
        "current_container_image",
        lambda _env: "ghcr.io/magrhino/wudup:v0.24.2",
    )
    monkeypatch.setattr(
        self_update_module,
        "_fetch_self_update_release_notes",
        lambda *_args, **_kwargs: ([], False, []),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
            **fake_env,
        },
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "wud",
        [
            (
                "wudup",
                "ghcr.io/magrhino/wudup:v0.24.2",
                "wudup",
            ),
        ],
    )
    (fake_root / "stacks" / "wud" / "pull_fail").write_text(
        "pull failed\n",
        encoding="utf-8",
    )
    compose_before = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    plan = client.post(
        "/api/v1/self-update/plan",
        headers=_csrf_headers(client),
    ).json()

    response = client.post(
        "/api/v1/self-update/prepare",
        json={
            "confirmation": "prepare_tag_update",
            "plan_id": plan["plan"]["plan_id"],
            "current_tag": "v0.24.2",
            "latest_tag": "v0.25.0",
            "target_image": "ghcr.io/magrhino/wudup:v0.25.0",
            "restart_container": "wudup",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 500
    assert response.json()["detail"].startswith(
        "could not prepare self-update tag update",
    )
    assert (compose_dir / "docker-compose.yml").read_text(encoding="utf-8") == compose_before
    calls = _fake_docker_calls(fake_root)
    assert "compose -f docker-compose.yml pull wudup" in calls
    assert " up -d " not in calls
    assert "restart " not in calls
    assert client.app.state.web_self_update_running is False

    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        row = conn.execute(
            "SELECT status, finished_at, metadata_json FROM update_runs WHERE mode = 'web-self-update'",
        ).fetchone()
    assert row["status"] == "failure"
    assert row["finished_at"]
    metadata = json.loads(row["metadata_json"])
    assert metadata["status"] == "failure"
    assert "docker compose" in metadata["error"]


def test_self_update_prepare_endpoint_keeps_backup_when_restore_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    monkeypatch.setattr(self_update_module, "current_tag", lambda: "v0.24.2")
    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.25.0")
    monkeypatch.setattr(
        self_update_module,
        "current_container_image",
        lambda _env: "ghcr.io/magrhino/wudup:v0.24.2",
    )
    monkeypatch.setattr(
        self_update_module,
        "_fetch_self_update_release_notes",
        lambda *_args, **_kwargs: ([], False, []),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
            **fake_env,
        },
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "wud",
        [
            (
                "wudup",
                "ghcr.io/magrhino/wudup:v0.24.2",
                "wudup",
            ),
        ],
    )
    (fake_root / "stacks" / "wud" / "pull_fail").write_text(
        "pull failed\n",
        encoding="utf-8",
    )
    compose_path = compose_dir / "docker-compose.yml"
    compose_before = compose_path.read_text(encoding="utf-8")
    backup = compose_path.with_name(".docker-compose.yml.backup.test")

    def backup_compose(path: Path) -> Path:
        shutil.copy2(path, backup)
        return backup

    def fail_restore(
        _backup: Path, _compose_path: Path, *, expected_source_hash: str,
    ) -> None:
        assert expected_source_hash
        raise OSError("restore blocked")

    monkeypatch.setattr(self_update_module, "_backup_compose", backup_compose)
    monkeypatch.setattr(self_update_module, "restore_compose_backup", fail_restore)
    plan = client.post(
        "/api/v1/self-update/plan",
        headers=_csrf_headers(client),
    ).json()

    response = client.post(
        "/api/v1/self-update/prepare",
        json={
            "confirmation": "prepare_tag_update",
            "plan_id": plan["plan"]["plan_id"],
            "current_tag": "v0.24.2",
            "latest_tag": "v0.25.0",
            "target_image": "ghcr.io/magrhino/wudup:v0.25.0",
            "restart_container": "wudup",
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 500
    assert "compose rollback failed: restore blocked" in response.json()["detail"]
    assert backup.read_text(encoding="utf-8") == compose_before
    assert "image: ghcr.io/magrhino/wudup:v0.25.0" in compose_path.read_text(
        encoding="utf-8",
    )
    assert client.app.state.web_self_update_running is False
