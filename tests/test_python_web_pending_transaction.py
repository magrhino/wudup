from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_env,
    _make_fake_stack,
)

from wudup import web_jobs, web_pending
from wudup.file_ops import OwnerConfig
from wudup.locks import DirectoryLock, lock_dir_for
from wudup.web_models import WebSettings
from wudup.wud_file import ParsedWudFile


def _prepare_pending_mutation(tmp_path: Path, operation: str):
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_LOCK_TIMEOUT": "0",
            **fake_env,
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text(
        "# header\n\nrepo/old:latest\n# middle\nrepo/old:latest\nrepo/app:latest\n",
        encoding="utf-8",
    )
    _make_fake_stack(
        tmp_path, fake_root, "stack", [("app", "repo/app:latest", "cid-app")],
    )
    headers = _csrf_headers(client)
    plan_url = "/api/v1/plans" if operation == "cleanup" else "/api/v1/pending/removal-plan"
    plan_response = client.post(plan_url, json={"line_numbers": [3]}, headers=headers)
    assert plan_response.status_code == 200
    plan = plan_response.json()
    if operation == "cleanup":
        plan = plan["cleanup"]
    payload = {
        f"{operation}_id": plan[f"{operation}_id"],
        "lines": [{"line_no": 3, "raw": "repo/old:latest"}],
        "confirmation": "remove_unmatched" if operation == "cleanup" else "remove_selected",
    }
    with web_pending.open_db(tmp_path / "state" / "wud.sqlite") as conn:
        web_pending.init_db(conn)
    return client, headers, payload, wud_file


@pytest.mark.parametrize("operation", ["cleanup", "removal"])
@pytest.mark.parametrize("failure", [None, "audit", "file"])
def test_pending_mutation_orders_locked_audit_file_write_and_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: str | None,
) -> None:
    client, headers, payload, wud_file = _prepare_pending_mutation(tmp_path, operation)
    original_text = wud_file.read_text(encoding="utf-8")
    db_path = tmp_path / "state" / "wud.sqlite"
    events: list[str] = []
    audit_connections: list[sqlite3.Connection] = []
    locks: list[DirectoryLock] = []
    owners: list[OwnerConfig] = []
    acquire = web_jobs._acquire_apply_wud_lock
    init_db = web_pending.init_db
    audit_name = f"_insert_pending_{operation}_audit"
    insert_audit = getattr(web_pending, audit_name)
    remove_lines = web_pending.remove_lines_before_run

    def acquire_lock(settings: WebSettings) -> DirectoryLock:
        lock = acquire(settings)
        locks.append(lock)
        owners.append(OwnerConfig(uid=settings.config.out_uid, gid=settings.config.out_gid))
        events.append("lock")
        close = lock.close

        def close_lock() -> None:
            events.append("unlock")
            close()

        monkeypatch.setattr(lock, "close", close_lock)
        return lock

    def initialize(conn: sqlite3.Connection) -> None:
        init_db(conn)
        events.append("init")
        conn.set_trace_callback(
            lambda sql: events.append(sql) if sql in {"BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"} else None
        )

    def audited(conn: sqlite3.Connection, *args: object) -> int:
        assert conn.in_transaction
        assert locks[0].held and lock_dir_for(wud_file).is_dir()
        audit_connections.append(conn)
        run_id = insert_audit(conn, *args)
        events.append("audit")
        for table in ("pending_updates", "update_events"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)).fetchone()[0] == 1
        if failure == "audit":
            raise sqlite3.OperationalError(f"audit failed at {db_path}")
        return run_id

    def remove(path: Path, parsed: ParsedWudFile, selected: list[int], **kwargs: object) -> bool:
        events.append("file")
        assert audit_connections[0].in_transaction
        assert selected == [3]
        assert kwargs["lock"] is locks[0] and locks[0].held
        assert kwargs["owner"] == owners[0]
        with closing(sqlite3.connect(db_path)) as external:
            assert external.execute(
                "SELECT COUNT(*) FROM update_runs WHERE mode = ?", (f"web-pending-{operation}",),
            ).fetchone()[0] == 0
        if failure == "file":
            raise OSError(f"write failed at {wud_file}")
        # Preserve an unselected duplicate and extras appended after parsing.
        with wud_file.open("a", encoding="utf-8") as stream:
            stream.write("repo/old:latest\nrepo/new:latest\n")
        return remove_lines(path, parsed, selected, **kwargs)

    monkeypatch.setattr(web_jobs, "_acquire_apply_wud_lock", acquire_lock)
    monkeypatch.setattr(web_pending, "init_db", initialize)
    monkeypatch.setattr(web_pending, audit_name, audited)
    monkeypatch.setattr(web_pending, "remove_lines_before_run", remove)

    response = client.post(f"/api/v1/pending/{operation}", json=payload, headers=headers)

    expected = ["lock", "init", "BEGIN IMMEDIATE", "audit"]
    if failure != "audit":
        expected.append("file")
    expected.extend(["ROLLBACK" if failure else "COMMIT", "unlock"])
    assert events == expected
    assert len(locks) == 1 and not locks[0].held
    assert not lock_dir_for(wud_file).exists()
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM update_runs WHERE mode = ?", (f"web-pending-{operation}",),
        ).fetchone()[0] == (0 if failure else 1)
    if failure:
        assert response.status_code == 500
        message = "could not remove pending lines" if failure == "file" else f"could not record {operation} audit"
        assert message in response.json()["detail"]
        assert str(tmp_path) not in response.json()["detail"]
        assert wud_file.read_text(encoding="utf-8") == original_text
    else:
        assert response.status_code == 200
        assert response.json()["removed"] == [{
            "line_no": 3, "raw": "repo/old:latest", "image": "repo/old:latest",
            "reason": "unmatched" if operation == "cleanup" else "selected",
        }]
        assert wud_file.read_text(encoding="utf-8") == (
            "# header\n\n# middle\nrepo/old:latest\nrepo/app:latest\nrepo/old:latest\nrepo/new:latest\n"
        )


@pytest.mark.parametrize("operation", ["cleanup", "removal"])
def test_pending_mutation_lock_timeout_preserves_existing_lock_and_file(
    tmp_path: Path,
    operation: str,
) -> None:
    client, headers, payload, wud_file = _prepare_pending_mutation(tmp_path, operation)
    original_text = wud_file.read_text(encoding="utf-8")
    with DirectoryLock(wud_file, timeout_seconds=0) as lock:
        response = client.post(f"/api/v1/pending/{operation}", json=payload, headers=headers)
        assert response.status_code == 409
        assert response.json()["detail"] == "WUD file is locked"
        assert lock.held and lock_dir_for(wud_file).is_dir()
        assert wud_file.read_text(encoding="utf-8") == original_text
    assert not lock_dir_for(wud_file).exists()
