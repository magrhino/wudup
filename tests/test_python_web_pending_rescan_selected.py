from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from urllib.error import HTTPError

import pytest
from tests.web_test_helpers import _client, _csrf_headers, _install_wud_api
from tests.web_wud_rescan_helpers import (
    container_payload,
    install_recording_wud_api,
    rescan_lines_from_pending,
    rescan_payload,
)

from wudup import web_wud_transport
from wudup.db import open_db
from wudup.web_models import WebApplyJob


def test_pending_selected_rescan_maps_lines_to_wud_container_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = container_payload(name="app")
    app["id"] = "docker/local app"
    calls = install_recording_wud_api(monkeypatch, [app])
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_API_BASE_URL": "https://wud.rescan-selected.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    original = "app\nunknown\n"
    wud_file.write_text(original, encoding="utf-8")
    pending_body = client.get("/api/v1/pending").json()
    lines = rescan_lines_from_pending(pending_body, [1, 2])
    calls.clear()

    response = client.post(
        "/api/v1/pending/rescan",
        json=rescan_payload("selected", [1, 2], lines),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["scope"] == "selected"
    assert body["requested_count"] == 2
    assert body["watched_count"] == 1
    assert body["skipped"] == [
        {"line_no": 2, "raw": "unknown", "reason": "no-wud-container-id"}
    ]
    assert ("POST", "/api/containers/docker%2Flocal%20app/watch") in calls
    assert ("POST", "/api/containers/watch") not in calls
    assert wud_file.read_text(encoding="utf-8") == original


def test_pending_selected_rescan_api_source_watches_all_deduped_container_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = install_recording_wud_api(
        monkeypatch,
        [
            container_payload(name="app", image="repo/app"),
            container_payload(name="worker", image="repo/app"),
        ],
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.rescan-api-deduped.test:3000",
        },
    )
    pending_body = client.get("/api/v1/pending").json()
    assert pending_body["count"] == 1
    lines = rescan_lines_from_pending(pending_body, [1])
    assert lines[0]["source_id"] == "docker.local.app,docker.local.worker"
    calls.clear()

    response = client.post(
        "/api/v1/pending/rescan",
        json=rescan_payload("selected", [1], lines),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["requested_count"] == 1
    assert body["watched_count"] == 2
    assert [path for method, path in calls if method == "POST"] == [
        "/api/containers/docker.local.app/watch",
        "/api/containers/docker.local.worker/watch",
    ]


def test_pending_selected_rescan_targets_cold_start_recovered_container(
    tmp_path: Path,
    monkeypatch,
) -> None:
    degraded = container_payload(name="app")
    degraded["updateAvailable"] = False
    degraded["result"] = None
    degraded["error"] = {"message": "registry lookup failed"}
    calls = install_recording_wud_api(monkeypatch, [degraded])
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "images.todo").write_text(
        "registry.example/acme/app:1.0.0 tag=1.1.0\n",
        encoding="utf-8",
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.rescan-recovered.test:3000",
        },
    )
    pending_body = client.get("/api/v1/pending").json()
    assert pending_body["count"] == 1
    assert pending_body["source"]["degraded"] is True
    lines = rescan_lines_from_pending(pending_body, [1])
    assert lines[0]["container_id"] == "docker.local.app"
    calls.clear()

    response = client.post(
        "/api/v1/pending/rescan",
        json=rescan_payload("selected", [1], lines),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["requested_count"] == 1
    assert body["watched_count"] == 1
    assert [path for method, path in calls if method == "POST"] == [
        "/api/containers/docker.local.app/watch"
    ]
    assert ("POST", "/api/containers/watch") not in calls


def test_pending_selected_rescan_rejects_stale_source_without_watch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = install_recording_wud_api(monkeypatch, [container_payload(name="app")])
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_API_BASE_URL": "https://wud.rescan-stale.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("app\n", encoding="utf-8")
    pending_body = client.get("/api/v1/pending").json()
    lines = rescan_lines_from_pending(pending_body, [1])
    wud_file.write_text("other\n", encoding="utf-8")
    calls.clear()

    response = client.post(
        "/api/v1/pending/rescan",
        json=rescan_payload("selected", [1], lines),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "selected rescan is stale"
    assert [call for call in calls if call[0] == "POST"] == []


def test_pending_selected_rescan_reports_partial_watch_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = install_recording_wud_api(
        monkeypatch,
        [
            container_payload(name="app", image="app"),
            container_payload(name="radarr", image="radarr"),
        ],
    )

    def post_json(url: str, _client_config=None, **_kwargs) -> object:
        path = urllib.parse.urlsplit(url).path
        calls.append(("POST", path))
        if path == "/api/containers/docker.local.radarr/watch":
            raise OSError("timeout")
        return {"status": "ok"}

    monkeypatch.setattr(web_wud_transport, "_post_json", post_json)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_API_BASE_URL": "https://wud.rescan-partial-watch.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("app\nradarr\n", encoding="utf-8")
    pending_body = client.get("/api/v1/pending").json()
    lines = rescan_lines_from_pending(pending_body, [1, 2])
    calls.clear()

    response = client.post(
        "/api/v1/pending/rescan",
        json=rescan_payload("selected", [1, 2], lines),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["watched_count"] == 1
    assert ("POST", "/api/containers/docker.local.app/watch") in calls
    assert ("POST", "/api/containers/docker.local.radarr/watch") in calls
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        run = conn.execute(
            "SELECT * FROM update_runs WHERE id = ?",
            (body["audit_run_id"],),
        ).fetchone()
    assert run["status"] == "success"
    metadata = json.loads(run["metadata_json"])
    assert metadata["status"] == "partial"
    assert metadata["watched_count"] == 1


def test_pending_selected_rescan_skips_unmapped_lines_without_global_watch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = install_recording_wud_api(monkeypatch, [container_payload(name="app")])
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_API_BASE_URL": "https://wud.rescan-unmapped.test:3000",
        },
    )
    wud_file = tmp_path / "state" / "images.todo"
    original = "unknown\n"
    wud_file.write_text(original, encoding="utf-8")
    pending_body = client.get("/api/v1/pending").json()
    lines = rescan_lines_from_pending(pending_body, [1])
    calls.clear()

    response = client.post(
        "/api/v1/pending/rescan",
        json=rescan_payload("selected", [1], lines),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["watched_count"] == 0
    assert body["skipped"] == [
        {"line_no": 1, "raw": "unknown", "reason": "no-wud-container-id"}
    ]
    assert [call for call in calls if call[0] == "POST"] == []
    assert wud_file.read_text(encoding="utf-8") == original


def test_pending_rescan_rejects_active_apply_job_without_watch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    posts: list[str] = []
    monkeypatch.setattr(
        web_wud_transport,
        "_request_json",
        lambda url, _client_config=None: {"status": "ok"},
    )
    monkeypatch.setattr(
        web_wud_transport,
        "_post_json",
        lambda url, _client_config=None, **_kwargs: posts.append(
            urllib.parse.urlsplit(url).path
        ),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_API_BASE_URL": "https://wud.rescan-active-job.test:3000",
        },
    )
    client.app.state.web_apply_jobs["job-active"] = WebApplyJob(
        id="job-active",
        status="running",
        selected_line_numbers=(1,),
    )

    response = client.post(
        "/api/v1/pending/rescan",
        json=rescan_payload(),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "an apply job is already running"
    assert posts == []


@pytest.mark.parametrize(
    ("missing_count", "watched_count", "missing_detail"),
    (
        (1, 2, "WUD skipped 1 container that no longer exists"),
        (2, 1, "WUD skipped 2 containers that no longer exist"),
    ),
)
def test_pending_selected_rescan_continues_after_missing_containers_and_audits_partial(
    tmp_path: Path,
    monkeypatch,
    missing_count: int,
    watched_count: int,
    missing_detail: str,
) -> None:
    _install_wud_api(
        monkeypatch,
        containers=[
            container_payload(name="missing-one"),
            container_payload(name="missing-two"),
            container_payload(name="app"),
        ],
    )
    posts: list[str] = []

    def post_json(url: str, _client_config=None, **_kwargs) -> object:
        path = urllib.parse.urlsplit(url).path
        posts.append(path)
        if len(posts) <= missing_count:
            raise HTTPError(url=url, code=404, msg="Not Found", hdrs=None, fp=None)
        return {"status": "ok"}

    monkeypatch.setattr(web_wud_transport, "_post_json", post_json)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_API_BASE_URL": "https://wud.rescan-missing.test:3000",
        },
    )

    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("missing-one\nmissing-two\napp\n", encoding="utf-8")
    lines = rescan_lines_from_pending(client.get("/api/v1/pending").json(), [1, 2, 3])

    response = client.post(
        "/api/v1/pending/rescan",
        json=rescan_payload("selected", [1, 2, 3], lines),
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["requested_count"] == 3
    assert body["watched_count"] == watched_count
    assert missing_detail in body["wud_api"]["detail"]
    assert len(posts) == 3
    assert set(posts) == {
        "/api/containers/docker.local.missing-one/watch",
        "/api/containers/docker.local.missing-two/watch",
        "/api/containers/docker.local.app/watch",
    }
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        run = conn.execute(
            "SELECT * FROM update_runs WHERE id = ?",
            (body["audit_run_id"],),
        ).fetchone()
    metadata = json.loads(run["metadata_json"])
    assert run["status"] == "success"
    assert metadata["status"] == "partial"
    assert metadata["requested_count"] == 3
    assert metadata["watched_count"] == watched_count
