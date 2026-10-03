from __future__ import annotations

import json
from pathlib import Path

from tests.web_test_helpers import (
    _client,
    _csrf_headers,
)

from wudup import web_self_update as self_update_module


def test_self_update_get_reports_available_up_to_date_disabled_and_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(self_update_module, "current_tag", lambda: "v0.24.2")
    monkeypatch.setattr(
        self_update_module,
        "current_container_image",
        lambda _env: "ghcr.io/magrhino/wudup:v0.24.2-trivy",
    )
    monkeypatch.setattr(
        self_update_module,
        "_fetch_self_update_release_notes",
        lambda *_args, **_kwargs: ([], False, []),
    )
    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.25.0")
    available = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
        },
    ).get("/api/v1/self-update")

    assert available.status_code == 200
    body = available.json()
    assert body["status"] == "available"
    assert body["current_tag"] == "v0.24.2"
    assert body["latest_tag"] == "v0.25.0"
    assert body["target_image"] == "ghcr.io/magrhino/wudup:v0.25.0-trivy"
    assert body["external_recreate_required"] is True
    assert body["can_update"] is False
    assert "Read-only mode" in body["disabled_reason"]

    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.24.2")
    up_to_date = _client(
        tmp_path,
        {"WUD_WEB_DEV_NO_AUTH": "true"},
    ).get("/api/v1/self-update")
    assert up_to_date.json()["status"] == "up_to_date"

    disabled = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUDUP_RELEASE_CHECK": "false",
        },
    ).get("/api/v1/self-update")
    assert disabled.json()["status"] == "disabled"

    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: None)
    unavailable = _client(
        tmp_path,
        {"WUD_WEB_DEV_NO_AUTH": "true"},
    ).get("/api/v1/self-update")
    assert unavailable.json()["status"] == "unavailable"
    assert unavailable.json()["warnings"]

    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.25.0")
    for current_image in (
        "",
        f"ghcr.io/magrhino/wudup@sha256:{'a' * 64}",
        f"sha256:{'b' * 64}",
    ):
        monkeypatch.setattr(
            self_update_module,
            "current_container_image",
            lambda _env, image=current_image: image,
        )
        unknown_variant = _client(
            tmp_path,
            {"WUD_WEB_DEV_NO_AUTH": "true"},
        ).get("/api/v1/self-update")
        assert unknown_variant.json()["status"] == "unavailable"
        assert unknown_variant.json()["current_image"] == current_image
        assert unknown_variant.json()["latest_tag"] == "v0.25.0"
        assert (
            "cannot preserve the image variant"
            in unknown_variant.json()["disabled_reason"]
        )

    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.24.2")
    monkeypatch.setattr(self_update_module, "current_container_image", lambda _env: "")
    current_without_image_identity = _client(
        tmp_path,
        {"WUD_WEB_DEV_NO_AUTH": "true"},
    ).get("/api/v1/self-update")
    assert current_without_image_identity.json()["status"] == "up_to_date"
    assert current_without_image_identity.json()["current_image"] == ""


def test_self_update_get_skips_edge_images_without_offering_a_downgrade(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_latest_release_lookup() -> str:
        raise AssertionError("edge images must not look up the latest release")

    monkeypatch.setattr(self_update_module, "current_tag", lambda: "v0.65.2")
    monkeypatch.setattr(
        self_update_module, "fetch_latest_release_tag", fail_latest_release_lookup
    )
    for current_image in (
        "ghcr.io/magrhino/wudup:edge",
        "ghcr.io/magrhino/wudup:edge-0123abc-trivy",
    ):
        monkeypatch.setattr(
            self_update_module,
            "current_container_image",
            lambda _env, image=current_image: image,
        )
        client = _client(
            tmp_path,
            {
                "WUD_WEB_DEV_NO_AUTH": "true",
                "WUD_WEB_MUTATIONS_ENABLED": "true",
                "WUD_WEB_RESTART_CONTAINER": "wudup",
            },
        )

        body = client.get("/api/v1/self-update").json()
        assert body["status"] == "disabled"
        assert body["can_update"] is False
        assert body["current_image"] == current_image
        assert body["target_image"] == ""
        assert "edge test image" in body["disabled_reason"]
        assert "stable releases" in body["disabled_reason"]

        applied = client.post(
            "/api/v1/self-update",
            json={
                "confirmation": "pull_image",
                "current_tag": "v0.65.2",
                "latest_tag": "v0.66.0",
                "target_image": "ghcr.io/magrhino/wudup:latest",
                "restart_container": "wudup",
            },
            headers=_csrf_headers(client),
        )
        assert applied.status_code == 409
        assert "edge test image" in applied.json()["detail"]


def test_status_reports_edge_build_version(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    monkeypatch.delenv("WUDUP_BUILD_VERSION", raising=False)
    assert client.get("/api/v1/status").json()["build_version"] == ""

    monkeypatch.setenv("WUDUP_BUILD_VERSION", "edge-0123abc")
    assert client.get("/api/v1/status").json()["build_version"] == "edge-0123abc"

    monkeypatch.setenv("WUDUP_BUILD_VERSION", "not-a-build")
    assert client.get("/api/v1/status").json()["build_version"] == ""


def test_self_update_get_can_use_local_demo_fixture(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_RESTART_CONTAINER": "demo-wudup",
            "WUD_WEB_DEMO_SELF_UPDATE": "true",
        },
    )

    response = client.get("/api/v1/self-update")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["current_tag"] == "v0.25.0"
    assert body["latest_tag"] == "v0.26.0"
    assert body["target_image"] == "ghcr.io/magrhino/wudup:latest"
    assert body["restart_container"] == "demo-wudup"
    assert body["can_update"] is True
    assert body["release_notes_truncated"] is True
    assert len(body["release_notes"]) == 10


def test_self_update_release_notes_are_between_versions_and_capped(
    monkeypatch,
) -> None:
    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            releases = [
                {
                    "tag_name": f"v0.{minor}.0",
                    "name": f"v0.{minor}.0",
                    "html_url": f"https://example.test/v0.{minor}.0",
                    "body": "Routine update",
                    "published_at": f"2026-05-{minor:02d}T00:00:00Z",
                }
                for minor in range(10, 25)
            ]
            return json.dumps(releases).encode("utf-8")

    monkeypatch.setattr(self_update_module.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    notes, truncated, warnings = self_update_module._fetch_self_update_release_notes(
        "v0.12.0",
        "v0.24.0",
        {},
        cap=10,
    )

    assert warnings == []
    assert truncated is True
    assert len(notes) == 10
    assert notes[0].tag == "v0.24.0"
    assert notes[-1].tag == "v0.15.0"
    assert all(note.tag not in {"v0.12.0", "v0.11.0"} for note in notes)
