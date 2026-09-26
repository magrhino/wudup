from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.web_retag_test_helpers import (
    _apply_retag_plan,
    _audit_settings,
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
    _fake_docker_env,
    _make_fake_stack,
    _wait_apply_job,
)

from wudup import web_retag_apply, web_retag_audit
from wudup.compose import ComposeStack, ServiceImage
from wudup.db import init_db, open_db, upsert_known_image
from wudup.digest_provenance import DigestTagProvenance
from wudup.updater_digest_pin import digest_pin_update_from_values
from wudup.web_retag_plans import RetagPlanUpdate

_LIVE_ENV = {
    "WUD_WEB_MUTATIONS_ENABLED": "true",
    "WUD_UPDATE_MODE": "live",
    "WUD_MAX_WAIT": "0",
}


def _known_image_row(db_path: Path, service_key: str) -> dict[str, object] | None:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM known_images WHERE service_key = ?",
            (service_key,),
        ).fetchone()
    return None if row is None else dict(row)


def _run_event_metadata(db_path: Path, run_id: object) -> list[tuple[str, dict]]:
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT status, metadata_json FROM update_events WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    return [(row["status"], json.loads(row["metadata_json"])) for row in rows]


def _fail_first_backup_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stack: str | None = None,
    before_failure: Callable[[], None] | None = None,
) -> None:
    original_delete = web_retag_apply._delete_path
    failed = False

    def delete_path(path: Path) -> None:
        nonlocal failed
        if not failed and (stack is None or stack in path.parts):
            failed = True
            if before_failure is not None:
                before_failure()
            raise PermissionError("backup cleanup denied")
        original_delete(path)

    monkeypatch.setattr(web_retag_apply, "_delete_path", delete_path)


@dataclass(frozen=True)
class _TwoStackFixture:
    client: TestClient
    alpha_dir: Path
    bravo_dir: Path
    db_path: Path
    choices: list[dict[str, str]]


def _make_two_stack_fixture(tmp_path: Path) -> _TwoStackFixture:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {"WUD_WEB_DEV_NO_AUTH": "true", **_LIVE_ENV, **fake_env},
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
    return _TwoStackFixture(
        client=client,
        alpha_dir=dirs["alpha"],
        bravo_dir=dirs["bravo"],
        db_path=tmp_path / "state" / "wud.sqlite",
        choices=[_switch_choice("alpha/app"), _switch_choice("bravo/app")],
    )


def _apply_two_stacks(fixture: _TwoStackFixture) -> dict[str, object]:
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers, fixture.choices)
    response = _apply_retag_plan(fixture.client, headers, plan, fixture.choices)
    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    _wait_run_status(fixture.db_path, job["run_id"], "failure")
    return job


def _events_by_stack(db_path: Path, run_id: object) -> dict[str, tuple[str, bool]]:
    with open_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT stack_name, status, metadata_json
            FROM update_events
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchall()
    return {
        row["stack_name"]: (
            row["status"],
            json.loads(row["metadata_json"])["known_image_recorded"],
        )
        for row in rows
    }


def test_retag_apply_restores_known_image_when_backup_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(tmp_path, env=_LIVE_ENV)
    db_path = tmp_path / "state" / "wud.sqlite"
    compose_file = fixture.compose_dir / "docker-compose.yml"
    compose_before = compose_file.read_bytes()
    known_before = _known_image_row(db_path, "stack/app")
    assert known_before is not None
    _fail_first_backup_cleanup(monkeypatch)
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)

    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "backup cleanup denied" in job["error"]
    _wait_run_status(db_path, job["run_id"], "failure")
    assert compose_file.read_bytes() == compose_before
    assert _known_image_row(db_path, "stack/app") == known_before
    events = _run_event_metadata(db_path, job["run_id"])
    assert [(status, meta["known_image_recorded"]) for status, meta in events] == [
        ("failure", False)
    ]


def test_retag_apply_keeps_known_image_when_compose_restore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(tmp_path, env=_LIVE_ENV)
    db_path = tmp_path / "state" / "wud.sqlite"
    compose_file = fixture.compose_dir / "docker-compose.yml"
    compose_before = compose_file.read_bytes()
    _fail_first_backup_cleanup(monkeypatch)

    def refuse_restore(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("restore denied")

    monkeypatch.setattr(web_retag_apply, "restore_compose_backup", refuse_restore)
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)

    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "compose rollback failed" in job["error"]
    _wait_run_status(db_path, job["run_id"], "failure")
    # Compose still declares the retagged image, so its record is retained.
    assert compose_file.read_bytes() != compose_before
    known = _known_image_row(db_path, "stack/app")
    assert known is not None
    assert known["image"] == "repo/app:2.0"
    events = _run_event_metadata(db_path, job["run_id"])
    assert [(status, meta["known_image_recorded"]) for status, meta in events] == [
        ("failure", True)
    ]


def test_retag_apply_reports_known_image_restore_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(tmp_path, env=_LIVE_ENV)
    db_path = tmp_path / "state" / "wud.sqlite"
    compose_file = fixture.compose_dir / "docker-compose.yml"
    compose_before = compose_file.read_bytes()
    _fail_first_backup_cleanup(monkeypatch)

    def refuse_known_image_restore(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        web_retag_audit,
        "_restore_retag_known_images",
        refuse_known_image_restore,
    )
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)

    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "backup cleanup denied" in job["error"]
    assert "saved image records could not be reset" in job["error"]
    assert "database is locked" in job["error"]
    _wait_run_status(db_path, job["run_id"], "failure")
    assert compose_file.read_bytes() == compose_before
    known = _known_image_row(db_path, "stack/app")
    assert known is not None
    assert known["image"] == "repo/app:2.0"
    events = _run_event_metadata(db_path, job["run_id"])
    assert [(status, meta["known_image_recorded"]) for status, meta in events] == [
        ("failure", True)
    ]


def test_retag_apply_restores_known_image_when_rollback_health_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(tmp_path, env=_LIVE_ENV)
    db_path = tmp_path / "state" / "wud.sqlite"
    compose_file = fixture.compose_dir / "docker-compose.yml"
    compose_before = compose_file.read_bytes()
    known_before = _known_image_row(db_path, "stack/app")

    def fail_rollback_up() -> None:
        (fixture.fake_root / "stacks" / "stack" / "up_fail").write_text(
            "up failed\n",
            encoding="utf-8",
        )

    _fail_first_backup_cleanup(monkeypatch, before_failure=fail_rollback_up)
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)

    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "compose rollback failed" in job["error"]
    _wait_run_status(db_path, job["run_id"], "failure")
    # The Compose file was restored even though its services did not recover.
    assert compose_file.read_bytes() == compose_before
    assert _known_image_row(db_path, "stack/app") == known_before
    events = _run_event_metadata(db_path, job["run_id"])
    assert [(status, meta["known_image_recorded"]) for status, meta in events] == [
        ("failure", False)
    ]


def test_retag_apply_reports_compose_changed_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(tmp_path, env=_LIVE_ENV)
    db_path = tmp_path / "state" / "wud.sqlite"
    compose_file = fixture.compose_dir / "docker-compose.yml"
    edited = b"# concurrent operator edit\n"

    def edit_compose() -> None:
        compose_file.write_bytes(edited + compose_file.read_bytes())

    _fail_first_backup_cleanup(monkeypatch, before_failure=edit_compose)
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)

    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "compose rollback failed" in job["error"]
    assert "changed by something else during the rollback" in job["error"]
    _wait_run_status(db_path, job["run_id"], "failure")
    assert compose_file.read_bytes().startswith(edited)
    known = _known_image_row(db_path, "stack/app")
    assert known is not None
    assert known["image"] == "repo/app:2.0"
    events = _run_event_metadata(db_path, job["run_id"])
    assert [(status, meta["known_image_recorded"]) for status, meta in events] == [
        ("failure", True)
    ]


def test_retag_apply_keeps_known_image_when_compose_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(tmp_path, env=_LIVE_ENV)
    db_path = tmp_path / "state" / "wud.sqlite"
    compose_file = fixture.compose_dir / "docker-compose.yml"
    compose_before = compose_file.read_bytes()
    _fail_first_backup_cleanup(monkeypatch)
    source_hash = web_retag_apply._compose_source_hash
    calls = 0

    def unreadable_after_backup(path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise PermissionError("compose read denied")
        return source_hash(path)

    monkeypatch.setattr(
        web_retag_apply, "_compose_source_hash", unreadable_after_backup
    )
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)

    response = _apply_retag_plan(fixture.client, headers, plan)

    assert response.status_code == 202
    job = _wait_apply_job(fixture.client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "could not be read to confirm the rollback" in job["error"]
    _wait_run_status(db_path, job["run_id"], "failure")
    # The rollback itself succeeded, but WUDup could not confirm it, so the
    # new records are kept and reported rather than reset on an assumption.
    assert "compose rollback failed" not in job["error"]
    assert compose_file.read_bytes() == compose_before
    known = _known_image_row(db_path, "stack/app")
    assert known is not None
    assert known["image"] == "repo/app:2.0"
    events = _run_event_metadata(db_path, job["run_id"])
    assert [(status, meta["known_image_recorded"]) for status, meta in events] == [
        ("failure", True)
    ]


def test_retag_apply_restores_only_failed_stack_known_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_two_stack_fixture(tmp_path)
    bravo_compose = fixture.bravo_dir / "docker-compose.yml"
    bravo_before = bravo_compose.read_bytes()
    bravo_known_before = _known_image_row(fixture.db_path, "bravo/app")
    _fail_first_backup_cleanup(monkeypatch, stack="bravo")

    job = _apply_two_stacks(fixture)

    assert job["status"] == "failure"
    assert "backup cleanup denied" in job["error"]
    assert "wud.tag.include=^2\\.0$$" in (
        fixture.alpha_dir / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    assert bravo_compose.read_bytes() == bravo_before
    alpha_known = _known_image_row(fixture.db_path, "alpha/app")
    assert alpha_known is not None
    assert alpha_known["digest_provenance_source"] == "retag"
    assert _known_image_row(fixture.db_path, "bravo/app") == bravo_known_before
    assert _events_by_stack(fixture.db_path, job["run_id"]) == {
        "alpha": ("success", True),
        "bravo": ("failure", False),
    }


def test_retag_apply_keeps_earlier_stack_success_when_unused_backup_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_two_stack_fixture(tmp_path)
    bravo_compose = fixture.bravo_dir / "docker-compose.yml"
    bravo_before = bravo_compose.read_bytes()
    rewrite = web_retag_apply.apply_compose_digest_pins

    def fail_bravo_rewrite(*args: object, **kwargs: object) -> object:
        if kwargs.get("stack_name") == "bravo":
            raise RuntimeError("bravo rewrite failed")
        return rewrite(*args, **kwargs)

    monkeypatch.setattr(
        web_retag_apply, "apply_compose_digest_pins", fail_bravo_rewrite
    )
    _fail_first_backup_cleanup(monkeypatch, stack="bravo")
    apply_updates = web_retag_apply._apply_retag_updates
    errors: list[Exception] = []

    def capture_apply_error(*args: object, **kwargs: object) -> object:
        try:
            return apply_updates(*args, **kwargs)
        except Exception as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(web_retag_apply, "_apply_retag_updates", capture_apply_error)

    job = _apply_two_stacks(fixture)

    assert job["status"] == "failure"
    assert "bravo rewrite failed" in job["error"]
    assert (
        "unused Compose backup could not be removed and was retained at "
        "[REDACTED_PATH]"
    ) in job["error"]
    assert str(tmp_path) not in job["error"]
    assert bravo_compose.read_bytes() == bravo_before
    assert list(fixture.bravo_dir.glob(".docker-compose.yml.backup.*"))
    [error] = errors
    assert isinstance(error, web_retag_apply._RetagApplyFailed)
    assert [item.service_key for item in error.successful_updates] == ["alpha/app"]
    assert isinstance(error.__cause__, PermissionError)
    assert isinstance(error.__cause__.__context__, RuntimeError)
    assert str(error.__cause__.__context__) == "bravo rewrite failed"
    alpha_known = _known_image_row(fixture.db_path, "alpha/app")
    assert alpha_known is not None
    assert alpha_known["digest_provenance_source"] == "retag"
    assert _events_by_stack(fixture.db_path, job["run_id"]) == {
        "alpha": ("success", True),
        "bravo": ("failure", False),
    }


def _retag_update(tmp_path: Path, service: str) -> RetagPlanUpdate:
    image = f"repo/{service}@sha256:old"
    stack = ComposeStack(
        index=1,
        directory=tmp_path / "docker" / "stack",
        file="docker-compose.yml",
        name="stack",
        images=(image,),
        service_images=(ServiceImage(service, image),),
    )
    return RetagPlanUpdate(
        target_id=f"target-{service}",
        service_key=f"stack/{service}",
        stack=stack,
        update=digest_pin_update_from_values(
            old_image=image,
            resolved_tag="2.0",
            planned_digest="sha256:new",
            services=(service,),
        ),
        provenance=DigestTagProvenance(
            source_image=f"repo/{service}:latest",
            resolved_tag="2.0",
            watch_tag="latest",
            target_digest="sha256:old",
            final_image=image,
            provenance_source="apply",
            provenance_confidence="verified",
        ),
    )


def _seed(tmp_path: Path, service: str) -> None:
    _seed_known_image(
        tmp_path,
        service_key=f"stack/{service}",
        image=f"repo/{service}@sha256:old",
        source_image=f"repo/{service}:latest",
        resolved_tag="1.0",
        watch_tag="latest",
        target_digest="sha256:old",
        final_image=f"repo/{service}@sha256:old",
    )


def test_retag_known_image_recording_is_atomic_per_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _audit_settings(tmp_path)
    _seed(tmp_path, "app")
    db_path = settings.config.db_path
    app_before = _known_image_row(db_path, "stack/app")
    calls = 0

    def fail_second_upsert(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("database write failed")
        upsert_known_image(*args, **kwargs)

    monkeypatch.setattr(web_retag_audit, "upsert_known_image", fail_second_upsert)

    with pytest.raises(RuntimeError, match="database write failed"):
        web_retag_audit._record_successful_retag_known_images(
            settings,
            [_retag_update(tmp_path, "app"), _retag_update(tmp_path, "worker")],
        )

    assert calls == 2
    assert _known_image_row(db_path, "stack/app") == app_before
    assert _known_image_row(db_path, "stack/worker") is None


def test_retag_known_image_restore_reverts_only_unchanged_records(
    tmp_path: Path,
) -> None:
    settings = _audit_settings(tmp_path)
    for service in ("app", "api"):
        _seed(tmp_path, service)
    db_path = settings.config.db_path
    before = {
        key: _known_image_row(db_path, key) for key in ("stack/app", "stack/api")
    }

    changes = web_retag_audit._record_successful_retag_known_images(
        settings,
        [
            _retag_update(tmp_path, service)
            for service in ("app", "api", "worker")
        ],
    )
    for key in ("stack/app", "stack/api", "stack/worker"):
        assert _known_image_row(db_path, key)["image"].endswith("sha256:new")
    with open_db(db_path) as conn:
        init_db(conn)
        upsert_known_image(conn, service_key="stack/api", image="repo/api:3.0")

    web_retag_audit._restore_retag_known_images(settings, changes)

    assert _known_image_row(db_path, "stack/app") == before["stack/app"]
    assert _known_image_row(db_path, "stack/api")["image"] == "repo/api:3.0"
    assert _known_image_row(db_path, "stack/worker") is None
