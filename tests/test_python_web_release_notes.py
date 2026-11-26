from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _fake_image_state_file,
    _install_wud_api,
    _wud_api_container,
)

from wudup import web_release_notes as release_notes_module
from wudup.command import CommandError, CommandResult
from wudup.release_notes import ReleaseNoteInfo as ReleaseNoteData
from wudup.web_models import WudApiStatus


def _release_client(tmp_path: Path, env: dict[str, str] | None = None):
    values = {
        "WUD_WEB_DEV_NO_AUTH": "true",
        "WUD_RELEASE_NOTES_ENABLED": "true",
    }
    if env:
        values.update(env)
    return _client(tmp_path, values)


def test_release_notes_get_returns_placeholders_by_default_without_creating_database(
    tmp_path: Path,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    db_path = tmp_path / "state" / "wud.sqlite"
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    wud_file.write_text("ghcr.io/acme/app:1.0.0\n", encoding="utf-8")

    response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["disabled_reason"] == ""
    assert body["notifications_enabled"] is False
    assert (
        body["notifications_disabled_reason"]
        == "Release-note notifications are disabled."
    )
    assert body["count"] == 1
    assert body["items"][0]["line_no"] == 1
    assert body["items"][0]["status"] == "missing"
    assert body["items"][0]["provider"] == "github"
    assert not db_path.exists()


def test_release_notes_get_returns_placeholders_without_creating_database(
    tmp_path: Path,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    db_path = tmp_path / "state" / "wud.sqlite"
    client = _release_client(tmp_path)
    wud_file.write_text("ghcr.io/acme/app:1.0.0\n", encoding="utf-8")

    response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["line_no"] == 1
    assert body["items"][0]["status"] == "missing"
    assert body["items"][0]["provider"] == "github"
    assert not db_path.exists()


def test_release_notes_get_exposes_lsio_classification_placeholder(
    tmp_path: Path,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    upstream_map = tmp_path / "upstreams.txt"
    upstream_map.write_text(
        "linuxserver/docker-radarr: Radarr/Radarr\n",
        encoding="utf-8",
    )
    client = _release_client(tmp_path, {"WUD_WEB_UPSTREAM_MAP": str(upstream_map)})
    wud_file.write_text("linuxserver/radarr:5.1.0-ls1 tag=5.1.0-ls2\n", encoding="utf-8")

    response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["provider"] == "lsio"
    assert item["classification"]["change_type"] == "image_rebuild"
    assert item["classification"]["current"]["upstream_version"] == "5.1.0"
    assert item["classification"]["target"]["build_suffix"] == "ls2"


def test_release_notes_get_uses_api_pending_source_without_wud_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=[
            _wud_api_container(
                image="ghcr.io/acme/app",
                tag="1.0.0",
                remote_tag="2.0.0",
            )
        ],
    )
    client = _release_client(
        tmp_path,
        {
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.release-api-source.test:3000",
        },
    )
    db_path = tmp_path / "state" / "wud.sqlite"

    response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["active"] == "api"
    assert body["source_file"] == "WUD API"
    assert body["count"] == 1
    assert body["items"][0]["line_no"] == 1
    assert body["items"][0]["status"] == "missing"
    assert body["items"][0]["provider"] == "github"
    assert body["items"][0]["upstream_repo"] == "acme/app"
    assert body["wud_api"]["metadata_available"] is True
    assert not (tmp_path / "state" / "images.todo").exists()
    assert not db_path.exists()


def test_release_notes_get_skips_wud_metadata_when_file_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_metadata_by_target(*_args, **_kwargs):
        raise AssertionError("metadata_by_target should not be called")

    monkeypatch.setattr(
        release_notes_module.web_wud_api,
        "get_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(
            status=WudApiStatus(
                state="ready",
                available=True,
                metadata_available=True,
                last_checked_at="2026-06-19T00:00:00+00:00",
                detail="test WUD API snapshot",
            ),
        ),
    )
    monkeypatch.setattr(
        release_notes_module.web_wud_api,
        "metadata_by_target",
        fail_metadata_by_target,
    )
    client = _release_client(tmp_path)

    response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["items"] == []
    assert body["wud_api"]["state"] == "ready"


def test_release_notes_get_uses_docker_source_label_without_creating_database(
    tmp_path: Path,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    db_path = tmp_path / "state" / "wud.sqlite"
    docker_env, fake_root = _fake_docker_env(tmp_path)
    client = _release_client(
        tmp_path,
        {
            **docker_env,
        },
    )
    image = "advplyr/audiobookshelf:latest"
    wud_file.write_text(f"{image}\n", encoding="utf-8")
    _fake_image_state_file(fake_root, image, "labels").write_text(
        "org.opencontainers.image.source=https://github.com/advplyr/audiobookshelf\n",
        encoding="utf-8",
    )

    response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["status"] == "missing"
    assert body["items"][0]["provider"] == "github"
    assert body["items"][0]["upstream_repo"] == "advplyr/audiobookshelf"
    assert f"image inspect {image}" in _fake_docker_calls(fake_root)
    assert not db_path.exists()


@pytest.mark.parametrize(
    ("wud_line", "label_image", "uninspected_image"),
    [
        (
            f"example.invalid/acme/app:latest@sha256:{'a' * 64}",
            "example.invalid/acme/app:latest",
            f"example.invalid/acme/app:latest@sha256:{'a' * 64}",
        ),
        (
            f"example.invalid/acme/app@sha256:{'b' * 64} tag=latest",
            "example.invalid/acme/app:latest",
            f"example.invalid/acme/app@sha256:{'b' * 64}",
        ),
    ],
)
def test_release_notes_get_uses_tagged_label_for_digest_ref(
    tmp_path: Path,
    caplog,
    wud_line: str,
    label_image: str,
    uninspected_image: str,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    docker_env, fake_root = _fake_docker_env(tmp_path)
    client = _release_client(
        tmp_path,
        {
            **docker_env,
        },
    )
    wud_file.write_text(f"{wud_line}\n", encoding="utf-8")
    _fake_image_state_file(fake_root, label_image, "labels").write_text(
        "org.opencontainers.image.source=https://github.com/acme/app\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR, logger="wudup.web_release_notes"):
        response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["provider"] == "github"
    assert body["items"][0]["upstream_repo"] == "acme/app"
    calls = _fake_docker_calls(fake_root)
    assert f"image inspect {label_image}" in calls
    assert f"image inspect {uninspected_image}" not in calls
    assert "Docker inspect failed" not in caplog.text


def test_release_notes_get_uses_running_container_label_for_bare_digest_ref(
    tmp_path: Path,
    caplog,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    docker_env, fake_root = _fake_docker_env(tmp_path)
    client = _release_client(
        tmp_path,
        {
            **docker_env,
        },
    )
    digest_image = f"example.invalid/acme/app@sha256:{'c' * 64}"
    running_image = "example.invalid/acme/app:latest"
    wud_file.write_text(f"{digest_image}\n", encoding="utf-8")
    (fake_root / "containers.tsv").write_text(
        f"app\t{running_image}\n",
        encoding="utf-8",
    )
    _fake_image_state_file(fake_root, running_image, "labels").write_text(
        "org.opencontainers.image.source=https://github.com/acme/app\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR, logger="wudup.web_release_notes"):
        response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["provider"] == "github"
    assert body["items"][0]["upstream_repo"] == "acme/app"
    calls = _fake_docker_calls(fake_root)
    assert f"image inspect {digest_image}" in calls
    assert "ps --format" in calls
    assert f"image inspect {running_image}" in calls
    assert "Docker inspect failed" not in caplog.text


@pytest.mark.parametrize(
    ("wud_line", "first_inspected_image"),
    [
        ("advplyr/audiobookshelf:latest", "advplyr/audiobookshelf:latest"),
        ("audiobookshelf", "audiobookshelf"),
    ],
)
def test_release_notes_get_recovers_ghcr_repo_from_running_container(
    tmp_path: Path,
    wud_line: str,
    first_inspected_image: str,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    db_path = tmp_path / "state" / "wud.sqlite"
    docker_env, fake_root = _fake_docker_env(tmp_path)
    client = _release_client(
        tmp_path,
        {
            **docker_env,
        },
    )
    wud_file.write_text(f"{wud_line}\n", encoding="utf-8")
    (fake_root / "containers.tsv").write_text(
        "audiobookshelf\tghcr.io/advplyr/audiobookshelf:latest\n",
        encoding="utf-8",
    )

    response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["status"] == "missing"
    assert body["items"][0]["provider"] == "github"
    assert body["items"][0]["image_repo"] == "advplyr/audiobookshelf"
    assert body["items"][0]["upstream_repo"] == "advplyr/audiobookshelf"
    calls = _fake_docker_calls(fake_root)
    assert f"image inspect {first_inspected_image}" in calls
    assert "ps --format" in calls
    assert not db_path.exists()

def test_release_notes_get_logs_when_docker_source_label_inspect_fails(
    tmp_path: Path,
    caplog,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    no_docker_bin = tmp_path / "no-docker-bin"
    no_docker_bin.mkdir()
    client = _release_client(
        tmp_path,
        {
            "PATH": str(no_docker_bin),
        },
    )
    image = "advplyr/audiobookshelf:latest"
    wud_file.write_text(f"{image}\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="wudup.web_release_notes"):
        response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["status"] == "unsupported"
    assert body["items"][0]["error"] == "no supported GitHub release source found"
    assert (
        "WebUI release-note fallback: Docker inspect failed for "
        "advplyr/audiobookshelf:latest"
    ) in caplog.text
    assert "cannot read org.opencontainers.image.source" in caplog.text


def test_release_notes_get_sanitizes_docker_inspect_stderr_in_log(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    secret = "release-notes-secret-token"
    image = "example.invalid/acme/app:latest"
    wud_file = tmp_path / "state" / "images.todo"
    stderr = (
        f"inspect failed for {tmp_path / 'state' / 'docker.sock'} with {secret} "
        + ("x" * 700)
        + " tail-marker"
    )

    class FakeDockerCli:
        def __init__(self, **_kwargs) -> None:
            pass

        def try_image_label(self, label_image: str, _label: str):
            result = CommandResult(
                args=("docker", "image", "inspect", label_image),
                cwd=None,
                returncode=1,
                stderr=stderr,
            )
            return "", CommandError(result)

        def try_container_images(self):
            return []

    monkeypatch.setattr(release_notes_module, "DockerCli", FakeDockerCli)
    client = _release_client(
        tmp_path,
        {
            "WUD_WEB_TOKEN": secret,
        },
    )
    wud_file.write_text(f"{image}\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="wudup.web_release_notes"):
        response = client.get("/api/v1/release-notes")

    assert response.status_code == 200
    assert f"Docker inspect failed for {image}" in caplog.text
    assert "cannot read org.opencontainers.image.source" in caplog.text
    assert f"Command: docker image inspect {image}" in caplog.text
    assert secret not in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "<redacted>" in caplog.text
    assert "[REDACTED_PATH]" in caplog.text
    assert "[truncated]" in caplog.text
    assert "tail-marker" not in caplog.text


def test_release_notes_refresh_requires_csrf(tmp_path: Path) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    client = _release_client(tmp_path)
    wud_file.write_text("docker.io/library/redis:latest\n", encoding="utf-8")

    response = client.post("/api/v1/release-notes/refresh")

    assert response.status_code == 403
    assert response.json()["detail"] == "origin header is required"

    response = client.post(
        "/api/v1/release-notes/refresh",
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "csrf token is required"


def test_release_notes_refresh_requires_mutations(
    tmp_path: Path,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    db_path = tmp_path / "state" / "wud.sqlite"
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    wud_file.write_text("docker.io/library/redis:latest\n", encoding="utf-8")

    response = client.post(
        "/api/v1/release-notes/refresh",
        headers=_csrf_headers(client),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "mutations are disabled"
    assert not db_path.exists()


def test_release_notes_refresh_works_when_mutations_are_enabled(
    tmp_path: Path,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    # busybox has no release-note upstream map entry, so no GitHub lookup runs.
    wud_file.write_text("docker.io/library/busybox:latest\n", encoding="utf-8")

    response = client.post(
        "/api/v1/release-notes/refresh",
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["notifications_enabled"] is False
    assert body["items"][0]["status"] == "unsupported"
    assert body["items"][0]["error"] == "no supported GitHub release source found"


def test_release_notes_refresh_only_bypasses_cache_when_forced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wud_file = tmp_path / "state" / "images.todo"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    wud_file.write_text("ghcr.io/acme/app:1.0.0 tag=2.0.0\n", encoding="utf-8")
    force_values: list[bool] = []

    def fake_refresh_release_notes(_conn, targets, _environ, *, force=False, **_kwargs):
        force_values.append(force)
        return [
            ReleaseNoteData(
                line_no=target.line_no,
                status="ready",
                provider="github",
                image_repo="acme/app",
                upstream_repo="acme/app",
            )
            for target in targets
        ]

    monkeypatch.setattr(
        release_notes_module,
        "refresh_release_notes",
        fake_refresh_release_notes,
    )
    headers = _csrf_headers(client)

    cached = client.post("/api/v1/release-notes/refresh", headers=headers)
    forced = client.post("/api/v1/release-notes/refresh?force=true", headers=headers)

    assert cached.status_code == 200
    assert forced.status_code == 200
    assert force_values == [False, True]


def test_release_note_error_metadata_redacts_configured_secrets(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    github_token = "github-token-secret-value"
    release_webhook = "https://discord.test/fail/release-secret-token"
    admin_webhook = "https://discord.test/fail/admin-secret-token"
    wud_file = tmp_path / "state" / "images.todo"
    client = _release_client(
        tmp_path,
        {
            "GITHUB_TOKEN": github_token,
            "DISCORD_WEBHOOK": release_webhook,
            "ADMIN_WEBHOOK": admin_webhook,
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    wud_file.write_text("ghcr.io/acme/app:1.0.0 tag=2.0.0\n", encoding="utf-8")

    def fake_refresh_release_notes(
        _conn,
        _targets,
        _environ,
        *,
        redact_error=None,
        **_kwargs,
    ):
        error = (
            f"request failed with {github_token} via {release_webhook} "
            f"and {admin_webhook}"
        )
        if redact_error is not None:
            error = redact_error(error)
        return [
            ReleaseNoteData(
                line_no=1,
                status="error",
                provider="github",
                image_repo="acme/app",
                upstream_repo="acme/app",
                error=error,
            )
        ]

    monkeypatch.setattr(
        release_notes_module,
        "refresh_release_notes",
        fake_refresh_release_notes,
    )

    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/api/v1/release-notes/refresh",
            headers=_csrf_headers(client),
        )

    assert response.status_code == 200
    assert "<redacted>" in response.text
    for secret in (
        github_token,
        release_webhook,
        "release-secret-token",
        admin_webhook,
        "admin-secret-token",
    ):
        assert secret not in response.text
        assert secret not in caplog.text
