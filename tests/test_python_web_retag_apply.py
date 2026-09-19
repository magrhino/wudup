from __future__ import annotations

import json
from pathlib import Path
from threading import Event

import pytest
from tests.web_retag_test_helpers import (
    _apply_retag_plan,
    _audit_settings,
    _create_retag_plan,
    _make_retag_fixture,
    _patch_digest_resolution_map,
    _seed_known_image,
    _set_retag_digest_pins,
    _switch_choice,
    _wait_run_status,
    _write_compose,
)
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _make_fake_stack,
    _wait_apply_job,
)

from wudup import web_retags as web_retags_module
from wudup.compose import ComposeStack, ServiceImage
from wudup.db import open_db
from wudup.digest_provenance import DigestTagProvenance
from wudup.locks import DirectoryLock
from wudup.updater_digest_pin import digest_pin_update_from_values
from wudup.web_models import RetagPlanResponse, WebApplyJob
from wudup.web_retag_plans import RetagPlanBuild, RetagPlanUpdate


def test_retag_plan_and_apply_rewrites_pulls_recreates_and_audits(
    tmp_path: Path,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
    )
    client = fixture.client
    compose_dir = fixture.compose_dir
    headers = _csrf_headers(client)

    plan = _create_retag_plan(client, headers)

    assert plan["status"] == "ready"
    assert plan["can_apply"] is True
    assert plan["selected_count"] == 1
    assert plan["external_recreate_required"] is False
    assert plan["stacks"][0]["services"] == ["app"]
    assert plan["stacks"][0]["tag_updates"][0]["target_tag"] == "2.0"
    assert plan["stacks"][0]["digest_pin_updates"][0]["final_image"] == "repo/app:2.0"

    apply_response = _apply_retag_plan(client, headers, plan)

    assert apply_response.status_code == 202
    job = _wait_apply_job(client, apply_response.json()["job_id"])
    assert job["status"] == "success"
    content = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert "# wudup.resolved-tag=" not in content
    assert "image: repo/app:2.0" in content
    assert "wud.tag.include=^\\d+\\.\\d+$$" in content
    calls = _fake_docker_calls(fixture.fake_root)
    assert "compose -f docker-compose.yml pull app" in calls
    assert "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --force-recreate --no-deps app" in calls

    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        run = conn.execute(
            "SELECT mode, status FROM update_runs WHERE id = ?",
            (job["run_id"],),
        ).fetchone()
        event = conn.execute(
            "SELECT stack_name, service_name, status, target_image, digest_provenance_source "
            "FROM update_events WHERE run_id = ?",
            (job["run_id"],),
        ).fetchone()
        known = conn.execute(
            "SELECT image, digest_provenance_source, digest_watch_tag "
            "FROM known_images WHERE service_key = 'stack/app'",
        ).fetchone()
    assert run["mode"] == "web-retag"
    assert run["status"] == "success"
    assert event["stack_name"] == "stack"
    assert event["service_name"] == "app"
    assert event["status"] == "success"
    assert event["target_image"] == "repo/app:2.0"
    assert event["digest_provenance_source"] == ""
    assert known["image"] == "repo/app:2.0"
    assert known["digest_provenance_source"] == ""
    assert known["digest_watch_tag"] == ""


def test_retag_selected_tag_tracks_numeric_tag_shape(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
        resolved_tag="v1.2.3",
    )
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)

    update = plan["stacks"][0]["tag_updates"][0]
    assert update["label_value"] == r"^v\d+\.\d+\.\d+$$"
    assert update["label_rewrites"][0]["proposed_label_regex"] == (
        r"^v\d+\.\d+\.\d+$"
    )

    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "success"
    content = (fixture.compose_dir / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "image: repo/app:v1.2.3" in content
    assert r"wud.tag.include=^v\d+\.\d+\.\d+$$" in content


def test_retag_digest_pin_setting_preserves_digest_rewrites(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
        retag_digest_pins=True,
    )
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)

    assert plan["stacks"][0]["tag_updates"] == []
    assert plan["stacks"][0]["digest_pin_updates"][0]["resolved_tag"] == "2.0"

    apply_response = _apply_retag_plan(fixture.client, headers, plan)
    assert apply_response.status_code == 202
    job = _wait_apply_job(fixture.client, apply_response.json()["job_id"])
    assert job["status"] == "success"
    content = (fixture.compose_dir / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "# wudup.resolved-tag=2.0" in content
    assert "image: repo/app@sha256:old" in content
    assert "wud.tag.include=^2\\.0$$" in content


def test_retag_apply_rejects_stale_manual_target_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    headers = _csrf_headers(fixture.client)
    _patch_digest_resolution_map(
        monkeypatch,
        {
            "repo/app:3.0": "sha256:" + "3" * 64,
            "repo/app:4.0": "sha256:" + "4" * 64,
        },
    )
    plan_choice = {
        "service_key": "stack/app",
        "choice": "switch-to-concrete",
        "target_tag": "3.0",
    }
    plan = _create_retag_plan(fixture.client, headers, choices=[plan_choice])

    response = fixture.client.post(
        "/api/v1/retag-plans/apply",
        json={
            "plan_id": plan["plan_id"],
            "choices": [
                {
                    "service_key": "stack/app",
                    "choice": "switch-to-concrete",
                    "target_tag": "4.0",
                }
            ],
            "confirmation": "apply-retags",
        },
        headers=headers,
    )

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert job["error"] == "retag apply failed: retag plan is stale"
    assert job["progress"][-1]["phase"] == "preflight"


def test_retag_apply_rejects_stale_plan(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    client = fixture.client
    compose_dir = fixture.compose_dir
    headers = _csrf_headers(client)
    plan = _create_retag_plan(client, headers)
    _write_compose(
        compose_dir,
        "app",
        "repo/app@sha256:old",
        label_value="^2\\.0$$",
    )

    response = client.post(
        "/api/v1/retag-plans/apply",
        json={
            "plan_id": plan["plan_id"],
            "choices": [_switch_choice()],
            "confirmation": "apply-retags",
        },
        headers=headers,
    )

    assert response.status_code == 202
    job = _wait_apply_job(client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert job["error"] == "retag apply failed: retag plan is stale"
    assert job["progress"][-1]["phase"] == "preflight"


def test_retag_apply_returns_job_before_slow_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={"WUD_WEB_MUTATIONS_ENABLED": "true"},
    )
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)
    original_build = web_retags_module._build_current_retag_plan
    started = Event()
    release = Event()

    def slow_build(*args: object, **kwargs: object) -> RetagPlanBuild:
        started.set()
        assert release.wait(timeout=2)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(
        web_retags_module,
        "_build_current_retag_plan",
        slow_build,
    )
    response = fixture.client.post(
        "/api/v1/retag-plans/apply",
        json={
            "plan_id": plan["plan_id"],
            "choices": [_switch_choice()],
            "confirmation": "apply-retags",
        },
        headers=headers,
    )

    try:
        assert response.status_code == 202
        assert started.wait(timeout=1)
        visible = fixture.client.get(
            f"/api/v1/jobs/{response.json()['job_id']}"
        ).json()
        assert visible["status"] == "running"
        progress = visible["progress"][-1]
        assert progress["phase"] == "preflight"
        assert progress["status"] == "running"
        assert progress["message"] == "Revalidating the selected retag plan."
    finally:
        release.set()

    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "success"


def test_retag_apply_reports_existing_wud_lock_as_failed_preflight(
    tmp_path: Path,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_LOCK_TIMEOUT": "0",
        },
    )
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)
    compose_file = fixture.compose_dir / "docker-compose.yml"
    compose_before = compose_file.read_text(encoding="utf-8")
    calls_before = _fake_docker_calls(fixture.fake_root)
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        audit_count_before = conn.execute(
            "SELECT COUNT(*) FROM update_runs"
        ).fetchone()[0]

    external_lock = DirectoryLock(
        tmp_path / "state" / "images.todo",
        timeout_seconds=0,
    )
    external_lock.acquire()
    try:
        response = _apply_retag_plan(fixture.client, headers, plan)
        assert response.status_code == 202
        job = _wait_apply_job(fixture.client, response.json()["job_id"])
    finally:
        external_lock.close()

    assert job["status"] == "failure"
    assert "WUD file is locked" in job["error"]
    assert job["progress"][-1]["phase"] == "preflight"
    assert job["progress"][-1]["status"] == "failure"
    assert compose_file.read_text(encoding="utf-8") == compose_before
    assert _fake_docker_calls(fixture.fake_root) == calls_before
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        audit_count_after = conn.execute(
            "SELECT COUNT(*) FROM update_runs"
        ).fetchone()[0]
    assert audit_count_after == audit_count_before


@pytest.mark.parametrize("runtime_state", ["not-running", "unknown"])
def test_retag_apply_rejects_runtime_drift_without_start_approval(
    tmp_path: Path,
    runtime_state: str,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)
    if runtime_state == "not-running":
        (fixture.fake_root / "compose-runtime.tsv").write_text("", encoding="utf-8")
    else:
        (fixture.fake_root / "ps_fail").touch()

    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert job["error"] == "retag apply failed: retag plan is stale"
    calls = _fake_docker_calls(fixture.fake_root)
    assert "compose -f docker-compose.yml pull app" not in calls
    assert "compose -f docker-compose.yml up" not in calls


def test_retag_apply_allows_explicit_inactive_start_approval(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
    )
    (fixture.fake_root / "compose-runtime.tsv").write_text("", encoding="utf-8")
    headers = _csrf_headers(fixture.client)
    choice = {**_switch_choice(), "allow_start": True}
    plan = _create_retag_plan(fixture.client, headers, choices=[choice])

    response = _apply_retag_plan(
        fixture.client,
        headers,
        plan,
        choices=[choice],
    )

    assert plan["status"] == "ready"
    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "success"
    calls = _fake_docker_calls(fixture.fake_root)
    assert "compose -f docker-compose.yml pull app" in calls
    assert (
        "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build "
        "--force-recreate --no-deps app"
    ) in calls


def test_retag_apply_worker_rechecks_runtime_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
    )
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)
    before = (fixture.compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    original_apply = web_retags_module._apply_retag_updates

    def apply_after_runtime_stops(
        *args: object,
        **kwargs: object,
    ) -> tuple[RetagPlanUpdate, ...]:
        (fixture.fake_root / "compose-runtime.tsv").write_text("", encoding="utf-8")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(
        web_retags_module,
        "_apply_retag_updates",
        apply_after_runtime_stops,
    )
    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "no longer running" in job["error"]
    assert (fixture.compose_dir / "docker-compose.yml").read_text(
        encoding="utf-8"
    ) == before
    calls = _fake_docker_calls(fixture.fake_root)
    assert "compose -f docker-compose.yml pull app" not in calls


def test_retag_apply_worker_rechecks_effective_project_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
    )
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)
    compose_file = fixture.compose_dir / "docker-compose.yml"
    changed_content = (
        f"name: replacement\n{compose_file.read_text(encoding='utf-8')}"
    )
    original_apply = web_retags_module._apply_retag_updates

    def apply_after_project_changes(
        *args: object,
        **kwargs: object,
    ) -> tuple[RetagPlanUpdate, ...]:
        compose_file.write_text(changed_content, encoding="utf-8")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(
        web_retags_module,
        "_apply_retag_updates",
        apply_after_project_changes,
    )
    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "Compose project changed" in job["error"]
    assert compose_file.read_text(encoding="utf-8") == changed_content
    calls = _fake_docker_calls(fixture.fake_root)
    assert "compose -f docker-compose.yml pull app" not in calls


def test_retag_apply_start_approval_does_not_bypass_project_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
    )
    (fixture.fake_root / "compose-runtime.tsv").write_text("", encoding="utf-8")
    choice = {**_switch_choice(), "allow_start": True}
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers, choices=[choice])
    compose_file = fixture.compose_dir / "docker-compose.yml"
    changed_content = (
        f"name: replacement\n{compose_file.read_text(encoding='utf-8')}"
    )
    original_apply = web_retags_module._apply_retag_updates

    def apply_after_project_changes(
        *args: object,
        **kwargs: object,
    ) -> tuple[RetagPlanUpdate, ...]:
        compose_file.write_text(changed_content, encoding="utf-8")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(
        web_retags_module,
        "_apply_retag_updates",
        apply_after_project_changes,
    )
    response = _apply_retag_plan(
        fixture.client,
        headers,
        plan,
        choices=[choice],
    )

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "Compose project changed" in job["error"]
    assert compose_file.read_text(encoding="utf-8") == changed_content
    calls = _fake_docker_calls(fixture.fake_root)
    assert "compose -f docker-compose.yml pull app" not in calls


def test_retag_apply_cleans_up_job_when_executor_submit_fails(
    tmp_path: Path,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    client = fixture.client
    headers = _csrf_headers(client)
    plan = _create_retag_plan(client, headers)

    class FailingExecutor:
        def submit(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("queue failed")

    client.app.state.web_apply_executor = FailingExecutor()

    with pytest.raises(RuntimeError, match="queue failed"):
        client.post(
            "/api/v1/retag-plans/apply",
            json={
                "plan_id": plan["plan_id"],
                "choices": [_switch_choice()],
                "confirmation": "apply-retags",
            },
            headers=headers,
        )

    assert client.app.state.web_apply_jobs == {}


def test_retag_apply_enforces_csrf_read_only_and_active_job(tmp_path: Path) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    read_only = _client(
        tmp_path,
        {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env},
    )
    mutating = _client(
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
        [("app", "repo/app:latest", "cid-app")],
    )
    payload = {
        "plan_id": "plan",
        "choices": [{"service_key": "stack/app", "choice": "keep-current"}],
        "confirmation": "apply-retags",
    }
    read_only_response = read_only.post(
        "/api/v1/retag-plans/apply",
        json=payload,
        headers=_csrf_headers(read_only),
    )
    missing_csrf = mutating.post("/api/v1/retag-plans/apply", json=payload)
    mutating.app.state.web_apply_jobs["active"] = WebApplyJob(
        id="active",
        status="running",
        selected_line_numbers=(),
    )
    active_job = mutating.post(
        "/api/v1/retag-plans/apply",
        json=payload,
        headers=_csrf_headers(mutating),
    )

    assert read_only_response.status_code == 403
    assert read_only_response.json()["detail"] == "mutations are disabled"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "origin header is required"
    assert active_job.status_code == 409
    assert active_job.json()["detail"] == "an apply job is already running"


def test_retag_apply_restores_compose_when_pull_fails(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
    )
    client = fixture.client
    compose_dir = fixture.compose_dir
    (fixture.fake_root / "stacks" / "stack" / "pull_fail").write_text(
        "pull failed\n",
        encoding="utf-8",
    )
    before = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    headers = _csrf_headers(client)
    plan = _create_retag_plan(client, headers)

    response = _apply_retag_plan(client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert (compose_dir / "docker-compose.yml").read_text(encoding="utf-8") == before
    calls = _fake_docker_calls(fixture.fake_root)
    assert "compose -f docker-compose.yml pull app" in calls
    assert "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --force-recreate --no-deps app" in calls
    run = _wait_run_status(
        tmp_path / "state" / "wud.sqlite",
        job["run_id"],
        "failure",
    )
    assert run["status"] == "failure"


def test_retag_apply_marks_job_failed_before_failure_audit_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
    )
    (fixture.fake_root / "stacks" / "stack" / "pull_fail").write_text(
        "pull failed\n",
        encoding="utf-8",
    )
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)
    original_finish = web_retags_module._finish_retag_audit_run

    def fail_failure_finish(*args: object, **kwargs: object) -> None:
        if kwargs.get("status") == "failure":
            raise RuntimeError("audit finalization failed")
        original_finish(*args, **kwargs)

    monkeypatch.setattr(
        web_retags_module,
        "_finish_retag_audit_run",
        fail_failure_finish,
    )

    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert job["error"]


def test_retag_apply_records_partial_stack_success_when_later_stack_fails(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
            **fake_env,
        },
    )
    alpha_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "alpha",
        [("app", "repo/alpha@sha256:old", "cid-alpha")],
    )
    bravo_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "bravo",
        [("app", "repo/bravo@sha256:old", "cid-bravo")],
    )
    _write_compose(
        alpha_dir,
        "app",
        "repo/alpha@sha256:old",
        label_value="^latest$$",
    )
    _write_compose(
        bravo_dir,
        "app",
        "repo/bravo@sha256:old",
        label_value="^latest$$",
    )
    _seed_known_image(
        tmp_path,
        service_key="alpha/app",
        image="repo/alpha@sha256:old",
        source_image="repo/alpha:latest",
        resolved_tag="2.0",
        watch_tag="latest",
        target_digest="sha256:old",
        final_image="repo/alpha@sha256:old",
    )
    _seed_known_image(
        tmp_path,
        service_key="bravo/app",
        image="repo/bravo@sha256:old",
        source_image="repo/bravo:latest",
        resolved_tag="3.0",
        watch_tag="latest",
        target_digest="sha256:old",
        final_image="repo/bravo@sha256:old",
    )
    _set_retag_digest_pins(tmp_path)
    (fake_root / "stacks" / "bravo" / "pull_fail").write_text(
        "pull failed\n",
        encoding="utf-8",
    )
    bravo_before = (bravo_dir / "docker-compose.yml").read_text(encoding="utf-8")
    headers = _csrf_headers(client)
    choices = [
        {"service_key": "alpha/app", "choice": "switch-to-concrete"},
        {"service_key": "bravo/app", "choice": "switch-to-concrete"},
    ]
    plan = client.post(
        "/api/v1/retag-plans",
        json={"choices": choices},
        headers=headers,
    ).json()

    response = client.post(
        "/api/v1/retag-plans/apply",
        json={
            "plan_id": plan["plan_id"],
            "choices": choices,
            "confirmation": "apply-retags",
        },
        headers=headers,
    )

    assert response.status_code == 202
    job = _wait_apply_job(client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "wud.tag.include=^2\\.0$$" in (
        alpha_dir / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    assert (bravo_dir / "docker-compose.yml").read_text(
        encoding="utf-8"
    ) == bravo_before
    db_path = tmp_path / "state" / "wud.sqlite"
    run = _wait_run_status(db_path, job["run_id"], "failure")
    with open_db(db_path) as conn:
        events = conn.execute(
            """
            SELECT stack_name, service_name, status, digest_provenance_confidence
            FROM update_events
            WHERE run_id = ?
            ORDER BY stack_name, service_name
            """,
            (job["run_id"],),
        ).fetchall()
        known = conn.execute(
            """
            SELECT service_key, digest_provenance_source, digest_watch_tag
            FROM known_images
            WHERE service_key IN ('alpha/app', 'bravo/app')
            ORDER BY service_key
            """
        ).fetchall()
    assert run["status"] == "failure"
    assert [
        (
            row["stack_name"],
            row["service_name"],
            row["status"],
            row["digest_provenance_confidence"],
        )
        for row in events
    ] == [
        ("alpha", "app", "success", "verified"),
        ("bravo", "app", "failure", "planned"),
    ]
    assert [
        (
            row["service_key"],
            row["digest_provenance_source"],
            row["digest_watch_tag"],
        )
        for row in known
    ] == [
        ("alpha/app", "retag", "2.0"),
        ("bravo/app", "apply", "latest"),
    ]


def test_retag_failure_audit_keeps_ambiguous_known_image_skip_reason_off_failures(
    tmp_path: Path,
) -> None:
    settings = _audit_settings(tmp_path)
    stack = ComposeStack(
        index=1,
        directory=tmp_path / "docker" / "stack",
        file="docker-compose.yml",
        name="stack",
        images=("repo/app@sha256:old",),
        service_images=(
            ServiceImage("app", "repo/app@sha256:old"),
        ),
    )
    provenance = DigestTagProvenance(
        source_image="repo/app:latest",
        resolved_tag="2.0",
        watch_tag="latest",
        target_digest="sha256:old",
        final_image="repo/app@sha256:old",
        provenance_source="apply",
        provenance_confidence="verified",
    )
    update = RetagPlanUpdate(
        target_id="target-one",
        service_key="stack/app",
        stack=stack,
        update=digest_pin_update_from_values(
            old_image="repo/app@sha256:old",
            resolved_tag="2.0",
            planned_digest="sha256:old",
            services=("app",),
        ),
        provenance=provenance,
        known_image_service_key_ambiguous=True,
    )
    build = RetagPlanBuild(
        response=RetagPlanResponse(
            plan_id="plan-one",
            status="ready",
            can_apply=True,
            selected_count=1,
        ),
        updates=(update,),
    )
    run_id = web_retags_module._insert_retag_audit_run(
        settings,
        build,
        status="running",
    )

    web_retags_module._finish_retag_audit_run(
        settings,
        run_id,
        build,
        status="failure",
        error="pull failed",
    )

    with open_db(settings.config.db_path) as conn:
        event = conn.execute(
            """
            SELECT status, metadata_json
            FROM update_events
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    assert event["status"] == "failure"
    metadata = json.loads(event["metadata_json"])
    assert metadata["known_image_recorded"] is False
    assert "known_image_skip_reason" not in metadata


def test_retag_apply_redacts_rollback_failure_paths(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
    )
    client = fixture.client
    (fixture.fake_root / "stacks" / "stack" / "up_fail").write_text(
        "up failed\n",
        encoding="utf-8",
    )
    headers = _csrf_headers(client)
    plan = _create_retag_plan(client, headers)

    response = _apply_retag_plan(client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(client, response.json()["job_id"])
    assert job["status"] == "failure"
    visible_error = " ".join(
        [
            job["error"],
            *[item["message"] for item in job["progress"]],
        ]
    )
    assert "backup retained at" in visible_error
    assert "[REDACTED_PATH]" in visible_error
    assert str(tmp_path) not in visible_error


def test_retag_apply_unpauses_before_rollback_when_pause_mode_up_fails(
    tmp_path: Path,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "pause",
            "WUD_MAX_WAIT": "0",
        },
    )
    client = fixture.client
    compose_dir = fixture.compose_dir
    (fixture.fake_root / "stacks" / "stack" / "up_fail").write_text(
        "up failed\n",
        encoding="utf-8",
    )
    before = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    headers = _csrf_headers(client)
    plan = _create_retag_plan(client, headers)

    response = _apply_retag_plan(client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert (compose_dir / "docker-compose.yml").read_text(encoding="utf-8") == before
    calls = _fake_docker_calls(fixture.fake_root).splitlines()
    up_call = "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --force-recreate --no-deps app"

    def call_index(needle: str, *, start: int = 0) -> int:
        return next(
            index for index, call in enumerate(calls[start:], start) if needle in call
        )

    pause = call_index("compose -f docker-compose.yml pause app")
    first_up = call_index(up_call)
    unpause = call_index("compose -f docker-compose.yml unpause app")
    rollback_up = call_index(up_call, start=first_up + 1)
    assert pause < first_up < unpause < rollback_up
