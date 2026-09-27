from __future__ import annotations

from pathlib import Path

import pytest
from tests.web_retag_test_helpers import (
    _patch_digest_resolution,
    _patch_digest_resolution_map,
    _patch_digest_resolution_results,
    _switch_choice,
    _wait_retag_preview_job,
)
from tests.web_test_helpers import (
    _assert_pending_grouping_did_not_mutate,
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _make_fake_stack,
    _wait_apply_job,
)

from wudup import web_database, web_retag_targets
from wudup import web_retags as web_retags_module
from wudup.compose import ComposeStack, ServiceImage
from wudup.db import init_db, open_db
from wudup.digest_verifier import DigestResolveResult
from wudup.release_notes import ReleaseNoteInfo


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/acme/app:1.0",
        "ghcr.io/acme/app@sha256:" + "a" * 64,
    ],
)
def test_retag_targets_endpoint_infers_safe_github_tags_link(
    tmp_path: Path,
    image: str,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", image, "cid-app")],
    )

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["candidate_link_label"] == "GitHub tags"
    assert item["candidate_link_url"] == "https://github.com/acme/app/tags"
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


@pytest.mark.parametrize(
    "image",
    [
        "docker.io/acme/app:1.0",
        "ghcr.io/acme/app?tab=tags:1.0",
        "ghcr.io/acme/..:1.0",
        "ghcr.io/acme/nested/app:1.0",
        "registry.example/ghcr.io/acme/app:1.0",
    ],
)
def test_retag_targets_endpoint_skips_unsafe_github_tags_link(
    tmp_path: Path,
    image: str,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", image, "cid-app")],
    )

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["candidate_link_label"] == ""
    assert item["candidate_link_url"] == ""
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_github_latest_fallback_refresh_enables_cached_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "ghcr.io/acme/app:latest", "cid-app")],
    )
    digest = "sha256:" + "b" * 64
    _patch_github_latest(
        monkeypatch,
        tag="v1.2.3",
        url="https://github.com/acme/app/releases/tag/v1.2.3",
    )
    _patch_digest_resolution(
        monkeypatch,
        expected_image="ghcr.io/acme/app:v1.2.3",
        digest=digest,
    )

    missing_csrf = client.post("/api/v1/retag-targets/github-latest/refresh")
    refresh = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=_csrf_headers(client),
    )
    cached = client.get(
        "/api/v1/retag-targets",
        params={"github_latest_fallback": "true"},
    )
    plain = client.get("/api/v1/retag-targets")

    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "origin header is required"
    assert refresh.status_code == 200
    item = refresh.json()["items"][0]
    assert item["retag_available"] is True
    assert item["retag_reason"] == "eligible"
    assert item["proposed_tag"] == "v1.2.3"
    assert item["final_image"] == "ghcr.io/acme/app:v1.2.3"
    assert item["candidate_source"] == "github-latest"
    assert "will update latest tracking to v1.2.3" in item["candidate_warning"]
    assert item["candidate_link_label"] == "GitHub release"
    assert item["candidate_link_url"].endswith("/v1.2.3")
    assert item["digest_provenance"]["provenance_source"] == "github-latest"
    assert cached.json()["items"][0]["retag_available"] is True
    assert plain.json()["items"][0]["retag_reason"] == "missing-provenance"
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_github_latest_fallback_keys_duplicate_services_by_target_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "ghcr.io/acme/app:latest", "cid-app-one")],
        parent=tmp_path / "docker" / "one",
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "ghcr.io/acme/worker:latest", "cid-app-two")],
        parent=tmp_path / "docker" / "two",
    )
    app_digest = "sha256:" + "1" * 64
    worker_digest = "sha256:" + "2" * 64
    _patch_github_latest(
        monkeypatch,
        tag="v2.1.0",
        url="https://github.com/acme/app/releases/tag/v2.1.0",
        extra={
            "https://api.github.com/repos/acme/worker/releases/latest": {
                "tag_name": "v3.4.0",
                "name": "v3.4.0",
                "html_url": "https://github.com/acme/worker/releases/tag/v3.4.0",
                "body": "Routine worker update",
                "published_at": "2026-01-02T00:00:00Z",
            }
        },
    )
    _patch_digest_resolution_map(
        monkeypatch,
        {
            "ghcr.io/acme/app:v2.1.0": app_digest,
            "ghcr.io/acme/worker:v3.4.0": worker_digest,
        },
    )
    headers = _csrf_headers(client)

    refresh = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=headers,
    )

    assert refresh.status_code == 200
    items = sorted(refresh.json()["items"], key=lambda item: item["image_repo"])
    assert [item["service_key"] for item in items] == ["stack/app", "stack/app"]
    assert len({item["target_id"] for item in items}) == 2
    assert [item["proposed_tag"] for item in items] == ["v2.1.0", "v3.4.0"]
    assert [item["final_image"] for item in items] == [
        "ghcr.io/acme/app:v2.1.0",
        "ghcr.io/acme/worker:v3.4.0",
    ]
    assert all(item["candidate_source"] == "github-latest" for item in items)

    choices = [
        _switch_choice(service_key=item["service_key"], target_id=item["target_id"])
        for item in items
    ]
    plan_response = client.post(
        "/api/v1/retag-plans",
        json={"choices": choices, "github_latest_fallback": True},
        headers=headers,
    )

    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["status"] == "ready"
    updates = [
        update
        for stack in plan["stacks"]
        for update in stack["tag_updates"]
    ]
    assert {update["target_id"] for update in updates} == {
        item["target_id"] for item in items
    }
    assert {
        (update["source_image"], update["target_tag"], update["final_image"])
        for update in updates
    } == {
        ("ghcr.io/acme/app:latest", "v2.1.0", "ghcr.io/acme/app:v2.1.0"),
        (
            "ghcr.io/acme/worker:latest",
            "v3.4.0",
            "ghcr.io/acme/worker:v3.4.0",
        ),
    }
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_github_latest_fallback_requires_matching_cache_info_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    settings = client.app.state.web_settings
    with open_db(settings.config.db_path) as conn:
        init_db(conn)
    stack = ComposeStack(
        index=0,
        directory=tmp_path / "docker",
        file="docker-compose.yml",
        name="stack",
        images=("ghcr.io/acme/app:latest", "ghcr.io/acme/worker:latest"),
        service_images=(
            ServiceImage(service="app", image="ghcr.io/acme/app:latest"),
            ServiceImage(service="worker", image="ghcr.io/acme/worker:latest"),
        ),
    )

    def fake_cached_release_notes(
        *_args: object,
        **_kwargs: object,
    ) -> list[ReleaseNoteInfo]:
        return [
            ReleaseNoteInfo(
                line_no=0,
                status="ready",
                provider="github",
                image_repo="ghcr.io/acme/app",
                upstream_repo="acme/app",
            )
        ]

    monkeypatch.setattr(
        web_retags_module,
        "cached_release_notes",
        fake_cached_release_notes,
    )

    with pytest.raises(ValueError):
        web_retags_module._cached_github_latest_fallback_by_target(
            settings,
            [stack],
            {},
        )


def test_retag_preview_uses_cached_selected_github_latest_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "ghcr.io/acme/app:latest", "cid-app")],
    )
    digest = "sha256:" + "d" * 64
    _patch_github_latest(
        monkeypatch,
        tag="v1.2.3",
        url="https://github.com/acme/app/releases/tag/v1.2.3",
    )
    _patch_digest_resolution(
        monkeypatch,
        expected_image="ghcr.io/acme/app:v1.2.3",
        digest=digest,
    )

    headers = _csrf_headers(client)
    refreshed = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=headers,
    )
    assert refreshed.status_code == 200

    def fail_refresh(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("preview must not refresh GitHub candidates")

    monkeypatch.setattr(web_retags_module, "refresh_release_notes", fail_refresh)
    resolved_images: list[str] = []

    class CountingDigestVerifier:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def resolve_tag_digest(self, image: str) -> DigestResolveResult:
            resolved_images.append(image)
            return DigestResolveResult(
                ok=True,
                status="resolved",
                reason="tag-digest-resolved",
                digest=digest,
                source="test",
            )

    monkeypatch.setattr(web_retags_module, "DigestVerifier", CountingDigestVerifier)
    (fake_root / "calls.log").write_text("", encoding="utf-8")

    missing_csrf = client.post(
        "/api/v1/retag-plans/preview",
        json={
            "choices": [{**_switch_choice(), "target_tag": "v1.2.3"}],
            "github_latest_fallback": True,
        },
    )
    started = client.post(
        "/api/v1/retag-plans/preview",
        json={
            "choices": [{**_switch_choice(), "target_tag": "v1.2.3"}],
            "github_latest_fallback": True,
        },
        headers=headers,
    )
    assert started.status_code == 202
    job = _wait_retag_preview_job(client, started.json()["preview_job_id"])

    assert missing_csrf.status_code == 403
    assert job["status"] == "success"
    assert [event["phase"] for event in job["progress"]] == ["preview", "preview"]
    plan = job["plan"]
    assert plan["status"] == "ready"
    assert plan["can_apply"] is True
    update = plan["stacks"][0]["tag_updates"][0]
    assert update["target_tag"] == "v1.2.3"
    assert update["final_image"] == "ghcr.io/acme/app:v1.2.3"
    assert resolved_images == ["ghcr.io/acme/app:v1.2.3"]
    calls = _fake_docker_calls(fake_root)
    assert calls.count("config --format json") == 1
    _assert_pending_grouping_did_not_mutate(calls)


def test_retag_preview_keeps_selected_tag_after_cached_candidate_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "ghcr.io/acme/app:latest", "cid-app")],
    )
    old_digest = "sha256:" + "e" * 64
    new_digest = "sha256:" + "f" * 64
    headers = _csrf_headers(client)
    _patch_github_latest(
        monkeypatch,
        tag="v1.0.0",
        url="https://github.com/acme/app/releases/tag/v1.0.0",
    )
    _patch_digest_resolution(
        monkeypatch,
        expected_image="ghcr.io/acme/app:v1.0.0",
        digest=old_digest,
    )
    cached = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=headers,
    )
    assert cached.status_code == 200

    _patch_github_latest(
        monkeypatch,
        tag="v1.1.0",
        url="https://github.com/acme/app/releases/tag/v1.1.0",
    )
    _patch_digest_resolution_map(
        monkeypatch,
        {
            "ghcr.io/acme/app:v1.0.0": old_digest,
            "ghcr.io/acme/app:v1.1.0": new_digest,
        },
    )

    refreshed = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=headers,
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["items"][0]["proposed_tag"] == "v1.1.0"

    started = client.post(
        "/api/v1/retag-plans/preview",
        json={
            "choices": [{**_switch_choice(), "target_tag": "v1.0.0"}],
            "github_latest_fallback": True,
        },
        headers=headers,
    )
    assert started.status_code == 202
    job = _wait_retag_preview_job(client, started.json()["preview_job_id"])

    assert job["status"] == "success"
    plan = job["plan"]
    assert plan["status"] == "ready"
    assert plan["can_apply"] is True
    update = plan["stacks"][0]["tag_updates"][0]
    assert update["target_tag"] == "v1.0.0"
    assert update["final_image"] == "ghcr.io/acme/app:v1.0.0"


def test_retag_apply_rejects_plan_when_fallback_flag_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "ghcr.io/acme/app:latest", "cid-app")],
    )
    _patch_github_latest(
        monkeypatch,
        tag="v1.2.3",
        url="https://github.com/acme/app/releases/tag/v1.2.3",
    )
    _patch_digest_resolution(
        monkeypatch,
        expected_image="ghcr.io/acme/app:v1.2.3",
        digest="sha256:" + "c" * 64,
    )
    headers = _csrf_headers(client)
    refresh = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=headers,
    )
    assert refresh.status_code == 200
    choices = [_switch_choice()]
    plan_response = client.post(
        "/api/v1/retag-plans",
        json={"choices": choices, "github_latest_fallback": True},
        headers=headers,
    )
    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["status"] == "ready"

    stale = client.post(
        "/api/v1/retag-plans/apply",
        json={
            "plan_id": plan["plan_id"],
            "choices": choices,
            "confirmation": "apply-retags",
        },
        headers=headers,
    )

    assert stale.status_code == 202
    job = _wait_apply_job(client, stale.json()["job_id"])
    assert job["status"] == "failure"
    assert job["error"] == "retag apply failed: retag plan is stale"
    assert job["progress"][-1]["phase"] == "preflight"


def test_retag_github_latest_fallback_uses_v_stripped_docker_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "ghcr.io/acme/app:latest", "cid-app")],
    )
    digest = "sha256:" + "9" * 64
    _patch_github_latest(
        monkeypatch,
        tag="v1.2.3",
        url="https://github.com/acme/app/releases/tag/v1.2.3",
    )
    _patch_digest_resolution_results(
        monkeypatch,
        {
            "ghcr.io/acme/app:v1.2.3": DigestResolveResult(
                ok=False,
                status="unavailable",
                reason="manifest-unavailable",
            ),
            "ghcr.io/acme/app:1.2.3": DigestResolveResult(
                ok=True,
                status="resolved",
                reason="tag-digest-resolved",
                digest=digest,
                source="test",
            ),
        },
    )

    response = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["retag_available"] is True
    assert item["retag_reason"] == "eligible"
    assert item["proposed_tag"] == "1.2.3"
    assert item["final_image"] == "ghcr.io/acme/app:1.2.3"
    assert "release tag v1.2.3 resolved as Docker tag 1.2.3" in item[
        "candidate_warning"
    ]
    assert item["candidate_link_url"].endswith("/v1.2.3")
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_github_latest_fallback_uses_v_prefixed_docker_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "ghcr.io/acme/app:latest", "cid-app")],
    )
    digest = "sha256:" + "8" * 64
    _patch_github_latest(
        monkeypatch,
        tag="1.2.3",
        url="https://github.com/acme/app/releases/tag/1.2.3",
    )
    _patch_digest_resolution_results(
        monkeypatch,
        {
            "ghcr.io/acme/app:1.2.3": DigestResolveResult(
                ok=False,
                status="unavailable",
                reason="manifest-unavailable",
            ),
            "ghcr.io/acme/app:v1.2.3": DigestResolveResult(
                ok=True,
                status="resolved",
                reason="tag-digest-resolved",
                digest=digest,
                source="test",
            ),
        },
    )

    response = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["retag_available"] is True
    assert item["retag_reason"] == "eligible"
    assert item["proposed_tag"] == "v1.2.3"
    assert item["final_image"] == "ghcr.io/acme/app:v1.2.3"
    assert "release tag 1.2.3 resolved as Docker tag v1.2.3" in item[
        "candidate_warning"
    ]
    assert item["candidate_link_url"].endswith("/1.2.3")
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_github_latest_fallback_uses_lsio_release_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream_map = tmp_path / "upstreams.txt"
    upstream_map.write_text(
        "linuxserver/docker-radarr: Radarr/Radarr\n",
        encoding="utf-8",
    )
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "UPSTREAM_MAP": str(upstream_map),
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("radarr", "ghcr.io/linuxserver/radarr:latest", "cid-radarr")],
    )
    digest = "sha256:" + "a" * 64
    _patch_digest_resolution(
        monkeypatch,
        expected_image="ghcr.io/linuxserver/radarr:5.1.0-ls1",
        digest=digest,
    )
    _patch_github_latest(
        monkeypatch,
        tag="5.1.0-ls1",
        url="https://github.com/linuxserver/docker-radarr/releases/tag/5.1.0-ls1",
        extra={
            "https://api.github.com/repos/Radarr/Radarr/releases/tags/v5.1.0": {
                "tag_name": "v5.1.0",
                "name": "v5.1.0",
                "html_url": "https://github.com/Radarr/Radarr/releases/tag/v5.1.0",
                "body": "Routine update",
                "published_at": "2026-01-02T00:00:00Z",
            }
        },
    )

    response = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["retag_available"] is True
    assert item["retag_reason"] == "eligible"
    assert item["proposed_tag"] == "5.1.0-ls1"
    assert item["final_image"] == "ghcr.io/linuxserver/radarr:5.1.0-ls1"
    assert "will update latest tracking to 5.1.0-ls1" in item["candidate_warning"]
    assert item["candidate_link_label"] == "LSIO release"
    assert item["candidate_link_url"].endswith("/5.1.0-ls1")
    assert item["digest_provenance"]["provenance_source"] == "github-latest"
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_github_latest_fallback_does_not_guess_lsio_upstream_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream_map = tmp_path / "upstreams.txt"
    upstream_map.write_text(
        "linuxserver/docker-radarr: Radarr/Radarr\n",
        encoding="utf-8",
    )
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "UPSTREAM_MAP": str(upstream_map),
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("radarr", "ghcr.io/linuxserver/radarr:latest", "cid-radarr")],
    )

    class FailDigestVerifier:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            # Test double accepts production constructor arguments but needs no setup.
            pass

        def resolve_tag_digest(self, image: str) -> DigestResolveResult:
            if image == "ghcr.io/linuxserver/radarr:v5.1.0":
                raise AssertionError("LSIO fallback should not use upstream tag")
            return DigestResolveResult(
                ok=False,
                status="unavailable",
                reason="manifest-unavailable",
            )

    monkeypatch.setattr(web_retags_module, "DigestVerifier", FailDigestVerifier)
    _patch_github_latest(
        monkeypatch,
        tag="5.1.0-ls1",
        url="https://github.com/linuxserver/docker-radarr/releases/tag/5.1.0-ls1",
        extra={
            "https://api.github.com/repos/Radarr/Radarr/releases/tags/v5.1.0": {
                "tag_name": "v5.1.0",
                "name": "v5.1.0",
                "html_url": "https://github.com/Radarr/Radarr/releases/tag/v5.1.0",
                "body": "Routine update",
                "published_at": "2026-01-02T00:00:00Z",
            }
        },
    )

    response = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["retag_available"] is False
    assert item["retag_reason"] == "missing-provenance"
    assert "5.1.0-ls1: manifest-unavailable" in item["candidate_warning"]
    assert item["choices"] == ["keep-current"]


def test_retag_refresh_and_preview_require_mutations_enabled(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    headers = _csrf_headers(client)

    refresh = client.post(
        "/api/v1/retag-targets/github-latest/refresh",
        headers=headers,
    )
    preview = client.post(
        "/api/v1/retag-plans/preview",
        json={"choices": [_switch_choice()]},
        headers=headers,
    )

    assert refresh.status_code == 403
    assert refresh.json()["detail"] == "mutations are disabled"
    assert preview.status_code == 403
    assert preview.json()["detail"] == "mutations are disabled"


def _patch_github_latest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tag: str,
    url: str,
    extra: dict[str, object] | None = None,
) -> None:
    responses = {
        "https://api.github.com/repos/acme/app/releases/latest": {
            "tag_name": tag,
            "name": tag,
            "html_url": url,
            "body": "Routine update",
            "published_at": "2026-01-02T00:00:00Z",
        },
        "https://api.github.com/repos/linuxserver/docker-radarr/releases/latest": {
            "tag_name": tag,
            "name": tag,
            "html_url": url,
            "body": "Remote Changes:\n- Updating to v5.1.0",
            "published_at": "2026-01-02T00:00:00Z",
        },
        **(extra or {}),
    }

    class FakeGitHubClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            # Test double accepts production constructor arguments but needs no setup.
            pass

        def get_json(self, request_url: str) -> object:
            return responses[request_url]

    monkeypatch.setattr(web_retags_module, "GitHubClient", FakeGitHubClient)


def test_retag_target_records_require_resolver_for_github_latest_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_database, "known_digest_state_by_service", lambda _settings: {})

    with pytest.raises(ValueError, match="requires a candidate resolver"):
        web_retag_targets._retag_target_records_for_stacks(
            object(),
            (),
            github_latest_fallback=True,
        )
