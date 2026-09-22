from __future__ import annotations

import urllib.parse
from pathlib import Path

from tests.web_plan_test_helpers import _seed_known_digest_provenance
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _install_wud_api,
    _make_fake_stack,
    _manifest_index_digest,
    _wait_apply_job,
    _write_fake_image_after_pull,
    _write_fake_manifest,
    _wud_api_container,
)

from wudup import web_jobs, web_plans, web_wud_transport
from wudup.db import open_db
from wudup.locks import DirectoryLock, WudLockError, lock_dir_for


def test_apply_endpoint_applies_digest_unpin_plan_and_records_provenance(
    tmp_path: Path,
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
    _seed_known_digest_provenance(tmp_path)
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest@sha256:new\n", encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app@sha256:old", "cid-app")],
    )
    compose_file = compose_dir / "docker-compose.yml"
    compose_file.write_text(
        "services:\n"
        "  app:\n"
        "    # wudup.resolved-tag=latest\n"
        "    image: repo/app@sha256:old\n"
        "    labels:\n"
        "      - wud.tag.include=^latest$\n",
        encoding="utf-8",
    )
    _write_fake_image_after_pull(
        fake_root,
        "repo/app:latest",
        "sha256:new-id",
        "sha256:new",
    )
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert plan["status"] == "ready"
    assert plan["stacks"][0]["digest_unpin_updates"][0]["tag_image"] == "repo/app:latest"
    assert apply_response.status_code == 202
    assert job["status"] == "success"
    rendered = compose_file.read_text(encoding="utf-8")
    assert "image: repo/app:latest" in rendered
    assert "wudup.resolved-tag" not in rendered
    assert "wud.tag.include=^latest$" in rendered
    assert wud_file.read_text(encoding="utf-8") == ""
    calls = _fake_docker_calls(fake_root)
    assert " pull app" in calls
    assert " up -d --remove-orphans --pull never --no-build --no-deps" in calls
    assert " app" in calls
    db_path = tmp_path / "state" / "wud.sqlite"
    with open_db(db_path) as conn:
        pending = conn.execute("SELECT * FROM pending_updates").fetchone()
        event = conn.execute("SELECT * FROM update_events").fetchone()
        known = conn.execute("SELECT * FROM known_images WHERE service_key = 'stack/app'").fetchone()
    assert pending["status"] == "resolved"
    assert pending["digest_source_image"] == "repo/app@sha256:old"
    assert pending["digest_resolved_tag"] == "latest"
    assert pending["digest_target_digest"] == "sha256:new"
    assert event["image"] == "repo/app@sha256:old"
    assert event["target_image"] == "repo/app:latest"
    assert event["old_digest"] == "sha256:old"
    assert event["new_digest"] == "sha256:new"
    assert known["image"] == "repo/app:latest"
    assert known["digest_source_image"] == "repo/app@sha256:old"
    assert known["digest_target_digest"] == "sha256:new"
    assert known["digest_provenance_source"] == "apply"


def test_apply_endpoint_requires_and_uses_digest_pin_label_rewrite_approval(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    target_image = "repo/app:2.0"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_DIGEST_PIN_UPDATES": "true",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:1.0 tag=2.0\n", encoding="utf-8")
    _write_fake_manifest(
        fake_root,
        "docker.io/repo/app:2.0",
        _manifest_index_digest("sha256:index", "sha256:child"),
    )
    _write_fake_image_after_pull(
        fake_root,
        target_image,
        "sha256:config",
        "sha256:index",
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:1.0", "cid-app")],
    )
    compose_file = compose_dir / "docker-compose.yml"
    compose_file.write_text(
        compose_file.read_text(encoding="utf-8").replace(
            "  app:\n    image: repo/app:1.0\n",
            "  app:\n"
            "    labels:\n"
            "      - wud.tag.include=^beta|^stable\n"
            "    image: repo/app:1.0\n",
        ),
        encoding="utf-8",
    )
    headers = _csrf_headers(client)
    initial = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1], "allow_tag_updates": True},
        headers=headers,
    ).json()
    issue = next(
        item
        for item in initial["issues"]
        if item["code"] == "compose-digest-pin-label-rewrite-unapproved"
    )
    approval = {
        "stack": issue["details"]["stack"],
        "service": issue["details"]["service"],
        "label_key": issue["details"]["label_key"],
        "current_label_value": issue["details"]["current_label_value"],
        "planned_tag": issue["details"]["planned_tag"],
        "proposed_label_value": issue["details"]["proposed_label_value"],
    }
    plan = client.post(
        "/api/v1/plans",
        json={
            "line_numbers": [1],
            "allow_tag_updates": True,
            "digest_pin_label_rewrite_approvals": [approval],
        },
        headers=headers,
    ).json()

    stale_apply = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "allow_tag_updates": True,
            "confirmation": "apply",
        },
        headers=headers,
    )
    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "allow_tag_updates": True,
            "digest_pin_label_rewrite_approvals": [approval],
            "confirmation": "apply",
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert stale_apply.status_code == 409
    assert stale_apply.json()["detail"] == "plan is stale"
    assert apply_response.status_code == 202
    assert job["status"] == "success"
    content = compose_file.read_text(encoding="utf-8")
    assert "# wudup.resolved-tag=2.0" in content
    assert "image: repo/app@sha256:index" in content
    assert "wud.tag.include=^2\\.0$$" in content


def test_apply_endpoint_runs_existing_updater_and_records_audit(
    tmp_path: Path,
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
    wud_file = tmp_path / "state" / "images.todo"
    original = "repo/app:latest\nrepo/db:latest\n"
    wud_file.write_text(original, encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("db", "repo/db:latest", "cid-db"),
        ],
    )
    image_state = fake_root / "images" / "repo_app_latest.id"
    image_state.write_text("old\n", encoding="utf-8")
    (fake_root / "images" / "repo_app_latest.after_id").write_text(
        "new\n",
        encoding="utf-8",
    )
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert apply_response.status_code == 202
    assert apply_response.json()["status"] == "queued"
    assert plan["can_apply"] is True
    assert job["status"] == "success"
    assert job["run_id"]
    assert job["selected_line_numbers"] == [1]
    assert wud_file.read_text(encoding="utf-8") == "repo/db:latest\n"
    calls = _fake_docker_calls(fake_root)
    assert "compose -f docker-compose.yml pull app" in calls
    assert "compose -f docker-compose.yml stop app" in calls
    assert "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps app" in calls

    detail = client.get(f"/api/v1/runs/{job['run_id']}").json()
    assert detail["metadata"]["source"] == "webui"
    assert detail["metadata"]["plan_id"] == plan["plan_id"]
    assert detail["metadata"]["selected_line_numbers"] == [1]
    assert detail["pending_updates"][0]["line_no"] == 1
    assert detail["pending_updates"][0]["status"] == "resolved"
    assert not lock_dir_for(wud_file).exists()


def test_apply_endpoint_uses_api_pending_source_without_editing_wud_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest="sha256:new",
            update_kind="digest",
        )
    ]
    _install_wud_api(
        monkeypatch,
        containers=containers,
    )
    wud_api_posts: list[str] = []

    def fake_post_json(url: str, _client_config=None, **_kwargs) -> object:
        path = urllib.parse.urlsplit(url).path
        wud_api_posts.append(path)
        if path == "/api/containers/docker.local.app/watch":
            containers.clear()
        return {"status": "ok"}

    monkeypatch.setattr(web_wud_transport, "_post_json", fake_post_json)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.apply-api-source.test:3000",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/file:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("worker", "repo/app:stable", "cid-worker"),
        ],
    )
    _write_fake_image_after_pull(
        fake_root,
        "repo/app:latest",
        "new",
        "sha256:new",
    )
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert plan["source"]["active"] == "api"
    assert plan["targets"][0]["raw"] == "repo/app:latest@sha256:new"
    assert apply_response.status_code == 202
    assert job["status"] == "success"
    assert wud_file.read_text(encoding="utf-8") == "repo/file:latest\n"
    calls = _fake_docker_calls(fake_root)
    assert "compose -f docker-compose.yml pull app" in calls
    assert "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps app" in calls
    assert "worker" not in calls
    detail = client.get(f"/api/v1/runs/{job['run_id']}").json()
    assert detail["metadata"]["pending_source"] == "api"
    assert detail["metadata"]["pending_source_configured"] == "api"
    assert detail["metadata"]["pending_source_label"] == "WUD API"
    assert wud_api_posts == ["/api/containers/docker.local.app/watch"]
    assert "/api/containers/watch" not in wud_api_posts
    pending = client.get("/api/v1/pending").json()
    assert pending["source"]["active"] == "api"
    assert pending["count"] == 0


def test_apply_endpoint_rejects_degraded_api_last_good_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest="sha256:new",
            update_kind="digest",
        )
    ]
    _install_wud_api(monkeypatch, containers=containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.apply-api-degraded.test:3000",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    initial = client.get("/api/v1/pending")
    assert initial.status_code == 200
    assert initial.json()["count"] == 1

    containers[0]["updateAvailable"] = False
    containers[0]["result"] = None
    containers[0]["error"] = {"message": "registry lookup failed"}

    degraded = client.get("/api/v1/pending")
    assert degraded.status_code == 200
    assert degraded.json()["count"] == 1
    assert degraded.json()["source"]["degraded"] is True
    assert degraded.json()["items"][0]["metadata_status"] == "retained"

    headers = _csrf_headers(client)
    plan_response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    )
    plan = plan_response.json()

    assert plan_response.status_code == 200
    assert plan["status"] == "ready"
    assert plan["source"]["active"] == "api"
    assert plan["source"]["degraded"] is True
    assert plan["can_apply"] is False
    metadata_check = next(
        check
        for check in plan["apply_preflight"]["checks"]
        if check["code"] == "wud-metadata-current"
    )
    assert metadata_check == {
        "status": "FAIL",
        "code": "wud-metadata-current",
        "label": "Selected update metadata",
        "detail": (
            "1 selected update is blocked because its metadata is stale (retained). "
            "Check your WUD configuration, then run a successful WUD scan."
        ),
        "source_check_codes": ["wud-api-observations"],
    }
    assert plan["targets"][0]["metadata_status"] == "retained"

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )

    assert apply_response.status_code == 409
    assert apply_response.json()["detail"] == "plan is not ready to apply"
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls


def test_plan_allows_fresh_update_with_unrelated_unsupported_registry_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest="sha256:new",
            update_kind="digest",
        )
    ]
    unsupported = _wud_api_container(
        name="socket-proxy",
        image="linuxserver/socket-proxy",
        update_available=False,
        update_kind="unknown",
    )
    unsupported["result"] = None
    unsupported["error"] = {"message": "Unsupported Registry unknown"}
    containers.append(unsupported)
    _install_wud_api(monkeypatch, containers=containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.apply-api-unsupported.test:3000",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    pending = client.get("/api/v1/pending")
    headers = _csrf_headers(client)
    plan_response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    )
    plan = plan_response.json()

    assert pending.status_code == 200
    assert pending.json()["source"]["degraded"] is False
    assert plan_response.status_code == 200
    assert plan["status"] == "ready"
    assert plan["source"]["active"] == "api"
    assert plan["source"]["degraded"] is False
    assert plan["can_apply"] is True
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls


def test_apply_allows_fresh_update_with_unrelated_degraded_observation_in_auto_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest="sha256:new",
            update_kind="digest",
        ),
        _wud_api_container(
            name="broken",
            image="repo/broken:latest",
            tag="latest",
            remote_tag="",
            remote_digest="sha256:broken-new",
            update_kind="digest",
        ),
    ]
    _install_wud_api(monkeypatch, containers=containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "auto",
            "WUD_API_BASE_URL": "https://wud.apply-api-mixed.test:3000",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    headers = _csrf_headers(client)
    pending = client.get("/api/v1/pending").json()
    assert pending["source"]["degraded"] is False
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()
    containers[1]["updateAvailable"] = False
    containers[1]["result"] = None
    containers[1]["error"] = {"message": "registry lookup failed"}

    metadata_check = next(
        check
        for check in plan["apply_preflight"]["checks"]
        if check["code"] == "wud-metadata-current"
    )
    assert plan["can_apply"] is True
    assert plan["targets"][0]["metadata_status"] == "fresh"
    assert metadata_check["status"] == "PASS"

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )

    assert apply_response.status_code == 202
    job = _wait_apply_job(client, apply_response.json()["job_id"])
    assert job["status"] == "success"
    refreshed = client.get("/api/v1/pending").json()
    assert refreshed["source"]["active"] == "api"
    assert refreshed["source"]["degraded"] is True
    calls = _fake_docker_calls(fake_root)
    assert "compose -f docker-compose.yml pull app" in calls
    assert "broken" not in calls


def test_plan_blocks_fresh_update_with_unresolved_matching_container(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest="sha256:new",
            update_kind="digest",
        ),
        _wud_api_container(
            name="app-shadow",
            image="repo/app",
            tag="latest",
            remote_tag="",
            remote_digest="sha256:new",
            update_kind="digest",
            update_available=False,
        ),
    ]
    containers[1]["result"] = None
    containers[1]["error"] = {"message": "registry lookup failed"}
    _install_wud_api(monkeypatch, containers=containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "auto",
            "WUD_API_BASE_URL": "https://wud.apply-api-related.test:3000",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("app-shadow", "repo/app:latest", "cid-app-shadow"),
        ],
    )

    pending = client.get("/api/v1/pending").json()
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    ).json()

    assert pending["source"]["degraded"] is True
    assert pending["items"][0]["metadata_status"] == "retained"
    assert plan["can_apply"] is False
    assert plan["targets"][0]["metadata_status"] == "retained"
    metadata_check = next(
        check
        for check in plan["apply_preflight"]["checks"]
        if check["code"] == "wud-metadata-current"
    )
    assert metadata_check["status"] == "FAIL"
    assert "1 selected update is blocked" in metadata_check["detail"]
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls


def test_shared_apply_scope_uses_least_fresh_container_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest="sha256:app-new",
            update_kind="digest",
        ),
        _wud_api_container(
            name="app-shadow",
            image="repo/app",
            tag="latest",
            remote_tag="",
            remote_digest="sha256:shadow-new",
            update_kind="digest",
        ),
    ]
    _install_wud_api(monkeypatch, containers=containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.apply-api-shared-scope.test:3000",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("app-shadow", "repo/app:latest", "cid-app-shadow"),
        ],
    )

    initial = client.get("/api/v1/pending").json()
    containers[1]["updateAvailable"] = False
    containers[1]["result"] = None
    containers[1]["error"] = {"message": "registry lookup failed"}
    degraded = client.get("/api/v1/pending").json()
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    ).json()

    assert [item["metadata_status"] for item in initial["items"]] == [
        "fresh",
        "fresh",
    ]
    assert [item["metadata_status"] for item in degraded["items"]] == [
        "retained",
        "retained",
    ]
    assert all(
        item["source_id"] == "docker.local.app,docker.local.app-shadow"
        for item in degraded["items"]
    )
    assert plan["can_apply"] is False
    assert plan["targets"][0]["metadata_status"] == "retained"


def test_apply_rejects_plan_when_selected_wud_container_identity_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest="sha256:new",
            update_kind="digest",
        )
    ]
    _install_wud_api(monkeypatch, containers=containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.apply-api-identity.test:3000",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()
    containers[0]["id"] = "docker.local.replaced-app"

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )

    assert plan["can_apply"] is True
    assert apply_response.status_code == 409
    assert apply_response.json()["detail"] == "plan is stale"
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls


def test_plan_blocks_mixed_fresh_and_retained_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest="sha256:app-new",
            update_kind="digest",
        ),
        _wud_api_container(
            name="worker",
            image="repo/worker:latest",
            tag="latest",
            remote_tag="",
            remote_digest="sha256:worker-new",
            update_kind="digest",
        ),
    ]
    _install_wud_api(monkeypatch, containers=containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.apply-api-retained-mixed.test:3000",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("worker", "repo/worker:latest", "cid-worker"),
        ],
    )

    assert client.get("/api/v1/pending").json()["count"] == 2
    containers[1]["updateAvailable"] = False
    containers[1]["result"] = None
    containers[1]["error"] = {"message": "registry lookup failed"}
    pending = client.get("/api/v1/pending").json()
    assert [item["metadata_status"] for item in pending["items"]] == [
        "fresh",
        "retained",
    ]

    headers = _csrf_headers(client)
    mixed = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1, 2]},
        headers=headers,
    ).json()
    fresh_only = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()

    assert mixed["can_apply"] is False
    assert [target["metadata_status"] for target in mixed["targets"]] == [
        "fresh",
        "retained",
    ]
    mixed_metadata_check = next(
        check
        for check in mixed["apply_preflight"]["checks"]
        if check["code"] == "wud-metadata-current"
    )
    assert mixed_metadata_check["status"] == "FAIL"
    assert "1 selected update is blocked" in mixed_metadata_check["detail"]

    assert fresh_only["can_apply"] is True
    fresh_metadata_check = next(
        check
        for check in fresh_only["apply_preflight"]["checks"]
        if check["code"] == "wud-metadata-current"
    )
    assert fresh_metadata_check["status"] == "WARN"
    assert fresh_metadata_check["detail"] == (
        "Every selected update has current information. The last WUD "
        "update check failed for other containers. "
        "View affected containers for details."
    )


def test_apply_endpoint_rejects_stale_api_pending_source_without_editing_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest=f"sha256:{'b' * 64}",
            update_kind="digest",
        )
    ]
    _install_wud_api(monkeypatch, containers=containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.apply-api-stale.test:3000",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/file:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()
    containers[:] = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest=f"sha256:{'a' * 64}",
            update_kind="digest",
        )
    ]

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )

    assert apply_response.status_code == 409
    assert apply_response.json()["detail"] == "plan is stale"
    assert wud_file.read_text(encoding="utf-8") == "repo/file:latest\n"
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls


def test_apply_endpoint_wraps_api_pending_source_oserror_without_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    redaction_value = "api-apply-redaction-value"
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = [
        _wud_api_container(
            tag="latest",
            remote_tag="",
            remote_digest=f"sha256:{'b' * 64}",
            update_kind="digest",
        )
    ]
    _install_wud_api(monkeypatch, containers=containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.apply-api-oserror.test:3000",
            "WUD_WEB_TOKEN": redaction_value,
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/file:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()
    calls_before = _fake_docker_calls(fake_root)

    def fail_pending_source_resolution(*_args, **_kwargs):
        raise OSError(
            f"could not read {tmp_path / 'state' / 'api-redaction-path'} "
            f"with {redaction_value}"
        )

    monkeypatch.setattr(
        web_plans.web_pending_sources,
        "resolve_pending_source",
        fail_pending_source_resolution,
    )

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )

    assert apply_response.status_code == 500
    detail = apply_response.json()["detail"]
    assert detail.startswith("could not revalidate plan: ")
    assert redaction_value not in detail
    assert str(tmp_path) not in detail
    assert "<redacted>" in detail
    assert "[REDACTED_PATH]" in detail
    assert wud_file.read_text(encoding="utf-8") == "repo/file:latest\n"
    assert _fake_docker_calls(fake_root) == calls_before


def test_apply_endpoint_wraps_plan_revalidation_oserror_without_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    redaction_value = "api-revalidation-redaction-value"
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_TOKEN": redaction_value,
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()
    calls_before = _fake_docker_calls(fake_root)

    def fail_build_web_plan(*_args, **_kwargs):
        raise OSError(
            f"could not read {tmp_path / 'state' / 'plan-redaction-path'} "
            f"with {redaction_value}"
        )

    monkeypatch.setattr(web_plans, "build_web_plan", fail_build_web_plan)

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )

    assert apply_response.status_code == 500
    detail = apply_response.json()["detail"]
    assert detail.startswith("could not revalidate plan: ")
    assert redaction_value not in detail
    assert str(tmp_path) not in detail
    assert "<redacted>" in detail
    assert "[REDACTED_PATH]" in detail
    assert wud_file.read_text(encoding="utf-8") == "repo/app:latest\n"
    assert _fake_docker_calls(fake_root) == calls_before
    assert not lock_dir_for(wud_file).exists()


def test_apply_endpoint_passes_tag_overrides_to_updater(tmp_path: Path) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:1.0 tag=wrong\n", encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:1.0", "cid-app")],
    )
    (fake_root / "images" / "repo_app_3.0.after_id").write_text(
        "new\n",
        encoding="utf-8",
    )
    headers = _csrf_headers(client)
    payload = {
        "line_numbers": [1],
        "allow_tag_updates": True,
        "tag_overrides": [{"line_no": 1, "tag": "3.0"}],
    }
    plan = client.post(
        "/api/v1/plans",
        json=payload,
        headers=headers,
    ).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            **payload,
            "confirmation": "apply",
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert apply_response.status_code == 202
    assert job["status"] == "success"
    assert wud_file.read_text(encoding="utf-8") == ""
    assert "image: repo/app:3.0" in (
        compose_dir / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    calls = _fake_docker_calls(fake_root)
    assert "manifest inspect repo/app:3.0" in calls
    assert "compose -f docker-compose.yml pull app" in calls


def test_apply_endpoint_applies_stream_image_and_label_as_one_plan(
    tmp_path: Path,
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
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text(
        "n8nio/runners:2.33.5-distroless tag=2.34.4\n",
        encoding="utf-8",
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis",
        [("task-runner", "n8nio/runners:2.33.5-distroless", "cid-task")],
    )
    (fake_root / "images" / "n8nio_runners_2.34.4-distroless.after_id").write_text(
        "new\n",
        encoding="utf-8",
    )
    headers = _csrf_headers(client)
    payload = {
        "line_numbers": [1],
        "allow_tag_updates": True,
        "tag_stream_decisions": [{"line_no": 1, "decision": "preserve"}],
    }
    plan = client.post("/api/v1/plans", json=payload, headers=headers).json()

    stale_apply = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "allow_tag_updates": True,
            "confirmation": "apply",
        },
        headers=headers,
    )
    apply_response = client.post(
        "/api/v1/jobs",
        json={"plan_id": plan["plan_id"], **payload, "confirmation": "apply"},
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert stale_apply.status_code == 409
    assert stale_apply.json()["detail"] == "plan is stale"
    assert apply_response.status_code == 202
    assert job["status"] == "success"
    rendered = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: n8nio/runners:2.34.4-distroless" in rendered
    assert r"wud.tag.include=^\d+\.\d+\.\d+-distroless$$" in rendered
    assert wud_file.read_text(encoding="utf-8") == ""


def test_apply_endpoint_preserves_stream_only_for_candidate_service(
    tmp_path: Path,
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
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text(
        "n8nio/runners:2.33.5-distroless tag=2.34.4\n",
        encoding="utf-8",
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis",
        [
            (
                "distroless",
                "n8nio/runners:2.33.5-distroless",
                "cid-distroless",
            ),
            ("default", "n8nio/runners:2.33.5", "cid-default"),
        ],
    )
    for image in (
        "n8nio_runners_2.34.4-distroless.after_id",
        "n8nio_runners_2.34.4.after_id",
    ):
        (fake_root / "images" / image).write_text("new\n", encoding="utf-8")
    headers = _csrf_headers(client)
    payload = {
        "line_numbers": [1],
        "allow_tag_updates": True,
        "tag_stream_decisions": [{"line_no": 1, "decision": "preserve"}],
    }
    plan = client.post("/api/v1/plans", json=payload, headers=headers).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={"plan_id": plan["plan_id"], **payload, "confirmation": "apply"},
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert plan["status"] == "ready"
    assert len(plan["stacks"][0]["tag_stream_updates"]) == 1
    assert apply_response.status_code == 202
    assert job["status"] == "success"
    rendered = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert "    image: n8nio/runners:2.34.4-distroless\n" in rendered
    assert "    image: n8nio/runners:2.33.5\n" in rendered
    assert rendered.count(r"wud.tag.include=^\d+\.\d+\.\d+-distroless$$") == 1
    assert wud_file.read_text(encoding="utf-8") == ""


def test_apply_endpoint_coalesces_duplicate_stream_entries(tmp_path: Path) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_MAX_WAIT": "0",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    line = "n8nio/runners:2.33.5-distroless tag=2.34.4\n"
    wud_file.write_text(line * 2, encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis",
        [("task-runner", "n8nio/runners:2.33.5-distroless", "cid-task")],
    )
    (fake_root / "images" / "n8nio_runners_2.34.4-distroless.after_id").write_text(
        "new\n",
        encoding="utf-8",
    )
    headers = _csrf_headers(client)
    payload = {
        "line_numbers": [1, 2],
        "allow_tag_updates": True,
        "tag_stream_decisions": [
            {"line_no": 1, "decision": "preserve"},
            {"line_no": 2, "decision": "preserve"},
        ],
    }
    plan = client.post("/api/v1/plans", json=payload, headers=headers).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={"plan_id": plan["plan_id"], **payload, "confirmation": "apply"},
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert plan["status"] == "ready"
    assert len(plan["stacks"][0]["tag_stream_updates"]) == 2
    assert apply_response.status_code == 202
    assert job["status"] == "success"
    rendered = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert rendered.count("    image: n8nio/runners:2.34.4-distroless\n") == 1
    assert rendered.count(r"wud.tag.include=^\d+\.\d+\.\d+-distroless$$") == 1
    assert wud_file.read_text(encoding="utf-8") == ""


def test_apply_endpoint_preserves_stream_rule_when_digest_pinning(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    target_image = "n8nio/runners:2.34.4-distroless"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_DIGEST_PIN_UPDATES": "true",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text(
        "n8nio/runners:2.33.5-distroless tag=2.34.4\n",
        encoding="utf-8",
    )
    _write_fake_manifest(
        fake_root,
        f"docker.io/{target_image}",
        _manifest_index_digest("sha256:index", "sha256:child"),
    )
    _write_fake_image_after_pull(
        fake_root,
        target_image,
        "sha256:config",
        "sha256:index",
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis",
        [("task-runner", "n8nio/runners:2.33.5-distroless", "cid-task")],
    )
    headers = _csrf_headers(client)
    payload = {
        "line_numbers": [1],
        "allow_tag_updates": True,
        "tag_stream_decisions": [{"line_no": 1, "decision": "preserve"}],
    }
    plan_response = client.post(
        "/api/v1/plans",
        json=payload,
        headers=headers,
    )
    plan = plan_response.json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={"plan_id": plan["plan_id"], **payload, "confirmation": "apply"},
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert plan_response.status_code == 200
    assert plan["status"] == "ready"
    assert not any(
        issue["code"] == "compose-digest-pin-label-rewrite-unapproved"
        for issue in plan["issues"]
    )
    assert apply_response.status_code == 202
    assert job["status"] == "success"
    rendered = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: n8nio/runners@sha256:index" in rendered
    assert r"wud.tag.include=^\d+\.\d+\.\d+-distroless$$" in rendered
    assert wud_file.read_text(encoding="utf-8") == ""


def test_apply_endpoint_keeps_same_named_nested_stacks_separate(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_MAX_WAIT": "0",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text(
        "n8nio/runners:2.33.5-distroless tag=2.34.4\n",
        encoding="utf-8",
    )
    first_source = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis-a",
        [("task-runner", "n8nio/runners:2.33.5-distroless", "cid-a")],
    )
    second_source = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis-b",
        [("task-runner", "n8nio/runners:2.33.5-distroless", "cid-b")],
    )
    first_stack = tmp_path / "docker" / "primary" / "jarvis"
    second_stack = tmp_path / "docker" / "secondary" / "jarvis"
    first_stack.parent.mkdir(parents=True)
    second_stack.parent.mkdir(parents=True)
    first_source.rename(first_stack)
    second_source.rename(second_stack)
    runtime_path = fake_root / "compose-runtime.tsv"
    runtime_path.write_text(
        runtime_path.read_text(encoding="utf-8")
        .replace(str(first_source), str(first_stack))
        .replace(str(second_source), str(second_stack))
        .replace("\tjarvis-a\t", "\tjarvis\t")
        .replace("\tjarvis-b\t", "\tjarvis\t"),
        encoding="utf-8",
    )
    (fake_root / "images" / "n8nio_runners_2.34.4-distroless.after_id").write_text(
        "new\n",
        encoding="utf-8",
    )
    headers = _csrf_headers(client)
    payload = {
        "line_numbers": [1],
        "allow_tag_updates": True,
        "tag_stream_decisions": [{"line_no": 1, "decision": "preserve"}],
    }
    plan = client.post(
        "/api/v1/plans",
        json=payload,
        headers=headers,
    ).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={"plan_id": plan["plan_id"], **payload, "confirmation": "apply"},
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert [stack["name"] for stack in plan["stacks"]] == ["jarvis", "jarvis"]
    assert apply_response.status_code == 202
    assert job["status"] == "success"
    for stack in (first_stack, second_stack):
        rendered = (stack / "docker-compose.yml").read_text(encoding="utf-8")
        assert "image: n8nio/runners:2.34.4-distroless" in rendered
        assert r"wud.tag.include=^\d+\.\d+\.\d+-distroless$$" in rendered
    assert wud_file.read_text(encoding="utf-8") == ""


def test_apply_endpoint_routes_same_directory_compose_files_separately(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_MAX_WAIT": "0",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text(
        "n8nio/runners:2.33.5-distroless tag=2.34.4\n",
        encoding="utf-8",
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis",
        [("task-runner", "n8nio/runners:2.33.5-distroless", "cid-task")],
    )
    alternate_compose = compose_dir / "compose.yml"
    alternate_compose.write_text(
        (compose_dir / "docker-compose.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with (fake_root / "compose-runtime.tsv").open("a", encoding="utf-8") as file:
        file.write(
            f"{compose_dir}\t{alternate_compose}\t"
            "jarvis\ttask-runner\tFalse\n"
        )
    (fake_root / "images" / "n8nio_runners_2.34.4-distroless.after_id").write_text(
        "new\n",
        encoding="utf-8",
    )
    headers = _csrf_headers(client)
    payload = {
        "line_numbers": [1],
        "allow_tag_updates": True,
        "tag_stream_decisions": [{"line_no": 1, "decision": "preserve"}],
    }
    plan = client.post("/api/v1/plans", json=payload, headers=headers).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={"plan_id": plan["plan_id"], **payload, "confirmation": "apply"},
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert plan["status"] == "ready"
    assert [stack["compose_file"] for stack in plan["stacks"]] == [
        "compose.yml",
        "docker-compose.yml",
    ]
    assert apply_response.status_code == 202
    assert job["status"] == "success"
    for compose_file in (alternate_compose, compose_dir / "docker-compose.yml"):
        rendered = compose_file.read_text(encoding="utf-8")
        assert "image: n8nio/runners:2.34.4-distroless" in rendered
        assert r"wud.tag.include=^\d+\.\d+\.\d+-distroless$$" in rendered
    assert wud_file.read_text(encoding="utf-8") == ""


def test_apply_endpoint_rejects_stale_stream_label_approval(
    tmp_path: Path,
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
    (tmp_path / "state" / "images.todo").write_text(
        "n8nio/runners:2.33.5-distroless tag=2.34.4\n",
        encoding="utf-8",
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis",
        [("task-runner", "n8nio/runners:2.33.5-distroless", "cid-task")],
    )
    compose_path = compose_dir / "docker-compose.yml"
    compose_path.write_text(
        "services:\n"
        "  task-runner:\n"
        "    image: n8nio/runners:2.33.5-distroless\n"
        "    labels:\n"
        "      wud.tag.include: ^stable-.+$$\n",
        encoding="utf-8",
    )
    headers = _csrf_headers(client)
    decision = {"line_no": 1, "decision": "preserve"}
    blocked = client.post(
        "/api/v1/plans",
        json={
            "line_numbers": [1],
            "allow_tag_updates": True,
            "tag_stream_decisions": [decision],
        },
        headers=headers,
    ).json()
    issue = next(
        item
        for item in blocked["issues"]
        if item["code"] == "compose-tag-stream-label-rewrite-unapproved"
    )
    approval = {
        "line_no": 1,
        "stack": issue["stack"],
        "stack_directory": issue["details"]["stack_directory"],
        "compose_file": issue["details"]["compose_file"],
        "service": issue["service"],
        "label_key": issue["details"]["label_key"],
        "current_label_value": issue["details"]["current_label_value"],
        "selected_tag": issue["details"]["selected_tag"],
        "proposed_label_value": issue["details"]["proposed_label_value"],
    }
    plan_payload = {
        "line_numbers": [1],
        "allow_tag_updates": True,
        "tag_stream_decisions": [decision],
        "tag_stream_label_rewrite_approvals": [approval],
    }
    plan = client.post(
        "/api/v1/plans",
        json=plan_payload,
        headers=headers,
    ).json()
    compose_path.write_text(
        compose_path.read_text(encoding="utf-8").replace(
            "^stable-.+$$",
            "^beta-.+$$",
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/api/v1/jobs",
        json={"plan_id": plan["plan_id"], **plan_payload, "confirmation": "apply"},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "plan is stale"
    rendered = compose_path.read_text(encoding="utf-8")
    assert "image: n8nio/runners:2.33.5-distroless" in rendered
    assert "wud.tag.include: ^beta-.+$$" in rendered


def test_apply_endpoint_holds_wud_lock_for_worker_handoff(
    tmp_path: Path,
    monkeypatch,
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
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    observed: dict[str, object] = {}

    def fake_run(runner: object) -> int:
        environ = runner.environ
        observed["lock_flag"] = environ.get("WUD_LOCK_HELD_BY_PARENT")
        observed["lock_exists"] = lock_dir_for(wud_file).is_dir()
        contender = DirectoryLock(wud_file, timeout_seconds=0)
        try:
            contender.acquire()
        except WudLockError:
            observed["contended"] = True
        else:
            contender.close()
            observed["contended"] = False
        return 0

    monkeypatch.setattr(web_jobs.UpdateFromWudRunner, "run", fake_run)
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert apply_response.status_code == 202
    assert job["status"] == "success"
    assert observed == {
        "lock_flag": "1",
        "lock_exists": True,
        "contended": True,
    }
    assert not lock_dir_for(wud_file).exists()
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls


def test_apply_endpoint_marks_job_terminal_when_wud_lock_cleanup_raises(
    tmp_path: Path,
    monkeypatch,
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
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    close_calls = 0
    original_close = DirectoryLock.close

    def close_then_raise(lock: DirectoryLock) -> None:
        nonlocal close_calls
        close_calls += 1
        original_close(lock)
        raise RuntimeError("lock cleanup exploded")

    monkeypatch.setattr(DirectoryLock, "close", close_then_raise)
    monkeypatch.setattr(web_jobs.UpdateFromWudRunner, "run", lambda _runner: 0)
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert apply_response.status_code == 202
    assert job["status"] == "success"
    assert job["error"] == ""
    assert close_calls == 1
    assert not lock_dir_for(wud_file).exists()
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls


def test_apply_endpoint_releases_wud_lock_when_runner_raises(
    tmp_path: Path,
    monkeypatch,
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
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    def fake_run(_runner: object) -> int:
        assert lock_dir_for(wud_file).is_dir()
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(web_jobs.UpdateFromWudRunner, "run", fake_run)
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert apply_response.status_code == 202
    assert job["status"] == "failure"
    assert job["error"] == "runner exploded"
    assert not lock_dir_for(wud_file).exists()
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls


def test_apply_endpoint_releases_wud_lock_when_pending_source_reread_fails(
    tmp_path: Path,
    monkeypatch,
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
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()
    original_resolve = web_plans.web_pending_sources.resolve_pending_source
    calls = 0

    def fail_second_source_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("pending source re-read failed")
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(
        web_plans.web_pending_sources,
        "resolve_pending_source",
        fail_second_source_read,
    )

    response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "could not revalidate plan: pending source re-read failed"
    )
    assert calls == 2
    assert not lock_dir_for(wud_file).exists()
    assert wud_file.read_text(encoding="utf-8") == "repo/app:latest\n"
    calls_text = _fake_docker_calls(fake_root)
    assert " pull " not in calls_text
    assert " up -d " not in calls_text


def test_apply_endpoint_reports_updater_failure_and_preserves_line(
    tmp_path: Path,
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
    wud_file = tmp_path / "state" / "images.todo"
    original = "repo/app:latest\n"
    wud_file.write_text(original, encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    (fake_root / "stacks" / "stack" / "pull_fail").write_text("", encoding="utf-8")
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()

    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])

    assert apply_response.status_code == 202
    assert job["status"] == "failure"
    assert job["run_id"]
    assert "updater exited with status 1" in str(job["error"])
    assert wud_file.read_text(encoding="utf-8") == original
    detail = client.get(f"/api/v1/runs/{job['run_id']}").json()
    assert detail["status"] == "failure"
    assert detail["pending_updates"][0]["status"] == "failed"


def test_legacy_apply_routes_remain_compatible(
    tmp_path: Path,
    monkeypatch,
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
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    monkeypatch.setattr(web_jobs.UpdateFromWudRunner, "run", lambda _runner: 0)
    headers = _csrf_headers(client)
    plan = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    ).json()

    apply_response = client.post(
        "/api/v1/plans/apply",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])
    legacy_status = client.get(f"/api/v1/apply-jobs/{job['job_id']}")

    assert apply_response.status_code == 202
    assert job["status"] == "success"
    assert legacy_status.status_code == 200
    assert legacy_status.json()["job_id"] == job["job_id"]
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls
