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
    _make_fake_stack,
    _self_update_payload,
    _sse_event_names,
    _sse_job_events,
    _sse_log_events,
    _sse_progress_events,
    _wait_apply_job,
    _write_fake_image_after_pull,
)

from wudup import web_jobs, web_wud_api
from wudup import web_self_update as self_update_module
from wudup.command import CommandError, CommandResult
from wudup.compose import ComposeCli
from wudup.config import UpdaterConfig
from wudup.docker_cli import DockerCli
from wudup.updater_models import CompletedUpdateSelection
from wudup.web_models import WebApplyJob, WebSettings


def _settings_for_lock_timeout(
    tmp_path: Path,
    command_env: dict[str, str] | None,
) -> WebSettings:
    root = tmp_path / "state"
    return WebSettings(
        config=UpdaterConfig(
            docker_base=tmp_path / "docker",
            wud_out_file=root / "images.todo",
            log_dir=root / "logs",
            db_path=root / "wud.sqlite",
            update_mode="stop",
            max_wait=180,
            lock_timeout=30,
            timezone_name="UTC",
            compose_ignore_paths=(),
            digest_pin_updates=False,
            out_uid=None,
            out_gid=None,
        ),
        auth_token="",
        command_env=command_env,
    )


def _shared_update_case(
    tmp_path: Path,
    *,
    wud_line: str = "repo/shared:latest",
    compose_image: str = "repo/shared:latest",
    pull_image: str = "repo/shared:latest",
    update_mode: str = "stop",
    restart_container: str = "",
) -> SimpleNamespace:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    environ = {
        "WUD_WEB_DEV_NO_AUTH": "true",
        "WUD_WEB_MUTATIONS_ENABLED": "true",
        "WUD_UPDATE_MODE": update_mode,
        "WUD_WEB_RESTART_CONTAINER": restart_container,
        **fake_env,
    }
    client = _client(tmp_path, environ)
    wud_file = tmp_path / "state" / "images.todo"
    original = f"{wud_line}\n"
    wud_file.write_text(original, encoding="utf-8")
    stack_dirs = {
        stack_name: _make_fake_stack(
            tmp_path,
            fake_root,
            stack_name,
            [("app", compose_image, f"cid-{stack_name}")],
        )
        for stack_name in ("active", "backup")
    }
    _write_fake_image_after_pull(
        fake_root,
        pull_image,
        "sha256:new-id",
        "sha256:new",
    )
    return SimpleNamespace(
        fake_root=fake_root,
        environ=environ,
        client=client,
        headers=_csrf_headers(client),
        wud_file=wud_file,
        original=original,
        active_dir=stack_dirs["active"],
        backup_dir=stack_dirs["backup"],
    )


def _selection_for_group(client, group_name: str) -> dict[str, object]:
    pending = client.get("/api/v1/pending").json()
    item = next(
        group["items"][0]
        for group in pending["grouping"]["groups"]
        if group["name"] == group_name
    )
    return {
        "line_no": item["line_no"],
        "selection_id": item["selection_id"],
    }


def _all_group_selections(client) -> list[dict[str, object]]:
    pending = client.get("/api/v1/pending").json()
    return [
        {
            "line_no": group["items"][0]["line_no"],
            "selection_id": group["items"][0]["selection_id"],
        }
        for group in pending["grouping"]["groups"]
    ]


def _plan_and_apply(
    client,
    headers: dict[str, str],
    selections: list[dict[str, object]],
    **plan_options: object,
) -> SimpleNamespace:
    plan_response = client.post(
        "/api/v1/plans",
        json={"selections": selections, **plan_options},
        headers=headers,
    )
    plan = plan_response.json()
    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "selections": plan["selected_selections"],
            "confirmation": "apply",
            **plan_options,
        },
        headers=headers,
    )
    job = _wait_apply_job(client, apply_response.json()["job_id"])
    assert plan_response.status_code == 200
    assert apply_response.status_code == 202
    return SimpleNamespace(
        plan_response=plan_response,
        plan=plan,
        apply_response=apply_response,
        job=job,
    )


def test_acquire_apply_wud_lock_coerces_env_timeout_to_int(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed: list[int] = []

    class FakeDirectoryLock:
        def __init__(self, _path: object, *, timeout_seconds: int) -> None:
            self.timeout_seconds = timeout_seconds
            observed.append(timeout_seconds)

        def acquire(self) -> None:
            pass

    monkeypatch.setattr(web_jobs, "DirectoryLock", FakeDirectoryLock)

    cases = [
        ({"WUD_LOCK_TIMEOUT": "5"}, 5),
        ({}, 30),
        ({"WUD_LOCK_TIMEOUT": "slow"}, 30),
        ({"WUD_LOCK_TIMEOUT": "-1"}, 30),
    ]

    for command_env, expected in cases:
        lock = web_jobs._acquire_apply_wud_lock(
            _settings_for_lock_timeout(tmp_path, command_env)
        )
        assert lock.timeout_seconds == expected

    assert observed == [5, 30, 30, 30]
    assert all(isinstance(timeout_seconds, int) for timeout_seconds in observed)


def test_refresh_api_pending_source_reports_degraded_detail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings_for_lock_timeout(tmp_path, {})
    jobs = {
        "job": WebApplyJob(
            id="job",
            status="running",
            selected_line_numbers=(1,),
        )
    }
    detail = "WUD API watch request failed: connection refused"
    snapshot = web_wud_api.WudApiSnapshot(
        status=web_wud_api.WudApiStatus(
            state="error",
            available=True,
            metadata_available=False,
            last_checked_at="2026-01-01T00:00:00+00:00",
            detail=detail,
        )
    )

    watch_result = web_wud_api.WudApiWatchResult(
        snapshot=snapshot,
        watched=False,
        requested_count=1,
        watched_count=0,
    )
    watched_ids: list[tuple[str, ...]] = []

    def watch_containers(
        _settings: WebSettings,
        container_ids,
    ) -> web_wud_api.WudApiWatchResult:
        watched_ids.append(tuple(container_ids))
        return watch_result

    monkeypatch.setattr(
        web_jobs.web_wud_api,
        "watch_containers",
        watch_containers,
    )
    checkpoints: list[WebSettings] = []
    monkeypatch.setattr(
        web_jobs.web_wud_api,
        "checkpoint_pending_observation_cache",
        checkpoints.append,
    )

    web_jobs._refresh_api_pending_source_after_apply(
        settings,
        jobs,
        web_jobs.Condition(),
        "job",
        ("docker.local.app",),
    )

    event = jobs["job"].progress[-1]
    assert event.phase == "wud-api-refresh"
    assert event.status == "skipped"
    assert event.message == f"WUD API pending refresh skipped. {detail}"
    assert watched_ids == [("docker.local.app",)]
    assert checkpoints == [settings]


def test_refresh_api_pending_source_logs_unexpected_watch_error(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    settings = _settings_for_lock_timeout(tmp_path, {})
    jobs = {
        "job": WebApplyJob(
            id="job",
            status="running",
            selected_line_numbers=(1,),
        )
    }

    def fail_refresh(*_args, **_kwargs):
        raise RuntimeError("watch exploded")

    monkeypatch.setattr(
        web_jobs.web_wud_api,
        "watch_containers",
        fail_refresh,
    )

    with caplog.at_level(logging.ERROR, logger=web_jobs.LOGGER.name):
        web_jobs._refresh_api_pending_source_after_apply(
            settings,
            jobs,
            web_jobs.Condition(),
            "job",
            ("docker.local.app",),
        )

    event = jobs["job"].progress[-1]
    assert event.phase == "wud-api-refresh"
    assert event.status == "skipped"
    assert event.message == "WUD API pending refresh skipped."
    assert "WUD API pending refresh failed" in caplog.text
    assert "watch exploded" in caplog.text


def test_successful_apply_survives_pending_checkpoint_failure(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    settings = _settings_for_lock_timeout(tmp_path, {})
    jobs = {
        "job": WebApplyJob(
            id="job",
            status="running",
            selected_line_numbers=(1,),
        )
    }
    snapshot = web_wud_api.WudApiSnapshot(
        status=web_wud_api.WudApiStatus(
            state="ready",
            available=True,
            metadata_available=True,
            last_checked_at="2026-01-01T00:00:00+00:00",
        )
    )
    watch_result = web_wud_api.WudApiWatchResult(
        snapshot=snapshot,
        watched=True,
        requested_count=1,
        watched_count=1,
    )

    def watch_selected(_settings, container_ids):
        assert tuple(container_ids) == ("docker.local.app",)
        return watch_result

    monkeypatch.setattr(
        web_jobs.web_wud_api,
        "watch_containers",
        watch_selected,
    )

    def fail_checkpoint(_settings: WebSettings) -> None:
        raise RuntimeError("checkpoint exploded")

    monkeypatch.setattr(
        web_jobs.web_wud_api,
        "checkpoint_pending_observation_cache",
        fail_checkpoint,
    )
    runner = SimpleNamespace(
        audit_run_id=7,
        log_file=tmp_path / "apply.log",
    )

    with caplog.at_level(logging.ERROR, logger=web_jobs.LOGGER.name):
        result = web_jobs._handle_apply_job_run_result(
            settings,
            jobs,
            web_jobs.Condition(),
            "job",
            runner,
            0,
            web_jobs.ApplyJobRunContext(
                pending_source_active="api",
                pending_source_text="registry.example/acme/app:1.0.0\n",
                pending_source_container_ids=("docker.local.app",),
            ),
            lambda *_args, **_kwargs: None,
        )

    assert result["status"] == "success"
    assert jobs["job"].progress[-1].status == "success"
    assert "WUD API pending observation checkpoint failed" in caplog.text
    assert "checkpoint exploded" not in caplog.text


def test_failed_api_apply_refreshes_pending_source_without_masking_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings_for_lock_timeout(tmp_path, {})
    jobs = {
        "job": WebApplyJob(
            id="job",
            status="running",
            selected_line_numbers=(1,),
        )
    }
    snapshot = web_wud_api.WudApiSnapshot(
        status=web_wud_api.WudApiStatus(
            state="ready",
            available=True,
            metadata_available=True,
            last_checked_at="2026-01-01T00:00:00+00:00",
        )
    )
    watched_ids: list[tuple[str, ...]] = []
    checkpoints: list[WebSettings] = []

    def watch_selected(_settings, container_ids):
        watched_ids.append(tuple(container_ids))
        return web_wud_api.WudApiWatchResult(
            snapshot=snapshot,
            watched=True,
            requested_count=1,
            watched_count=1,
        )

    monkeypatch.setattr(web_jobs.web_wud_api, "watch_containers", watch_selected)
    monkeypatch.setattr(
        web_jobs.web_wud_api,
        "checkpoint_pending_observation_cache",
        checkpoints.append,
    )

    result = web_jobs._handle_apply_job_run_result(
        settings,
        jobs,
        web_jobs.Condition(),
        "job",
        SimpleNamespace(audit_run_id=7, log_file=tmp_path / "apply.log"),
        1,
        web_jobs.ApplyJobRunContext(
            pending_source_active="api",
            pending_source_text="registry.example/acme/app:1.0.0\n",
            pending_source_container_ids=("docker.local.app",),
        ),
        lambda *_args, **_kwargs: None,
    )

    assert result["status"] == "failure"
    assert result["error"] == "updater exited with status 1"
    assert watched_ids == [("docker.local.app",)]
    assert checkpoints == [settings]
    assert jobs["job"].progress[-1].phase == "wud-api-refresh"
    assert jobs["job"].progress[-1].status == "success"


def test_apply_job_refreshes_only_api_pending_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings_for_lock_timeout(tmp_path, {})
    jobs = {
        "job": WebApplyJob(
            id="job",
            status="queued",
            selected_line_numbers=(1,),
        )
    }
    schedule_updates: list[tuple[tuple[str, ...], str, int | None, str]] = []

    class FakeRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            self.audit_run_id = 7
            self.log_file = tmp_path / "apply.log"
            self.options = SimpleNamespace(
                update_selections=(),
                completed_update_selections=(),
            )
            self.successful_completed_update_selections = ()
            self.discovered_completed_update_selections = ()

        def run(self) -> int:
            return 0

    def fail_refresh(*_args, **_kwargs):
        raise AssertionError("watch_containers called")

    def record_schedule_update(
        _settings,
        schedule_keys,
        *,
        status,
        run_id,
        error="",
    ) -> None:
        schedule_updates.append((tuple(schedule_keys), status, run_id, error))

    monkeypatch.setattr(web_jobs, "UpdateFromWudRunner", FakeRunner)
    monkeypatch.setattr(
        web_jobs.web_wud_api,
        "watch_containers",
        fail_refresh,
    )

    web_jobs._run_apply_job(
        settings,
        "plan",
        (1,),
        False,
        (),
        (),
        (),
        (),
        jobs,
        web_jobs.Condition(),
        "job",
        None,
        lambda settings: settings.config,
        record_schedule_update,
        web_jobs.ApplyJobRunContext(
            pending_source_active="file",
            pending_source_text="repo/app:latest\n",
        ),
    )

    job = jobs["job"]
    assert job.status == "success"
    assert job.run_id == 7
    assert all(event.phase != "wud-api-refresh" for event in job.progress)
    assert schedule_updates == [((), "success", 7, "")]


def test_self_update_endpoint_enforces_auth_csrf_read_only_and_active_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(self_update_module, "current_tag", lambda: "v0.24.2")
    monkeypatch.setattr(self_update_module, "fetch_latest_release_tag", lambda: "v0.25.0")
    monkeypatch.setattr(
        self_update_module,
        "_fetch_self_update_release_notes",
        lambda *_args, **_kwargs: ([], False, []),
    )
    payload = _self_update_payload()
    unauthenticated = _client(tmp_path, {"WUD_WEB_MUTATIONS_ENABLED": "true"})
    read_only = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
        },
    )
    mutating = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_WEB_RESTART_CONTAINER": "wudup",
        },
    )
    mutating.app.state.web_apply_jobs["job-active"] = WebApplyJob(
        id="job-active",
        status="running",
        selected_line_numbers=(1,),
    )

    unauthenticated_response = unauthenticated.post(
        "/api/v1/self-update",
        json=payload,
        headers=_csrf_headers(unauthenticated),
    )
    missing_csrf = mutating.post("/api/v1/self-update", json=payload)
    read_only_response = read_only.post(
        "/api/v1/self-update",
        json=payload,
        headers=_csrf_headers(read_only),
    )
    active_job = mutating.post(
        "/api/v1/self-update",
        json=payload,
        headers=_csrf_headers(mutating),
    )

    assert unauthenticated_response.status_code == 403
    assert unauthenticated_response.json()["detail"] == "setup required"
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "origin header is required"
    assert read_only_response.status_code == 403
    assert read_only_response.json()["detail"] == "mutations are disabled"
    assert active_job.status_code == 409
    assert active_job.json()["detail"] == "an apply job is already running"


def test_job_stream_returns_404_for_missing_job(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get("/api/v1/jobs/missing/stream")

    assert response.status_code == 404
    assert response.json()["detail"] == "apply job not found"


def test_job_status_snapshots_while_locked(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    job_id = "job-active"
    client.app.state.web_apply_jobs[job_id] = WebApplyJob(
        id=job_id,
        status="running",
        selected_line_numbers=(1,),
    )
    original_response = web_jobs._apply_job_response
    observed: dict[str, bool] = {}

    def assert_locked(job):
        observed["locked"] = client.app.state.web_apply_lock.locked()
        return original_response(job)

    monkeypatch.setattr(web_jobs, "_apply_job_response", assert_locked)

    response = client.get(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 200
    assert observed["locked"] is True


def test_job_stream_emits_initial_and_terminal_status(tmp_path: Path) -> None:
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
    hook = fake_root / "post-pull-hook"
    hook.write_text("#!/usr/bin/env bash\nsleep 0.1\n", encoding="utf-8")
    hook.chmod(0o755)
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

    with client.stream(
        "GET",
        f"/api/v1/jobs/{apply_response.json()['job_id']}/stream",
    ) as response:
        content = response.read().decode("utf-8")

    events = _sse_job_events(content)
    log_events = _sse_log_events(content)
    progress_events = _sse_progress_events(content)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert len(events) >= 2
    assert events[0]["job_id"] == apply_response.json()["job_id"]
    assert events[0]["status"] in {"queued", "running"}
    assert events[-1]["status"] == "success"
    assert events[-1]["selected_line_numbers"] == [1]
    assert events[-1]["progress"]
    assert progress_events
    assert progress_events[0]["job_id"] == apply_response.json()["job_id"]
    assert {event["phase"] for event in progress_events} >= {
        "preflight",
        "pull",
        "recreate",
        "health",
        "cleanup",
        "completion",
    }
    assert progress_events[-1]["phase"] == "completion"
    assert progress_events[-1]["status"] == "success"
    assert log_events
    assert log_events[0]["job_id"] == apply_response.json()["job_id"]
    assert log_events[0]["max_bytes"] == 65_536
    assert "docker-update-from-wud-v2" in str(log_events[0]["content"])
    event_names = _sse_event_names(content)
    assert event_names[-1] == "job"
    assert "log" in event_names[:-1]


def test_scoped_apply_updates_shared_stacks_sequentially_and_clears_line(
    tmp_path: Path,
) -> None:
    case = _shared_update_case(tmp_path)
    selection = _selection_for_group(case.client, "active")
    plan_response = case.client.post(
        "/api/v1/plans",
        json={"selections": [selection]},
        headers=case.headers,
    )
    plan = plan_response.json()

    stale_apply = case.client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "selections": [
                {"line_no": 1, "selection_id": f"sel-v1-{'0' * 64}"}
            ],
            "confirmation": "apply",
        },
        headers=case.headers,
    )
    apply_response = case.client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "selections": plan["selected_selections"],
            "confirmation": "apply",
        },
        headers=case.headers,
    )
    job = _wait_apply_job(case.client, apply_response.json()["job_id"])

    assert plan_response.status_code == 200
    assert [stack["name"] for stack in plan["stacks"]] == ["active"]
    assert stale_apply.status_code == 409
    assert stale_apply.json()["detail"] == "plan is stale"
    assert apply_response.status_code == 202
    assert job["status"] == "success"
    assert job["selected_line_numbers"] == [1]
    assert case.wud_file.read_text(encoding="utf-8") == case.original
    calls = _fake_docker_calls(case.fake_root)
    active_mutations = [
        line
        for line in calls.splitlines()
        if str(case.active_dir) in line
        and (" pull app" in line or " up -d " in line)
    ]
    backup_mutations = [
        line
        for line in calls.splitlines()
        if str(case.backup_dir) in line
        and (" pull app" in line or " up -d " in line)
    ]
    assert active_mutations
    assert backup_mutations == []
    assert {
        event["stack"]
        for event in job["progress"]
        if event["stack"]
    } == {"active"}
    run = case.client.get(f"/api/v1/runs/{job['run_id']}")
    assert run.status_code == 200
    assert {
        event["stack_name"]
        for event in run.json()["events"]
        if event["stack_name"]
    } == {"active"}

    unrelated = "repo/unrelated:latest\n"
    case.wud_file.write_text(
        f"{case.original}{unrelated}",
        encoding="utf-8",
    )
    restarted_client = _client(tmp_path, case.environ)
    restarted_headers = _csrf_headers(restarted_client)
    remaining_pending = restarted_client.get("/api/v1/pending").json()
    assert [
        group["name"] for group in remaining_pending["grouping"]["groups"]
    ] == ["backup"]
    legacy_plan = restarted_client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=restarted_headers,
    )
    assert legacy_plan.status_code == 200
    assert {
        stack["name"] for stack in legacy_plan.json()["stacks"]
    } == {"active", "backup"}
    backup_result = _plan_and_apply(
        restarted_client,
        restarted_headers,
        [_selection_for_group(restarted_client, "backup")],
    )

    assert [stack["name"] for stack in backup_result.plan["stacks"]] == ["backup"]
    assert backup_result.job["status"] == "success"
    assert case.wud_file.read_text(encoding="utf-8") == unrelated
    final_pending = restarted_client.get("/api/v1/pending").json()
    assert final_pending["count"] == 1
    assert final_pending["grouping"]["groups"] == []
    final_calls = _fake_docker_calls(case.fake_root)
    final_active_mutations = [
        line
        for line in final_calls.splitlines()
        if str(case.active_dir) in line
        and (" pull app" in line or " up -d " in line)
    ]
    final_backup_mutations = [
        line
        for line in final_calls.splitlines()
        if str(case.backup_dir) in line
        and (" pull app" in line or " up -d " in line)
    ]
    assert final_active_mutations == active_mutations
    assert final_backup_mutations
    assert {
        event["stack"]
        for event in backup_result.job["progress"]
        if event["stack"]
    } == {"backup"}


@pytest.mark.parametrize("mode", ["stop", "pause", "live"])
@pytest.mark.parametrize("image", [
    "ghcr.io/magrhino/wudup:latest-trivy",
    "ghcr.io/magrhino/wud-updater:v0.24.2",
])
def test_apply_job_blocks_self_recreate_before_mutation(
    tmp_path: Path, mode: str, image: str,
) -> None:
    case = _shared_update_case(
        tmp_path, wud_line=image, compose_image=image, pull_image=image, update_mode=mode,
    )
    original_compose = (case.active_dir / "docker-compose.yml").read_text()
    result = _plan_and_apply(
        case.client, case.headers, [_selection_for_group(case.client, "active")],
    )
    assert result.job["status"] == "failure"
    assert any(
        "would stop or recreate WUDup itself" in event["message"]
        for event in result.job["progress"]
    )
    assert case.wud_file.read_text() == case.original
    assert (case.active_dir / "docker-compose.yml").read_text() == original_compose
    calls = _fake_docker_calls(case.fake_root)
    assert not any(
        mutation in calls for mutation in (" pull ", " stop ", " pause ", " up -d ")
    )


@pytest.mark.parametrize("lookup_result", ["error", "empty", "compose_error"])
def test_apply_job_fails_closed_when_protected_identity_is_unavailable(
    tmp_path: Path, monkeypatch, lookup_result: str,
) -> None:
    image = "example/custom-updater:latest"
    case = _shared_update_case(
        tmp_path, wud_line=image, compose_image=image, pull_image=image,
        restart_container="renamed-server",
    )
    inspect = DockerCli.inspect

    def inspect_identity(docker, target, fmt):
        if target == "renamed-server" and fmt == "{{.Id}}":
            if lookup_result == "error":
                raise CommandError(CommandResult(
                    ("docker", "inspect"), None, 1, stderr="private daemon detail",
                ))
            return ["cid-active"] if lookup_result == "compose_error" else []
        return inspect(docker, target, fmt)

    monkeypatch.setattr(DockerCli, "inspect", inspect_identity)
    if lookup_result == "compose_error":
        def fail_compose_lookup(*_args, **_kwargs):
            raise CommandError(CommandResult(
                ("docker", "compose", "ps"), None, 1, stderr="private daemon detail",
            ))

        monkeypatch.setattr(ComposeCli, "ps_quiet_checked", fail_compose_lookup)
    original_compose = (case.active_dir / "docker-compose.yml").read_text()
    result = _plan_and_apply(
        case.client, case.headers, [_selection_for_group(case.client, "active")],
    )
    assert result.job["status"] == "failure"
    assert result.job["run_id"] is not None
    expected_message = (
        "Could not verify the update's recreate scope"
        if lookup_result == "compose_error"
        else "Could not verify the WUDup container identity"
    )
    assert any(
        expected_message in event["message"]
        for event in result.job["progress"]
    )
    assert "private daemon detail" not in str(result.job)
    assert case.wud_file.read_text() == case.original
    assert (case.active_dir / "docker-compose.yml").read_text() == original_compose
    assert not any(
        mutation in _fake_docker_calls(case.fake_root)
        for mutation in (" pull ", " stop ", " pause ", " up -d ")
    )


def test_scoped_apply_preserves_completions_for_other_pending_lines(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    environ = {
        "WUD_WEB_DEV_NO_AUTH": "true",
        "WUD_WEB_MUTATIONS_ENABLED": "true",
        **fake_env,
    }
    client = _client(tmp_path, environ)
    wud_file = tmp_path / "state" / "images.todo"
    original = "repo/one:latest\nrepo/two:latest\n"
    wud_file.write_text(original, encoding="utf-8")
    for image_name in ("one", "two"):
        for stack_role in ("active", "backup"):
            _make_fake_stack(
                tmp_path,
                fake_root,
                f"{image_name}-{stack_role}",
                [
                    (
                        "app",
                        f"repo/{image_name}:latest",
                        f"cid-{image_name}-{stack_role}",
                    )
                ],
            )
        _write_fake_image_after_pull(
            fake_root,
            f"repo/{image_name}:latest",
            f"sha256:{image_name}-new-id",
            f"sha256:{image_name}-new",
        )
    headers = _csrf_headers(client)

    for group_name in ("one-active", "two-active"):
        result = _plan_and_apply(
            client,
            headers,
            [_selection_for_group(client, group_name)],
        )
        assert result.job["status"] == "success"

    assert wud_file.read_text(encoding="utf-8") == original
    restarted_client = _client(tmp_path, environ)
    remaining = restarted_client.get("/api/v1/pending").json()
    assert {
        group["name"] for group in remaining["grouping"]["groups"]
    } == {"one-backup", "two-backup"}


def test_legacy_broad_apply_clears_scoped_completion_for_target(
    tmp_path: Path,
) -> None:
    case = _shared_update_case(tmp_path)
    scoped_result = _plan_and_apply(
        case.client,
        case.headers,
        [_selection_for_group(case.client, "active")],
    )
    assert scoped_result.job["status"] == "success"
    assert case.wud_file.read_text(encoding="utf-8") == case.original

    legacy_plan_response = case.client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=case.headers,
    )
    legacy_plan = legacy_plan_response.json()
    legacy_apply = case.client.post(
        "/api/v1/jobs",
        json={
            "plan_id": legacy_plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=case.headers,
    )
    legacy_job = _wait_apply_job(case.client, legacy_apply.json()["job_id"])

    assert legacy_plan_response.status_code == 200
    assert {
        stack["name"] for stack in legacy_plan["stacks"]
    } == {"active", "backup"}
    assert legacy_apply.status_code == 202
    assert legacy_job["status"] == "success"
    assert case.wud_file.read_text(encoding="utf-8") == ""

    case.wud_file.write_text(case.original, encoding="utf-8")
    restarted_client = _client(tmp_path, case.environ)
    requeued = restarted_client.get("/api/v1/pending").json()
    assert {
        group["name"] for group in requeued["grouping"]["groups"]
    } == {"active", "backup"}


def test_scoped_tag_rewrite_keeps_completed_stack_hidden_after_restart(
    tmp_path: Path,
) -> None:
    case = _shared_update_case(
        tmp_path,
        wud_line="repo/shared:1.0 tag=2.0",
        compose_image="repo/shared:1.0",
        pull_image="repo/shared:2.0",
    )
    result = _plan_and_apply(
        case.client,
        case.headers,
        [_selection_for_group(case.client, "active")],
        allow_tag_updates=True,
    )

    assert result.job["status"] == "success"
    assert case.wud_file.read_text(encoding="utf-8") == case.original
    assert "image: repo/shared:2.0" in (
        case.active_dir / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    assert "image: repo/shared:1.0" in (
        case.backup_dir / "docker-compose.yml"
    ).read_text(encoding="utf-8")

    restarted_client = _client(tmp_path, case.environ)
    remaining = restarted_client.get("/api/v1/pending").json()
    assert [
        group["name"] for group in remaining["grouping"]["groups"]
    ] == ["backup"]


def test_scoped_apply_checkpoints_successful_stack_when_sibling_fails(
    tmp_path: Path,
) -> None:
    case = _shared_update_case(tmp_path)
    pull_failure = case.fake_root / "stacks" / "backup" / "pull_fail"
    pull_failure.write_text(
        "fail\n",
        encoding="utf-8",
    )
    result = _plan_and_apply(
        case.client,
        case.headers,
        _all_group_selections(case.client),
    )

    assert result.job["status"] == "failure"
    assert case.wud_file.read_text(encoding="utf-8") == case.original
    first_calls = _fake_docker_calls(case.fake_root)
    active_mutations = [
        line
        for line in first_calls.splitlines()
        if str(case.active_dir) in line
        and (" pull app" in line or " up -d " in line)
    ]
    assert active_mutations
    assert any(
        str(case.backup_dir) in line and " pull app" in line
        for line in first_calls.splitlines()
    )

    restarted_client = _client(tmp_path, case.environ)
    restarted_headers = _csrf_headers(restarted_client)
    remaining = restarted_client.get("/api/v1/pending").json()
    assert [
        group["name"] for group in remaining["grouping"]["groups"]
    ] == ["backup"]
    pull_failure.unlink()
    retry_result = _plan_and_apply(
        restarted_client,
        restarted_headers,
        [_selection_for_group(restarted_client, "backup")],
    )

    assert [stack["name"] for stack in retry_result.plan["stacks"]] == ["backup"]
    assert retry_result.job["status"] == "success"
    assert case.wud_file.read_text(encoding="utf-8") == ""
    final_calls = _fake_docker_calls(case.fake_root)
    final_active_mutations = [
        line
        for line in final_calls.splitlines()
        if str(case.active_dir) in line
        and (" pull app" in line or " up -d " in line)
    ]
    assert final_active_mutations == active_mutations


def test_scoped_apply_fails_when_completion_checkpoint_cannot_persist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    case = _shared_update_case(tmp_path)
    plan_response = case.client.post(
        "/api/v1/plans",
        json={"selections": [_selection_for_group(case.client, "active")]},
        headers=case.headers,
    )
    plan = plan_response.json()

    def fail_checkpoint(*_args, **_kwargs) -> None:
        raise RuntimeError("private database path")

    monkeypatch.setattr(
        web_jobs.web_file_selection_store,
        "replace_completed_update_selections",
        fail_checkpoint,
    )
    apply_response = case.client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "selections": plan["selected_selections"],
            "confirmation": "apply",
        },
        headers=case.headers,
    )
    job = _wait_apply_job(case.client, apply_response.json()["job_id"])

    assert plan_response.status_code == 200
    assert apply_response.status_code == 202
    assert job["status"] == "failure"
    assert job["error"] == "Could not persist partial update completion state."
    assert "private database path" not in str(job)
    assert case.wud_file.read_text(encoding="utf-8") == case.original


def test_file_selection_checkpoint_read_failure_is_not_reported_as_success(
    tmp_path: Path,
) -> None:
    settings = _settings_for_lock_timeout(tmp_path, {})
    settings.config.wud_out_file.parent.mkdir(parents=True)
    settings.config.wud_out_file.mkdir()
    selection = CompletedUpdateSelection("target", "current")
    runner = SimpleNamespace(
        options=SimpleNamespace(
            update_selections=(SimpleNamespace(line_no=1, selection_id="selected"),),
            completed_update_selections=(),
        ),
        successful_completed_update_selections=(selection,),
        discovered_completed_update_selections=(selection,),
    )
    run_context = web_jobs.ApplyJobRunContext(
        pending_source_active="file",
    )

    with pytest.raises(
        web_jobs.web_file_selection_store.FileSelectionCheckpointError,
        match="Could not persist partial update completion state",
    ):
        web_jobs._checkpoint_file_selection_completions(
            settings,
            runner,
            run_context=run_context,
        )


def test_job_stream_caps_live_log_tail_size(tmp_path: Path) -> None:
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
    apply_response = client.post(
        "/api/v1/jobs",
        json={
            "plan_id": plan["plan_id"],
            "line_numbers": [1],
            "confirmation": "apply",
        },
        headers=headers,
    )

    with client.stream(
        "GET",
        f"/api/v1/jobs/{apply_response.json()['job_id']}/stream?log_tail_bytes=9999999",
    ) as response:
        content = response.read().decode("utf-8")

    log_events = _sse_log_events(content)
    assert response.status_code == 200
    assert log_events
    assert log_events[0]["max_bytes"] == 1_048_576


def test_job_status_get_and_stream_do_not_mutate(tmp_path: Path) -> None:
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
    (fake_root / "calls.log").write_text("", encoding="utf-8")

    status_response = client.get(f"/api/v1/jobs/{job['job_id']}")
    stream_response = client.get(f"/api/v1/jobs/{job['job_id']}/stream")

    assert status_response.status_code == 200
    assert status_response.json()["status"] == "success"
    assert stream_response.status_code == 200
    assert _sse_job_events(stream_response.text)[-1]["status"] == "success"
    assert _fake_docker_calls(fake_root) == ""
