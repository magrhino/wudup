from __future__ import annotations

import hashlib
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from tests.web_retag_test_helpers import (
    _apply_retag_plan,
    _create_retag_plan,
    _make_retag_fixture,
    _wait_run_status,
)
from tests.web_test_helpers import _csrf_headers, _wait_apply_job

from wudup import web_jobs, web_retag_apply, web_retag_audit, web_retags
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
    closed = Event()
    acquire = web_jobs._acquire_apply_wud_lock

    def acquire_lock(*args: object, **kwargs: object) -> SimpleNamespace:
        lock = acquire(*args, **kwargs)
        events.append("lock")

        def close() -> None:
            lock.close()
            events.append("unlock")
            closed.set()

        return SimpleNamespace(close=close)

    monkeypatch.setattr(web_jobs, "_acquire_apply_wud_lock", acquire_lock)

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
    assert closed.wait(5), "retag worker did not release its WUD lock"
    prefix = ["lock", "plan", "audit-start", "apply", "runtime", "backup", "rewrite"]
    suffix = ["cleanup", "audit-finish", "unlock"]
    stages = ["pull", "recreate", "health", "known"]
    if failure is None:
        expected = prefix + stages + suffix
        assert job["status"] == "success"
        assert compose_file.read_bytes() != before
    else:
        attempted = [] if failure == "rewrite" else stages[:stages.index(failure) + 1]
        recovered = [] if failure == "rewrite" else ["restore", "health"]
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
