from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from threading import Event

import pytest
from tests.web_retag_test_helpers import (
    _create_retag_plan,
    _make_retag_fixture,
    _patch_digest_resolution,
    _patch_digest_resolution_results,
    _seed_known_image,
    _switch_choice,
    _wait_retag_preview_job,
    _write_compose,
)
from tests.web_test_helpers import (
    _assert_pending_grouping_did_not_mutate,
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _make_fake_stack,
)

from wudup import web_database
from wudup import web_retags as web_retags_module
from wudup.db import init_db, open_db, upsert_known_image
from wudup.digest_provenance import DigestTagProvenance
from wudup.digest_verifier import DigestResolveResult
from wudup.updater_models import (
    ComposeTagRewriteError,
    ResolvedTagMarkerConflictError,
)


def test_retag_targets_endpoint_returns_eligible_tagged_service(
    tmp_path: Path,
) -> None:
    fixture = _make_retag_fixture(tmp_path)
    client = fixture.client
    compose_dir = fixture.compose_dir
    before = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["count"] == 1
    assert body["warnings"] == []
    item = body["items"][0]
    assert item["target_id"]
    assert item["service_key"] == "stack/app"
    assert item["image"] == "repo/app@sha256:old"
    assert item["current_tag"] == ""
    assert item["tracking_tag"] == "latest"
    assert item["tracking_tag_source"] == "label"
    assert item["label_key"] == "wud.tag.include"
    assert item["label_value"] == "^latest$$"
    assert item["proposed_tag"] == "2.0"
    assert item["final_image"] == "repo/app:2.0"
    assert item["retag_available"] is True
    assert item["retag_reason"] == "eligible"
    assert item["choices"] == ["keep-current", "switch-to-concrete"]
    assert item["digest_provenance"]["resolved_tag"] == "2.0"
    assert (compose_dir / "docker-compose.yml").read_text(encoding="utf-8") == before
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fixture.fake_root))


@pytest.mark.parametrize(
    "provenance_field",
    [field.name for field in fields(DigestTagProvenance)],
)
def test_known_digest_state_reads_any_non_empty_provenance_column(
    tmp_path: Path,
    provenance_field: str,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    provenance_value = f"value-for-{provenance_field}"
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        upsert_known_image(
            conn,
            service_key="stack/app",
            image="repo/app:latest",
            digest_provenance=DigestTagProvenance(
                **{provenance_field: provenance_value},
            ),
        )

    state = web_database.known_digest_state_by_service(
        client.app.state.web_settings,
    )
    provenance = web_database.known_digest_provenance_by_service(
        client.app.state.web_settings,
    )

    assert state["stack/app"].image == "repo/app:latest"
    assert (
        getattr(state["stack/app"].digest_provenance, provenance_field)
        == provenance_value
    )
    assert getattr(provenance["stack/app"], provenance_field) == provenance_value
    for field in fields(DigestTagProvenance):
        if field.name == provenance_field:
            continue
        assert getattr(state["stack/app"].digest_provenance, field.name) == ""
        assert getattr(provenance["stack/app"], field.name) == ""


def test_retag_targets_endpoint_marks_ineligible_review_states(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("latest", "repo/latest:latest", "cid-latest"),
            ("concrete", "repo/concrete:1.0", "cid-concrete"),
            ("custom", "repo/custom:latest", "cid-custom"),
            ("escaped", "repo/escaped:latest", "cid-escaped"),
        ],
    )
    (compose_dir / "docker-compose.yml").write_text(
        "\n".join(
            [
                "services:",
                "  latest:",
                "    image: repo/latest:latest",
                "  concrete:",
                "    image: repo/concrete:1.0",
                "  custom:",
                "    image: repo/custom:latest",
                "    labels:",
                "      - wud.tag.include=^latest|stable$",
                "  escaped:",
                "    image: repo/escaped:latest",
                "    labels:",
                "      - wud.tag.include=^2\\.0$$",
                "",
            ]
        ),
        encoding="utf-8",
    )

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    items = {item["service"]: item for item in response.json()["items"]}
    assert items["latest"]["tracking_tag"] == "latest"
    assert items["latest"]["retag_available"] is False
    assert items["latest"]["retag_reason"] == "missing-provenance"
    assert items["latest"]["choices"] == ["keep-current"]
    assert items["concrete"]["tracking_tag"] == "1.0"
    assert items["concrete"]["retag_reason"] == "not-latest-tracking"
    assert items["custom"]["tracking_tag"] == ""
    assert items["custom"]["tracking_tag_source"] == "unsupported-label"
    assert items["custom"]["retag_reason"] == "unsupported-tracking-label"
    assert items["escaped"]["tracking_tag"] == "2.0"
    assert items["escaped"]["tracking_tag_source"] == "label"
    assert items["escaped"]["retag_reason"] == "not-latest-tracking"
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_targets_endpoint_marks_stale_known_provenance(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app@sha256:current", "cid-app")],
    )
    _write_compose(
        compose_dir,
        "app",
        "repo/app@sha256:current",
        label_value="^latest$$",
    )
    _seed_known_image(
        tmp_path,
        service_key="stack/app",
        image="repo/app@sha256:old",
        source_image="repo/app:latest",
        resolved_tag="2.0",
        watch_tag="latest",
        target_digest="sha256:old",
        final_image="repo/app@sha256:old",
    )

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["tracking_tag"] == "latest"
    assert item["retag_available"] is False
    assert item["retag_reason"] == "stale-provenance"
    assert item["proposed_tag"] == "2.0"
    assert item["final_image"] == "repo/app:2.0"
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_targets_endpoint_includes_service_without_pending_line(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["service_key"] == "stack/app"
    assert body["items"][0]["tracking_tag"] == "latest"
    assert body["items"][0]["retag_reason"] == "missing-provenance"
    assert not (tmp_path / "state" / "images.todo").exists()
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_targets_endpoint_reports_running_and_not_running_services(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    _make_fake_stack(
        tmp_path,
        fake_root,
        "live",
        [("app", "repo/live@sha256:live", "cid-live")],
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "archive",
        [("app", "repo/archive@sha256:archive", None)],
    )
    for stack in ("live", "archive"):
        image = f"repo/{stack}@sha256:{stack}"
        _seed_known_image(
            tmp_path,
            service_key=f"{stack}/app",
            image=image,
            source_image=f"repo/{stack}:latest",
            resolved_tag="2.0",
            watch_tag="latest",
            target_digest=f"sha256:{stack}",
            final_image=image,
        )

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    items = {item["stack"]: item for item in response.json()["items"]}
    assert items["live"]["runtime_state"] == "running"
    assert items["archive"]["runtime_state"] == "not-running"
    for item in items.values():
        assert item["retag_available"] is True
        assert item["choices"] == ["keep-current", "switch-to-concrete"]
    calls = _fake_docker_calls(fake_root)
    assert calls.count(
        'ps --format {{.Label "com.docker.compose.project.working_dir"}}\t'
        '{{.Label "com.docker.compose.project.config_files"}}\t'
        '{{.Label "com.docker.compose.project"}}\t'
        '{{.Label "com.docker.compose.service"}}\t'
        '{{.Label "com.docker.compose.oneoff"}}'
    ) == 1
    assert "compose -f docker-compose.yml ps" not in calls
    _assert_pending_grouping_did_not_mutate(calls)


def test_retag_targets_endpoint_reports_unknown_when_runtime_probe_fails(
    tmp_path: Path,
) -> None:
    fixture = _make_retag_fixture(tmp_path)
    (fixture.fake_root / "ps_fail").touch()

    response = fixture.client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["runtime_state"] == "unknown"
    assert item["retag_available"] is True
    assert item["choices"] == ["keep-current", "switch-to-concrete"]
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fixture.fake_root))


def test_retag_targets_ignores_compose_run_one_off_containers(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", None)],
    )
    (fake_root / "compose-runtime.tsv").write_text(
        f"{compose_dir}\t{compose_dir / 'docker-compose.yml'}\tstack\tapp\tTrue\n",
        encoding="utf-8",
    )

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    assert response.json()["items"][0]["runtime_state"] == "not-running"


def test_retag_targets_matches_relative_config_from_host_project_directory(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    host_base = tmp_path / "host-docker"
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "HOST_DOCKER_BASE": str(host_base),
            **fake_env,
        },
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app@sha256:old", "cid-app")],
    )
    host_stack = host_base / "stack"
    host_stack.mkdir(parents=True)
    (host_stack / "docker-compose.yml").write_text(
        (compose_dir / "docker-compose.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (fake_root / "compose-runtime.tsv").write_text(
        f"{host_stack}\tdocker-compose.yml\tstack\tapp\tFalse\n",
        encoding="utf-8",
    )
    _seed_known_image(
        tmp_path,
        service_key="stack/app",
        image="repo/app@sha256:old",
        source_image="repo/app:latest",
        resolved_tag="2.0",
        watch_tag="latest",
        target_digest="sha256:old",
        final_image="repo/app@sha256:old",
    )

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    assert response.json()["items"][0]["runtime_state"] == "running"


def test_retag_targets_does_not_match_a_multi_file_compose_project(
    tmp_path: Path,
) -> None:
    fixture = _make_retag_fixture(tmp_path)
    compose_dir = tmp_path / "docker" / "stack"
    (fixture.fake_root / "compose-runtime.tsv").write_text(
        f"{compose_dir}\t{compose_dir / 'docker-compose.yml'},"
        f"{compose_dir / 'compose.override.yml'}\tstack\tapp\tFalse\n",
        encoding="utf-8",
    )

    response = fixture.client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    assert response.json()["items"][0]["runtime_state"] == "not-running"


def test_retag_targets_does_not_match_a_custom_compose_project(
    tmp_path: Path,
) -> None:
    fixture = _make_retag_fixture(tmp_path)
    compose_dir = tmp_path / "docker" / "stack"
    (fixture.fake_root / "compose-runtime.tsv").write_text(
        f"{compose_dir}\t{compose_dir / 'docker-compose.yml'}\tcustom\tapp\tFalse\n",
        encoding="utf-8",
    )

    response = fixture.client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    assert response.json()["items"][0]["runtime_state"] == "not-running"


def test_retag_targets_matches_the_configured_compose_project(
    tmp_path: Path,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={"COMPOSE_PROJECT_NAME": "custom"},
    )
    compose_dir = tmp_path / "docker" / "stack"
    (fixture.fake_root / "compose-runtime.tsv").write_text(
        f"{compose_dir}\t{compose_dir / 'docker-compose.yml'}\tcustom\tapp\tFalse\n",
        encoding="utf-8",
    )

    response = fixture.client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    assert response.json()["items"][0]["runtime_state"] == "running"


def test_retag_targets_matches_the_compose_file_project_name(
    tmp_path: Path,
) -> None:
    fixture = _make_retag_fixture(tmp_path)
    compose_file = fixture.compose_dir / "docker-compose.yml"
    compose_file.write_text(
        f"name: custom\n{compose_file.read_text(encoding='utf-8')}",
        encoding="utf-8",
    )
    (fixture.fake_root / "compose-runtime.tsv").write_text(
        f"{fixture.compose_dir}\t{compose_file}\tcustom\tapp\tFalse\n",
        encoding="utf-8",
    )

    response = fixture.client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    assert response.json()["items"][0]["runtime_state"] == "running"


def test_retag_preview_rejects_second_active_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    headers = _csrf_headers(client)
    started = Event()
    release = Event()

    def slow_build(
        _settings: object,
        _payload: object,
    ) -> None:
        started.set()
        release.wait(timeout=2)
        raise RuntimeError("test preview released")

    monkeypatch.setattr(
        web_retags_module,
        "_build_current_retag_plan",
        slow_build,
    )

    first = client.post(
        "/api/v1/retag-plans/preview",
        json={"choices": [_switch_choice()]},
        headers=headers,
    )
    assert first.status_code == 202
    assert started.wait(timeout=1)

    second = client.post(
        "/api/v1/retag-plans/preview",
        json={"choices": [_switch_choice()]},
        headers=headers,
    )

    assert second.status_code == 409
    assert second.json()["detail"] == "retag preview is already running"
    release.set()
    job = _wait_retag_preview_job(client, first.json()["preview_job_id"])
    assert job["status"] == "failure"


def test_retag_targets_do_not_treat_persisted_source_image_as_current(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    _seed_known_image(
        tmp_path,
        service_key="stack/app",
        image="repo/app@sha256:old",
        source_image="repo/app:latest",
        resolved_tag="1.0.0",
        watch_tag="latest",
        target_digest="sha256:old",
        final_image="repo/app@sha256:old",
    )

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["image"] == "repo/app:latest"
    assert item["retag_available"] is False
    assert item["retag_reason"] == "stale-provenance"
    assert item["choices"] == ["keep-current"]


def test_retag_targets_endpoint_returns_unavailable_when_discovery_fails(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get("/api/v1/retag-targets")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["count"] == 0
    assert body["items"] == []
    assert body["warnings"]
    assert "[REDACTED_PATH]" in body["warnings"][0]
    assert str(tmp_path) not in body["warnings"][0]


def test_retag_plan_keep_current_is_empty_noop(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    client = fixture.client
    headers = _csrf_headers(client)

    response = client.post(
        "/api/v1/retag-plans",
        json={"choices": [{"service_key": "stack/app", "choice": "keep-current"}]},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["can_apply"] is False
    assert body["selected_count"] == 0
    assert body["keep_current_count"] == 1
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fixture.fake_root))


def test_retag_plan_rejects_keep_current_target_tag(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(tmp_path)

    response = fixture.client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                {
                    "service_key": "stack/app",
                    "choice": "keep-current",
                    "target_tag": "2.0",
                }
            ]
        },
        headers=_csrf_headers(fixture.client),
    )

    assert response.status_code == 422
    assert "target_tag is only allowed" in str(response.json()["detail"])
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fixture.fake_root))


def test_retag_plan_rejects_keep_current_start_approval(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(tmp_path)

    response = fixture.client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                {
                    "service_key": "stack/app",
                    "choice": "keep-current",
                    "allow_start": True,
                }
            ]
        },
        headers=_csrf_headers(fixture.client),
    )

    assert response.status_code == 422
    assert "allow_start is only allowed" in str(response.json()["detail"])
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fixture.fake_root))


@pytest.mark.parametrize("runtime_state", ["not-running", "unknown"])
def test_retag_plan_requires_start_approval_for_inactive_runtime(
    tmp_path: Path,
    runtime_state: str,
) -> None:
    fixture = _make_retag_fixture(tmp_path)
    initial = fixture.client.get("/api/v1/retag-targets")

    assert initial.status_code == 200
    assert initial.json()["items"][0]["runtime_state"] == "running"

    if runtime_state == "not-running":
        (fixture.fake_root / "compose-runtime.tsv").write_text("", encoding="utf-8")
    else:
        (fixture.fake_root / "ps_fail").touch()
    headers = _csrf_headers(fixture.client)

    blocked = fixture.client.post(
        "/api/v1/retag-plans",
        json={"choices": [_switch_choice()]},
        headers=headers,
    )
    approved = fixture.client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                {
                    **_switch_choice(),
                    "allow_start": True,
                }
            ]
        },
        headers=headers,
    )

    assert blocked.status_code == 200
    blocked_plan = blocked.json()
    assert blocked_plan["status"] == "blocked"
    assert blocked_plan["can_apply"] is False
    assert blocked_plan["issues"][0]["code"] == "retag-start-not-approved"
    assert blocked_plan["issues"][0]["details"] == {
        "runtime_state": runtime_state
    }
    assert approved.status_code == 200
    approved_plan = approved.json()
    assert approved_plan["status"] == "ready"
    assert approved_plan["can_apply"] is True
    assert approved_plan["plan_id"] != blocked_plan["plan_id"]


@pytest.mark.parametrize(
    ("target_tag", "expected_regex"),
    [("3.0", r"^\d+(?:\.\d+)+$$"), ("2.7-alpine", r"^2(?:\.\d+)+-alpine$$")],
)
def test_retag_plan_manual_target_allows_non_latest_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_tag: str,
    expected_regex: str,
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
        [("app", "repo/app:1.0", "cid-app")],
    )
    digest = "sha256:" + "3" * 64
    _patch_digest_resolution(
        monkeypatch,
        expected_image=f"repo/app:{target_tag}",
        digest=digest,
    )

    response = client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                {
                    "service_key": "stack/app",
                    "choice": "switch-to-concrete",
                    "target_tag": target_tag,
                }
            ]
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["can_apply"] is True
    update = body["stacks"][0]["tag_updates"][0]
    assert update["service_key"] == "stack/app"
    assert update["source_image"] == "repo/app:1.0"
    assert update["target_tag"] == target_tag
    assert update["final_image"] == f"repo/app:{target_tag}"
    assert update["label_value"] == expected_regex
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_plan_manual_target_overrides_automatch_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(tmp_path)
    headers = _csrf_headers(fixture.client)
    auto_plan = _create_retag_plan(fixture.client, headers)
    digest = "sha256:" + "4" * 64
    manual_choice = {
        "service_key": "stack/app",
        "choice": "switch-to-concrete",
        "target_tag": "3.0",
    }
    _patch_digest_resolution(
        monkeypatch,
        expected_image="repo/app:3.0",
        digest=digest,
    )

    manual_plan = _create_retag_plan(
        fixture.client,
        headers,
        choices=[manual_choice],
    )

    assert manual_plan["plan_id"] != auto_plan["plan_id"]
    update = manual_plan["stacks"][0]["tag_updates"][0]
    assert update["target_tag"] == "3.0"
    assert update["final_image"] == "repo/app:3.0"


@pytest.mark.parametrize(
    ("target_tag", "issue_code"),
    [
        ("", "retag-manual-empty-tag"),
        ("latest", "retag-manual-latest-tag"),
        ("-bad", "retag-manual-invalid-tag"),
    ],
)
def test_retag_plan_blocks_invalid_manual_targets(
    tmp_path: Path,
    target_tag: str,
    issue_code: str,
) -> None:
    fixture = _make_retag_fixture(tmp_path)
    response = fixture.client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                {
                    "service_key": "stack/app",
                    "choice": "switch-to-concrete",
                    "target_tag": target_tag,
                }
            ]
        },
        headers=_csrf_headers(fixture.client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["can_apply"] is False
    assert body["issues"][0]["code"] == issue_code


def test_retag_plan_blocks_manual_digest_resolution_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(tmp_path)
    _patch_digest_resolution_results(
        monkeypatch,
        {
            "repo/app:9.9": DigestResolveResult(
                ok=False,
                status="unavailable",
                reason="manifest-unavailable",
            )
        },
    )

    response = fixture.client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                {
                    "service_key": "stack/app",
                    "choice": "switch-to-concrete",
                    "target_tag": "9.9",
                }
            ]
        },
        headers=_csrf_headers(fixture.client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["issues"][0]["code"] == "retag-manual-digest-unavailable"
    assert "repo/app:9.9" in body["issues"][0]["message"]


def test_retag_plan_sanitizes_compose_preview_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(tmp_path)
    headers = _csrf_headers(fixture.client)

    def fail_preview(*_args: object, **_kwargs: object) -> None:
        raise ComposeTagRewriteError(
            "Service app has conflicting resolved-tag markers: latest, v1.25.0. "
            f"Invalid compose at {tmp_path}/secret/docker-compose.yml"
        )

    monkeypatch.setattr(
        web_retags_module,
        "render_compose_retag_updates",
        fail_preview,
    )

    response = fixture.client.post(
        "/api/v1/retag-plans",
        json={"choices": [_switch_choice()]},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    message = body["issues"][0]["message"]
    assert "Could not safely preview retag for stack/app" in message
    assert "[REDACTED_PATH]" in message
    assert str(tmp_path) not in message
    assert body["issues"][0]["hint"] == ""


@pytest.mark.parametrize(
    ("conflict_service", "expected_hint"),
    [
        (
            "app",
            (
                "Remove stale wudup.resolved-tag comments from this Compose "
                "service, then preview again."
            ),
        ),
        ("other", ""),
    ],
)
def test_retag_plan_scopes_marker_conflict_hint_to_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflict_service: str,
    expected_hint: str,
) -> None:
    fixture = _make_retag_fixture(tmp_path)

    def fail_preview(*_args: object, **_kwargs: object) -> None:
        raise ResolvedTagMarkerConflictError(
            service=conflict_service,
            tags=("latest", "v1.25.0"),
        )

    monkeypatch.setattr(
        web_retags_module,
        "render_compose_retag_updates",
        fail_preview,
    )

    response = fixture.client.post(
        "/api/v1/retag-plans",
        json={"choices": [_switch_choice()]},
        headers=_csrf_headers(fixture.client),
    )

    assert response.status_code == 200
    assert response.json()["issues"][0]["hint"] == expected_hint


def test_patch_digest_resolution_results_asserts_on_unknown_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_digest_resolution_results(monkeypatch, {})

    with pytest.raises(
        AssertionError,
        match=r"Unexpected digest resolution for 'unknown:tag'",
    ):
        web_retags_module.DigestVerifier().resolve_tag_digest("unknown:tag")
