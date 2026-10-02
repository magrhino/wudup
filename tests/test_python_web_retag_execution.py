from __future__ import annotations

import hashlib
from pathlib import Path
from threading import Condition
from types import SimpleNamespace
from typing import Any

import pytest
from tests.health_gate_test_helpers import (
    FakeHealthCompose,
    FakeHealthDocker,
    health_stack,
)
from tests.web_retag_test_helpers import (
    _apply_retag_plan,
    _create_retag_plan,
    _make_retag_fixture,
    _make_two_stack_fixture,
    _switch_choice,
    _wait_run_status,
)
from tests.web_test_helpers import (
    _csrf_headers,
    _fake_docker_calls,
    _wait_apply_job,
)

from wudup import web_retag_apply, web_retag_audit, web_retags
from wudup.command import CommandError, CommandResult
from wudup.compose import ComposeCli, ComposeStack


@pytest.mark.parametrize("failure", [None, "rewrite", "pull", "health", "known"])
def test_retag_execution_preserves_mutation_recovery_and_audit_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
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
    before = compose_file.read_bytes()
    events: list[str] = []
    errors: list[Exception] = []
    source_hashes: dict[str, str] = {}

    def track(owner: object, name: str, event: str) -> None:
        original = getattr(owner, name)

        def call(*args: object, **kwargs: object) -> object:
            events.append(event)
            if event == "rewrite":
                source_hashes["backup"] = kwargs["expected_source_hash"]
            if event == "restore":
                source_hashes["written"] = kwargs["expected_source_hash"]
            if event == failure and events.count(event) == 1:
                raise RuntimeError(f"injected {event} failure")
            try:
                result = original(*args, **kwargs)
                if event == "rewrite":
                    source_hashes["actual"] = hashlib.sha256(
                        compose_file.read_bytes()
                    ).hexdigest()
                return result
            except Exception as exc:
                if event == "apply":
                    errors.append(exc)
                raise

        monkeypatch.setattr(owner, name, call)

    for owner, name, event in (
        (web_retags, "_build_current_retag_plan", "plan"),
        (web_retag_audit, "_insert_retag_audit_run", "audit-start"),
        (web_retag_apply, "_apply_retag_updates", "apply"),
        (web_retag_apply, "_revalidate_retag_runtime_before_apply", "runtime"),
        (web_retag_apply, "_backup_compose", "backup"),
        (web_retag_apply, "apply_compose_retag_updates", "rewrite"),
        (web_retag_apply, "_recreate_retag_services", "recreate"),
        (web_retag_apply, "_wait_for_retag_health", "health"),
        (web_retag_audit, "_record_successful_retag_known_images", "known"),
        (web_retag_apply, "restore_compose_backup", "restore"),
        (web_retag_apply, "_delete_path", "cleanup"),
        (web_retag_audit, "_finish_retag_audit_run", "audit-finish"),
    ):
        track(owner, name, event)
    track(ComposeCli, "pull", "pull")

    response = _apply_retag_plan(fixture.client, headers, plan)
    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    prefix = ["plan", "audit-start", "apply", "runtime", "backup", "rewrite"]
    suffix = ["cleanup", "audit-finish"]
    stages = ["pull", "recreate", "health", "known"]
    if failure is None:
        expected = prefix + stages + suffix
        assert job["status"] == "success"
        assert compose_file.read_bytes() != before
    else:
        attempted = [] if failure == "rewrite" else stages[:stages.index(failure) + 1]
        if failure == "rewrite":
            recovered = []
        elif failure == "pull":
            # Nothing was recreated yet, so only the Compose file is restored.
            recovered = ["restore"]
        else:
            recovered = ["restore", "health"]
        expected = prefix + attempted + recovered + suffix
        assert job["status"] == "failure"
        assert compose_file.read_bytes() == before
        assert len(errors) == 1
        assert isinstance(errors[0], web_retag_apply._RetagApplyFailed)
        assert errors[0].successful_updates == ()
        assert isinstance(errors[0].__cause__, RuntimeError)
        assert str(errors[0].__cause__) == f"injected {failure} failure"
        if recovered:
            assert source_hashes["written"] == source_hashes["actual"]
    assert source_hashes["backup"] == hashlib.sha256(before).hexdigest()
    assert events == expected


def test_retag_recovery_retains_backup_when_compose_changed_after_rewrite(
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
    before = compose_file.read_bytes()
    edited = b"# concurrent operator edit\n" + before
    backups: list[Path] = []
    backup_compose = web_retag_apply._backup_compose

    def remember_backup(path: Path) -> Path:
        backup = backup_compose(path)
        backups.append(backup)
        return backup

    def change_source_then_fail(*args: object, **kwargs: object) -> None:
        compose_file.write_bytes(edited)
        raise RuntimeError("injected pull failure")

    monkeypatch.setattr(web_retag_apply, "_backup_compose", remember_backup)
    monkeypatch.setattr(ComposeCli, "pull", change_source_then_fail)

    response = _apply_retag_plan(fixture.client, headers, plan)
    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    _wait_run_status(tmp_path / "state" / "wud.sqlite", job["run_id"], "failure")
    assert compose_file.read_bytes() == edited
    assert len(backups) == 1
    assert backups[0].read_bytes() == before
    assert "compose rollback failed" in job["error"]
    assert "backup retained at [REDACTED_PATH]" in job["error"]
    assert str(tmp_path) not in job["error"]


@pytest.mark.parametrize(
    ("unreadable_at_plan", "stale_error"),
    [
        (False, "Compose file for bravo changed after the plan was approved"),
        (True, "could not read the Compose file for bravo when the plan was made"),
    ],
)
def test_retag_apply_rejects_later_stack_changed_after_plan_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unreadable_at_plan: bool,
    stale_error: str,
) -> None:
    fixture = _make_two_stack_fixture(tmp_path)
    client = fixture.client
    dirs = {"alpha": fixture.alpha_dir, "bravo": fixture.bravo_dir}
    bravo_file = dirs["bravo"] / "docker-compose.yml"
    if unreadable_at_plan:
        # Planning stores an empty hash when it cannot read a Compose file.
        original_hashes = web_retags._compose_hashes

        def blank_bravo_hash(selected: Any) -> dict[str, str]:
            hashes = original_hashes(selected)
            hashes[str(bravo_file)] = ""
            return hashes

        monkeypatch.setattr(web_retags, "_compose_hashes", blank_bravo_hash)
        edited = bravo_file.read_bytes()
    else:
        edited = bravo_file.read_bytes().replace(
            b"    labels:\n", b"    labels:\n      - operator.edit=true\n"
        )
    original_pull = ComposeCli.pull

    def edit_bravo_while_alpha_pulls(
        self: ComposeCli, directory: object, *args: object, **kwargs: object
    ) -> object:
        if Path(str(directory)) == dirs["alpha"]:
            bravo_file.write_bytes(edited)
        return original_pull(self, directory, *args, **kwargs)

    monkeypatch.setattr(ComposeCli, "pull", edit_bravo_while_alpha_pulls)
    headers = _csrf_headers(client)
    plan = _create_retag_plan(client, headers, fixture.choices)

    response = _apply_retag_plan(client, headers, plan, fixture.choices)

    assert response.status_code == 202
    job = _wait_apply_job(client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "retag plan is stale" in job["error"]
    assert stale_error in job["error"]
    assert bravo_file.read_bytes() == edited
    assert "wud.tag.include=^2\\.0$$" in (
        dirs["alpha"] / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    bravo_mutations = [
        line
        for line in _fake_docker_calls(fixture.fake_root).splitlines()
        if line.startswith(f"{dirs['bravo']}\t")
        and (" pull " in line or " up " in line or " stop " in line)
    ]
    assert bravo_mutations == []
    assert not list(dirs["bravo"].glob(".docker-compose.yml.backup.*"))
    _wait_run_status(fixture.db_path, job["run_id"], "failure")


@pytest.mark.parametrize("stays_stopped", [True, False])
def test_retag_rollback_keeps_service_stopped_before_apply_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stays_stopped: bool,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
    )
    # The service is stopped, so the operator approves starting it on the new image.
    (fixture.fake_root / "compose-runtime.tsv").write_text("", encoding="utf-8")
    if stays_stopped:
        # No container reports as running, so the health wait fails after recreate.
        (fixture.fake_root / "stacks" / "stack" / "cids-app.txt").unlink()
    else:
        def fail_known_images(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected known-image failure")

        monkeypatch.setattr(
            web_retag_audit,
            "_record_successful_retag_known_images",
            fail_known_images,
        )
    compose_file = fixture.compose_dir / "docker-compose.yml"
    before = compose_file.read_bytes()
    headers = _csrf_headers(fixture.client)
    choice = {**_switch_choice(), "allow_start": True}
    plan = _create_retag_plan(fixture.client, headers, [choice])

    response = _apply_retag_plan(fixture.client, headers, plan, [choice])

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert compose_file.read_bytes() == before
    calls = _fake_docker_calls(fixture.fake_root)
    up_calls = [
        line.split("\t", 1)[1] for line in calls.splitlines() if " up -d " in line
    ]
    recreate = (
        "compose -f docker-compose.yml up -d --remove-orphans --pull never "
        "--no-build --force-recreate --no-deps"
    )
    # Apply starts the service on the new image; rollback recreates it without starting.
    assert up_calls == [f"{recreate} app", f"{recreate} --no-start app"]
    if stays_stopped:
        assert job["error"].endswith(
            "rollback restored the Compose file for stack; recreated app on the "
            "previous image without starting it, because it was stopped before "
            "the apply"
        )
        assert not list(fixture.compose_dir.glob(".docker-compose.yml.backup.*"))
    else:
        assert "compose -f docker-compose.yml stop app" in calls
        assert "app started during rollback although it was stopped" in job["error"]
        assert "after the Compose file was restored" in job["error"]


class _RollbackCompose:
    """Fake Compose CLI that records rollback commands and injects failures."""

    def __init__(
        self,
        *,
        stopped_up_fails: bool = False,
        running_up_fails: bool = False,
        ps_result: str | None = "",
        stop_fails: bool = False,
    ) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.remove_orphans: list[bool] = []
        self.stopped_up_fails = stopped_up_fails
        self.running_up_fails = running_up_fails
        self.ps_result = ps_result
        self.stop_fails = stop_fails

    @staticmethod
    def _error(name: str) -> CommandError:
        return CommandError(CommandResult(("docker", "compose", name), None, 1))

    def up(self, directory: Path, file: str, services: Any, **kwargs: Any) -> None:
        no_start = bool(kwargs.get("no_start"))
        self.calls.append(("up-no-start" if no_start else "up", tuple(services)))
        self.remove_orphans.append(kwargs.get("remove_orphans", True))
        if no_start and self.stopped_up_fails:
            raise self._error("up-no-start")
        if not no_start and self.running_up_fails:
            raise self._error("up")

    def up_wait_supported(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def ps_quiet_checked(
        self, directory: Path, file: str, services: Any, **kwargs: Any
    ) -> str:
        self.calls.append(("ps", tuple(services)))
        if self.ps_result is None:
            raise self._error("ps")
        return self.ps_result

    def stop(self, directory: Path, file: str, services: Any, **kwargs: Any) -> None:
        self.calls.append(("stop", tuple(services)))
        if self.stop_fails:
            raise self._error("stop")


def _restore_mixed_stack(
    tmp_path: Path,
    compose: _RollbackCompose,
    *,
    remove_orphans: bool = True,
) -> str:
    """Roll back a stack where db was stopped and web was running before apply."""
    stack = ComposeStack(
        index=1,
        directory=tmp_path,
        file="docker-compose.yml",
        name="stack",
        images=(),
        service_images=(),
    )
    backup = tmp_path / "backup.yml"
    backup.write_text("services: {}\n", encoding="utf-8")
    return web_retag_apply._restore_retag_compose(
        compose,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        SimpleNamespace(update_mode="live", max_wait=0),  # type: ignore[arg-type]
        stack,
        ("db", "web"),
        ("db",),
        backup,
        {},
        Condition(),
        "job",
        original_error="health failed",
        expected_source_hash="hash",
        remove_orphans=remove_orphans,
    )


@pytest.fixture
def _no_compose_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        web_retag_apply, "restore_compose_backup", lambda *args, **kwargs: None
    )


@pytest.mark.usefixtures("_no_compose_restore")
def test_retag_rollback_restores_mixed_stopped_and_running_services(
    tmp_path: Path,
) -> None:
    compose = _RollbackCompose()

    summary = _restore_mixed_stack(tmp_path, compose)

    assert compose.calls == [
        ("up-no-start", ("db",)),
        ("ps", ("db",)),
        ("up", ("web",)),
    ]
    assert "recreated and started web on the previous image" in summary
    assert "recreated db on the previous image without starting it" in summary
    assert not (tmp_path / "backup.yml").exists()


@pytest.mark.usefixtures("_no_compose_restore")
def test_retag_rollback_keeps_orphans_when_project_name_is_shared(
    tmp_path: Path,
) -> None:
    compose = _RollbackCompose()

    _restore_mixed_stack(tmp_path, compose, remove_orphans=False)

    # Both the stopped and the running service group must avoid --remove-orphans,
    # or Compose would delete the other file set's containers.
    assert [call[0] for call in compose.calls if call[0].startswith("up")] == [
        "up-no-start",
        "up",
    ]
    assert compose.remove_orphans == [False, False]


@pytest.mark.usefixtures("_no_compose_restore")
@pytest.mark.parametrize(
    ("compose_kwargs", "stopped_error", "stopped_again"),
    [
        (
            {"stopped_up_fails": True},
            "WUDup could not recreate db on the previous image without starting",
            True,
        ),
        ({"ps_result": "cid-db"}, "db started during rollback", True),
        ({"ps_result": None}, "WUDup could not check that db stayed stopped", True),
    ],
)
def test_retag_rollback_still_restores_running_services_after_stopped_failure(
    tmp_path: Path,
    compose_kwargs: dict[str, Any],
    stopped_error: str,
    stopped_again: bool,
) -> None:
    compose = _RollbackCompose(**compose_kwargs)

    with pytest.raises(RuntimeError) as raised:
        _restore_mixed_stack(tmp_path, compose)

    # The running service is rolled back even though the stopped one failed.
    assert ("up", ("web",)) in compose.calls
    message = str(raised.value)
    assert message.startswith(
        "health failed; compose rollback failed after the Compose file was "
        "restored: db (stopped before the apply) could not be rolled back: "
    )
    assert stopped_error in message
    assert "web could not be rolled back" not in message
    # Whenever db may be running, WUDup stops it again.
    assert (("stop", ("db",)) in compose.calls) is stopped_again
    assert ("WUDup stopped it again" in message) is stopped_again
    assert (tmp_path / "backup.yml").exists()


@pytest.mark.usefixtures("_no_compose_restore")
def test_retag_rollback_reports_every_service_group_that_failed(
    tmp_path: Path,
) -> None:
    compose = _RollbackCompose(stopped_up_fails=True, running_up_fails=True)

    with pytest.raises(RuntimeError) as raised:
        _restore_mixed_stack(tmp_path, compose)

    message = str(raised.value)
    assert "db (stopped before the apply) could not be rolled back" in message
    assert "web could not be rolled back to the previous image" in message
    assert message.endswith(f"backup retained at {tmp_path / 'backup.yml'}")
    assert (tmp_path / "backup.yml").exists()


@pytest.mark.usefixtures("_no_compose_restore")
def test_retag_rollback_reports_when_stopping_service_again_fails(
    tmp_path: Path,
) -> None:
    compose = _RollbackCompose(stopped_up_fails=True, stop_fails=True)

    with pytest.raises(RuntimeError) as raised:
        _restore_mixed_stack(tmp_path, compose)

    assert ("stop", ("db",)) in compose.calls
    message = str(raised.value)
    assert "WUDup tried to stop it again but that failed" in message
    assert "WUDup stopped it again" not in message
    assert (tmp_path / "backup.yml").exists()


@pytest.mark.usefixtures("_no_compose_restore")
def test_retag_rollback_reports_backup_cleanup_failure_after_services_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_delete(path: Path) -> None:
        raise PermissionError("backup cleanup denied")

    monkeypatch.setattr(web_retag_apply, "_delete_path", deny_delete)
    compose = _RollbackCompose()

    with pytest.raises(RuntimeError) as raised:
        _restore_mixed_stack(tmp_path, compose)

    # Both service groups were rolled back before cleanup failed.
    assert compose.calls == [
        ("up-no-start", ("db",)),
        ("ps", ("db",)),
        ("up", ("web",)),
    ]
    assert str(raised.value) == (
        "health failed; compose rollback failed after the Compose file was "
        "restored: backup cleanup denied; backup retained at "
        f"{tmp_path / 'backup.yml'}"
    )
    assert isinstance(raised.value.__cause__, PermissionError)
    assert (tmp_path / "backup.yml").exists()


def test_retag_runtime_revalidation_without_updates_reports_no_stopped_services() -> None:
    # No approved updates means no Compose or runtime lookups are needed.
    stopped, _remove_orphans = web_retag_apply._revalidate_retag_runtime_before_apply(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        (),
    )
    assert stopped == ()


def _retag_health_error(
    tmp_path: Path,
    compose: FakeHealthCompose,
    services: tuple[str, ...],
    docker: FakeHealthDocker | None = None,
) -> str:
    args = (
        compose,
        docker or FakeHealthDocker(),
        SimpleNamespace(max_wait=0),
        health_stack(tmp_path),
        services,
        {},
        Condition(),
        "job",
    )
    with pytest.raises(RuntimeError) as raised:
        web_retag_apply._wait_for_retag_health(*args)
    return str(raised.value)


# `compose ps -q` lists only running containers, so an exited worker has none;
# a combined lookup would hide it behind the running app.
@pytest.mark.parametrize(
    ("compose", "expected", "unexpected"),
    [
        (
            FakeHealthCompose({"app": ["cid-app"], "worker": []}),
            "no running container for service(s): worker",
            "could not list containers",
        ),
        (
            FakeHealthCompose(
                {"app": ["cid-app"], "worker": []}, failing=["worker"]
            ),
            (
                "could not list containers for service(s): worker because docker "
                "compose ps failed"
            ),
            "exited or never started",
        ),
        (
            FakeHealthCompose({"app": [], "worker": []}),
            "no running container for service(s): app, worker",
            "returned no containers",
        ),
    ],
    ids=["exited", "compose_error", "all_exited"],
)
def test_retag_health_wait_fails_when_selected_service_has_no_container(
    tmp_path: Path,
    compose: FakeHealthCompose,
    expected: str,
    unexpected: str,
) -> None:
    error = _retag_health_error(tmp_path, compose, ("app", "worker"))

    assert expected in error
    assert unexpected not in error


@pytest.mark.parametrize(
    ("compose", "expected", "unexpected"),
    [
        (
            FakeHealthCompose({"app": ["cid-app"]}, fail_all=True),
            (
                "could not list containers for service(s): (all services) because "
                "docker compose ps failed"
            ),
            "returned no containers",
        ),
        (
            FakeHealthCompose({"app": []}),
            "docker compose ps -q returned no containers",
            "could not list containers",
        ),
    ],
    ids=["compose_error", "no_containers"],
)
def test_retag_health_wait_reports_whole_stack_lookup(
    tmp_path: Path,
    compose: FakeHealthCompose,
    expected: str,
    unexpected: str,
) -> None:
    error = _retag_health_error(tmp_path, compose, ())

    assert expected in error
    assert unexpected not in error


def test_retag_health_wait_reports_unhealthy_container_output(
    tmp_path: Path,
) -> None:
    docker = FakeHealthDocker(
        summaries={"cid-app": "/app|running|unhealthy|1|0"},
        health_logs={"cid-app": ["", "probe failed: connection refused"]},
    )

    error = _retag_health_error(
        tmp_path, FakeHealthCompose({"app": ["cid-app"]}), ("app",), docker
    )

    assert "/app|running|unhealthy|1|0" in error
    assert "probe failed: connection refused" in error
    assert "no running container" not in error
