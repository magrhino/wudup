from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from tests.web_retag_test_helpers import (
    _apply_retag_plan,
    _create_retag_plan,
    _make_retag_fixture,
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

from wudup import web_retag_apply, web_retag_audit, web_retags
from wudup.compose import ComposeCli


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


def test_retag_apply_rejects_later_stack_changed_after_plan_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    dirs: dict[str, Path] = {}
    for name in ("alpha", "bravo"):
        image = f"repo/{name}@sha256:old"
        dirs[name] = _make_fake_stack(
            tmp_path, fake_root, name, [("app", image, f"cid-{name}")]
        )
        _write_compose(dirs[name], "app", image, label_value="^latest$$")
        _seed_known_image(
            tmp_path,
            service_key=f"{name}/app",
            image=image,
            source_image=f"repo/{name}:latest",
            resolved_tag="2.0",
            watch_tag="latest",
            target_digest="sha256:old",
            final_image=image,
        )
    _set_retag_digest_pins(tmp_path)
    bravo_file = dirs["bravo"] / "docker-compose.yml"
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
    choices = [_switch_choice("alpha/app"), _switch_choice("bravo/app")]
    plan = _create_retag_plan(client, headers, choices)

    response = _apply_retag_plan(client, headers, plan, choices)

    assert response.status_code == 202
    job = _wait_apply_job(client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "retag plan is stale" in job["error"]
    assert "Compose file for bravo changed after the plan was approved" in job["error"]
    assert bravo_file.read_bytes() == edited
    assert "wud.tag.include=^2\\.0$$" in (
        dirs["alpha"] / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    bravo_mutations = [
        line
        for line in _fake_docker_calls(fake_root).splitlines()
        if line.startswith(f"{dirs['bravo']}\t")
        and (" pull " in line or " up " in line or " stop " in line)
    ]
    assert bravo_mutations == []
    assert not list(dirs["bravo"].glob(".docker-compose.yml.backup.*"))
    _wait_run_status(tmp_path / "state" / "wud.sqlite", job["run_id"], "failure")


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
