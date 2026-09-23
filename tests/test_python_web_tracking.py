from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _fake_image_state_file,
    _install_wud_api,
    _make_fake_stack,
    _wait_apply_job,
)

from wudup import (
    compose_persistence,
    compose_rewrite,
    web_retags,
    web_tracking,
    web_wud_api,
)
from wudup.compose_rewrite import apply_compose_tracking_label
from wudup.db import init_db, insert_update_run, open_db
from wudup.updater_models import ComposeTagRewriteError
from wudup.web_models import TrackingRepairPlan, WudApiStatus
from wudup.web_wud_api import WudApiSnapshot


def _tracking_fixture(
    tmp_path: Path, *, mutations: bool = True,
    tag: str = "v1.36.2", regex: str = r"^v1\.36\.2$",
):
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true" if mutations else "false",
            "WUD_MAX_WAIT": "0",
            **fake_env,
        },
    )
    compose_dir = _make_fake_stack(
        tmp_path, fake_root, "bindery", [("bindery", f"repo/bindery:{tag}", "cid-bindery")]
    )
    compose_path = compose_dir / "docker-compose.yml"
    compose_path.write_text(
        f"services:\n  bindery:\n    image: repo/bindery:{tag}\n"
        + (f"    labels:\n      - wud.tag.include={regex.replace('$', '$$')}\n" if regex else ""),
        encoding="utf-8",
    )
    image_id = "sha256:" + "a" * 64
    (fake_root / "containers" / "cid-bindery.image-id").write_text(image_id, encoding="utf-8")
    _fake_image_state_file(fake_root, f"repo/bindery:{tag}", "id").write_text(
        image_id, encoding="utf-8"
    )
    return client, fake_root, compose_path


def test_inventory_includes_compose_service_without_wud(
    tmp_path: Path, monkeypatch
) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    monkeypatch.setattr(
        web_wud_api,
        "get_snapshot",
        lambda *_args, **_kwargs: WudApiSnapshot(
            status=WudApiStatus(
                state="ready", available=True, metadata_available=True,
                last_checked_at="2026-09-21T00:00:00Z",
            )
        ),
    )

    response = client.get("/api/v1/tracked-containers")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["service_key"] == "bindery/bindery"
    assert item["compose_path"] == str(compose_path)
    assert item["wud"] is None
    assert item["wud_match_state"] == "untracked"
    assert item["tracking_health"] == "frozen"
    assert item["suggested_regex"] == r"^v\d+(?:\.\d+)+$"


@pytest.mark.parametrize(
    ("tag", "regex", "health", "suggested"),
    [
        ("latest", r"^latest$", "exact-tag", ""),
        ("latest-trivy", r"^latest-trivy$", "exact-tag", ""),
        ("stable", r"^stable$", "exact-tag", ""),
        ("16", r"^16$", "exact-tag", ""),
        ("16-alpine", r"^16-alpine$", "exact-tag", ""),
        ("v1.36.2", r"^v1\.36\.2$", "frozen", r"^v\d+(?:\.\d+)+$"),
        ("2.33.5-distroless", r"^2\.33\.5-distroless$", "frozen", r"^\d+\.\d+\.\d+-distroless$"),
        ("2.7-alpine", r"^2\.7-alpine$", "frozen", r"^2(?:\.\d+)+-alpine$"),
        ("10.11.11ubu2604-ls43", r"^10\.11\.11ubu2604-ls43$", "frozen", r"^\d+\.\d+\.\d+ubu\d+-ls\d+$"),
    ],
)
def test_inventory_distinguishes_exact_channels_from_version_releases(
    tmp_path: Path, tag: str, regex: str, health: str, suggested: str,
) -> None:
    client, _fake_root, _compose_path = _tracking_fixture(tmp_path, tag=tag, regex=regex)

    item = client.get("/api/v1/tracked-containers").json()["items"][0]

    assert item["tracking_health"] == health
    assert item["suggested_regex"] == suggested


def test_suggested_regex_rejects_oversized_compose_tag_before_release_match() -> None:
    tag = "v1." + "1." * 10_000 + "invalid!"

    assert web_tracking._suggested_regex(tag, f"repo/app:{tag}") == ""


def test_alpine_repair_preview_accepts_major_pinned_suggestion(tmp_path: Path) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path, tag="2.7-alpine", regex="2.7-alpine")
    original = compose_path.read_bytes()
    item = client.get("/api/v1/tracked-containers").json()["items"][0]
    assert item["suggested_regex"] == r"^2(?:\.\d+)+-alpine$"
    response = client.post(
        "/api/v1/tracking-repairs",
        json={"target_id": item["target_id"], "regex": item["suggested_regex"]},
        headers=_csrf_headers(client),
    )
    assert response.status_code == 200
    assert response.json()["can_apply"] is True
    assert compose_path.read_bytes() == original


@pytest.mark.parametrize(
    ("tag", "release_shaped"),
    [("v1.2", True), ("1.2.3", True), ("v1.2.3rc", True),
     ("v1.2.3..foo", True), ("v1.2._foo", False), ("latest", False)],
)
def test_release_shape_keeps_dotted_suffix_rules(tag: str, release_shaped: bool) -> None:
    assert web_tracking._release_shaped_tag(tag) is release_shaped


def test_repair_previews_exact_mutable_tag_without_a_numeric_wildcard(tmp_path: Path) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path, tag="latest", regex="")
    original = compose_path.read_text(encoding="utf-8")
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]

    response = client.post(
        "/api/v1/tracking-repairs",
        json={"target_id": target, "regex": r"^latest$"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["can_apply"] is True
    assert compose_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("current", "expected_old"),
    [
        ("", "(not set)"),
        ("(not set)", "(set) (not set)"),
        ("^v1'foo$", "(set) ^v1'foo$"),
        ("^v1\u2028.36.2$", r"(set) '^v1\u2028.36.2$'"),
    ],
)
def test_repair_preview_distinguishes_missing_label_and_escapes_line_separator(
    tmp_path: Path, current: str, expected_old: str,
) -> None:
    client, _fake_root, _compose_path = _tracking_fixture(tmp_path, regex=current)
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]

    response = client.post(
        "/api/v1/tracking-repairs",
        json={"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["compose_diff"].splitlines() == [
        "wud.tag.include:", f"- {expected_old}", r"+ (set) ^v\d+(?:\.\d+)+$",
    ]


def test_repair_preview_never_returns_other_compose_fields(tmp_path: Path) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path, mutations=False)
    compose_path.write_text(
        compose_path.read_text(encoding="utf-8").replace(
            "    labels:\n",
            '    environment:\n      API_TOKEN: "example-secret-do-not-expose"\n    labels:\n',
        ),
        encoding="utf-8",
    )
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]

    response = client.post(
        "/api/v1/tracking-repairs",
        json={"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert "wud.tag.include" in response.json()["compose_diff"]
    assert "API_TOKEN" not in response.text
    assert "example-secret-do-not-expose" not in response.text


def test_inventory_includes_wud_watched_service_without_update(
    tmp_path: Path, monkeypatch
) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    _install_wud_api(
        monkeypatch,
        containers=[{
            "id": "docker.local.bindery",
            "name": "bindery",
            "displayName": "Bindery",
            "status": "running",
            "watcher": "local",
            "image": {"name": "repo/bindery", "tag": {"value": "v1.36.2"}, "digest": {"value": "sha256:old"}},
            "result": {"tag": "v1.36.2"},
            "labels": {
                "com.docker.compose.project": "bindery",
                "com.docker.compose.service": "bindery",
                "com.docker.compose.project.working_dir": str(compose_path.parent),
                "com.docker.compose.project.config_files": str(compose_path),
            },
            "updateAvailable": False,
        }],
    )

    response = client.get("/api/v1/tracked-containers")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["wud"]["name"] == "bindery"
    assert item["wud_match_state"] == "watching"
    assert item["wud_update_available"] is False


def test_inventory_does_not_call_missing_wud_labels_untracked(
    tmp_path: Path, monkeypatch
) -> None:
    client, _fake_root, _compose_path = _tracking_fixture(tmp_path)
    _install_wud_api(
        monkeypatch,
        containers=[{
            "id": "docker.local.bindery",
            "name": "bindery",
            "status": "running",
            "watcher": "local",
            "image": {"name": "repo/bindery", "tag": {"value": "v1.36.2"}},
            "result": {"tag": "v1.36.2"},
            "updateAvailable": False,
        }],
    )

    item = client.get("/api/v1/tracked-containers").json()["items"][0]

    assert item["wud_match_state"] == "unknown"
    assert item["wud"] is None


def test_inventory_marks_degraded_wud_update_state_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    _install_wud_api(
        monkeypatch,
        containers=[{
            "id": "docker.local.bindery",
            "name": "bindery",
            "status": "running",
            "watcher": "local",
            "image": {"name": "repo/bindery", "tag": {"value": "v1.36.2"}},
            "result": None,
            "error": {"message": "registry lookup failed"},
            "labels": {
                "com.docker.compose.project": "bindery",
                "com.docker.compose.service": "bindery",
                "com.docker.compose.project.working_dir": str(compose_path.parent),
                "com.docker.compose.project.config_files": str(compose_path),
            },
            "updateAvailable": False,
        }],
    )

    item = client.get("/api/v1/tracked-containers").json()["items"][0]

    assert item["wud_match_state"] == "watching"
    assert item["wud_update_available"] is None


def test_inventory_does_not_match_same_named_foreign_compose_file(
    tmp_path: Path, monkeypatch
) -> None:
    client, _fake_root, _compose_path = _tracking_fixture(tmp_path)
    _install_wud_api(
        monkeypatch,
        containers=[{
            "id": "docker.other.bindery",
            "name": "bindery",
            "status": "running",
            "watcher": "other",
            "image": {"name": "repo/bindery", "tag": {"value": "v1.36.2"}},
            "result": {"tag": "v1.36.2"},
            "labels": {
                "com.docker.compose.project": "bindery",
                "com.docker.compose.service": "bindery",
                "com.docker.compose.project.working_dir": "/other",
                "com.docker.compose.project.config_files": "/other/compose.yml",
            },
            "updateAvailable": False,
        }],
    )

    item = client.get("/api/v1/tracked-containers").json()["items"][0]

    assert item["wud_match_state"] == "unknown"
    assert item["wud"] is None


def test_inventory_matches_exact_compose_path_despite_duplicate_project_service(
    tmp_path: Path, monkeypatch
) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    discover = web_retags._discover_retag_stacks

    def duplicate_identity(settings):
        stacks = discover(settings)
        assert isinstance(stacks, tuple)
        original = stacks[0]
        other_directory = tmp_path / "other"
        other_directory.mkdir()
        return original, replace(
            original, directory=other_directory, project_directory=other_directory,
        )

    monkeypatch.setattr(web_retags, "_discover_retag_stacks", duplicate_identity)
    _install_wud_api(
        monkeypatch,
        containers=[{
            "id": "docker.local.bindery", "name": "bindery", "status": "running",
            "watcher": "local",
            "image": {"name": "repo/bindery", "tag": {"value": "v1.36.2"}},
            "result": {"tag": "v1.36.2"},
            "labels": {
                "com.docker.compose.project": "bindery",
                "com.docker.compose.service": "bindery",
                "com.docker.compose.project.working_dir": str(compose_path.parent),
                "com.docker.compose.project.config_files": str(compose_path),
            },
            "updateAvailable": False,
        }],
    )

    items = client.get("/api/v1/tracked-containers").json()["items"]

    assert len(items) == 2
    assert len([item for item in items if item["wud_match_state"] == "watching"]) == 1
    assert len([item for item in items if item["wud_match_state"] == "unknown"]) == 1


def test_inventory_includes_service_when_compose_json_falls_back(
    tmp_path: Path
) -> None:
    client, fake_root, compose_path = _tracking_fixture(tmp_path)
    (fake_root / "stacks" / "bindery" / "config_json_fail").touch()

    response = client.get("/api/v1/tracked-containers")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    item = response.json()["items"][0]
    assert item["service_key"] == "bindery/bindery"
    assert item["compose_path"] == str(compose_path)
    assert item["tracking_health"] == "image-unresolved"
    assert item["wud_match_state"] == "unknown"


def test_inventory_uses_compose_source_when_service_listing_fails(
    tmp_path: Path
) -> None:
    client, fake_root, _compose_path = _tracking_fixture(tmp_path)
    (fake_root / "stacks" / "bindery" / "config_json_fail").touch()
    (fake_root / "stacks" / "bindery" / "config_services_fail").touch()

    response = client.get("/api/v1/tracked-containers")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["tracking_health"] == "image-unresolved"


def test_inventory_includes_build_only_service(
    tmp_path: Path, monkeypatch
) -> None:
    client, _fake_root, _compose_path = _tracking_fixture(tmp_path)
    discover = web_retags._discover_retag_stacks

    def with_build_only(settings):
        stacks = discover(settings)
        assert isinstance(stacks, tuple)
        return tuple(replace(stack, service_names=(*stack.service_names, "worker")) for stack in stacks)

    monkeypatch.setattr(web_retags, "_discover_retag_stacks", with_build_only)

    response = client.get("/api/v1/tracked-containers")

    assert response.status_code == 200
    assert response.json()["count"] == 2
    worker = next(item for item in response.json()["items"] if item["service"] == "worker")
    assert worker["tracking_health"] == "no-image"
    assert worker["wud_match_state"] == "unknown"


def test_repair_plan_apply_recreates_only_selected_service(tmp_path: Path) -> None:
    client, fake_root, compose_path = _tracking_fixture(tmp_path)
    original = compose_path.read_text(encoding="utf-8")
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    headers = _csrf_headers(client)
    payload = {"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"}

    preview = client.post("/api/v1/tracking-repairs", json=payload, headers=headers)

    assert preview.status_code == 200
    plan = preview.json()
    assert plan["can_apply"] is True
    assert plan["compose_diff"].splitlines() == [
        "wud.tag.include:", r"- (set) ^v1\.36\.2$", r"+ (set) ^v\d+(?:\.\d+)+$",
    ]
    assert compose_path.read_text(encoding="utf-8") == original

    apply = client.post(
        "/api/v1/tracking-repairs/apply",
        json={**payload, "plan_id": plan["plan_id"], "confirmation": "apply-tracking-repair"},
        headers=headers,
    )
    assert apply.status_code == 202
    job = _wait_apply_job(client, apply.json()["job_id"])
    assert job["status"] == "success", job["error"]
    assert r"wud.tag.include=^v\d+(?:\.\d+)+$$" in compose_path.read_text(encoding="utf-8")
    calls = _fake_docker_calls(fake_root)
    assert "compose -f docker-compose.yml up -d --pull never --no-build --force-recreate --no-deps --wait --wait-timeout 0 bindery" in calls
    assert "--remove-orphans" not in calls
    assert "compose -f docker-compose.yml pull bindery" not in calls
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        run = conn.execute("SELECT mode, status FROM update_runs WHERE id = ?", (job["run_id"],)).fetchone()
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(update_events)")}
    assert (run["mode"], run["status"]) == ("web-tracking-repair", "success")
    assert "idx_update_events_service_latest" in indexes
    inventory = client.get("/api/v1/tracked-containers").json()["items"][0]
    assert inventory["last_action_run_id"] == job["run_id"]


def test_repair_applies_to_crlf_compose_file(tmp_path: Path) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    compose_path.write_bytes(compose_path.read_bytes().replace(b"\n", b"\r\n"))
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    headers = _csrf_headers(client)
    payload = {"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"}

    preview = client.post("/api/v1/tracking-repairs", json=payload, headers=headers)
    assert preview.status_code == 200
    assert preview.json()["can_apply"] is True
    response = client.post(
        "/api/v1/tracking-repairs/apply",
        json={**payload, "plan_id": preview.json()["plan_id"], "confirmation": "apply-tracking-repair"},
        headers=headers,
    )

    assert response.status_code == 202
    job = _wait_apply_job(client, response.json()["job_id"])
    assert job["status"] == "success", job["error"]
    assert rb"wud.tag.include=^v\d+(?:\.\d+)+$$" in compose_path.read_bytes()


def test_repair_blocks_when_local_image_tag_moves(tmp_path: Path) -> None:
    client, fake_root, compose_path = _tracking_fixture(tmp_path)
    original = compose_path.read_bytes()
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    headers = _csrf_headers(client)
    payload = {"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"}
    preview = client.post("/api/v1/tracking-repairs", json=payload, headers=headers)
    assert preview.json()["can_apply"] is True
    _fake_image_state_file(fake_root, "repo/bindery:v1.36.2", "id").write_text(
        "sha256:" + "b" * 64, encoding="utf-8"
    )

    apply = client.post(
        "/api/v1/tracking-repairs/apply",
        json={**payload, "plan_id": preview.json()["plan_id"], "confirmation": "apply-tracking-repair"},
        headers=headers,
    )

    assert apply.status_code == 409
    assert compose_path.read_bytes() == original
    assert " up " not in _fake_docker_calls(fake_root)


def test_repair_does_not_recreate_after_tag_moves_during_label_write(
    tmp_path: Path, monkeypatch
) -> None:
    client, fake_root, compose_path = _tracking_fixture(tmp_path)
    original = compose_path.read_bytes()
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    headers = _csrf_headers(client)
    payload = {"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"}
    preview = client.post("/api/v1/tracking-repairs", json=payload, headers=headers).json()
    original_apply = web_tracking.apply_compose_tracking_label

    def move_tag_after_write(*args, **kwargs) -> None:
        original_apply(*args, **kwargs)
        _fake_image_state_file(fake_root, "repo/bindery:v1.36.2", "id").write_text(
            "sha256:" + "b" * 64, encoding="utf-8"
        )

    monkeypatch.setattr(web_tracking, "apply_compose_tracking_label", move_tag_after_write)
    response = client.post(
        "/api/v1/tracking-repairs/apply",
        json={**payload, "plan_id": preview["plan_id"], "confirmation": "apply-tracking-repair"},
        headers=headers,
    )
    job = _wait_apply_job(client, response.json()["job_id"])

    assert job["status"] == "failure"
    assert "Running image changed" in job["error"]
    assert compose_path.read_bytes() == original
    assert " up " not in _fake_docker_calls(fake_root)


def test_repair_rechecks_compose_immediately_before_recreate(
    tmp_path: Path, monkeypatch
) -> None:
    client, fake_root, compose_path = _tracking_fixture(tmp_path)
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    payload = {"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"}
    plan = client.post(
        "/api/v1/tracking-repairs", json=payload, headers=_csrf_headers(client),
    ).json()
    original_check = web_tracking._matching_runtime_image_id
    checks = 0

    def edit_after_last_image_check(*args, **kwargs):
        nonlocal checks
        result = original_check(*args, **kwargs)
        checks += 1
        if checks == 4:
            compose_path.write_text(
                compose_path.read_text(encoding="utf-8") + "# operator edit\n",
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(web_tracking, "_matching_runtime_image_id", edit_after_last_image_check)
    response = client.post(
        "/api/v1/tracking-repairs/apply",
        json={**payload, "plan_id": plan["plan_id"], "confirmation": "apply-tracking-repair"},
        headers=_csrf_headers(client),
    )
    job = _wait_apply_job(client, response.json()["job_id"])

    assert checks == 4
    assert job["status"] == "failure"
    assert "Compose file changed" in job["error"]
    assert " up " not in _fake_docker_calls(fake_root)


def test_repair_rejects_stale_plan_and_read_only_mode(tmp_path: Path) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    headers = _csrf_headers(client)
    payload = {"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"}
    plan = client.post("/api/v1/tracking-repairs", json=payload, headers=headers).json()
    compose_path.write_text(compose_path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    response = client.post(
        "/api/v1/tracking-repairs/apply",
        json={**payload, "plan_id": plan["plan_id"], "confirmation": "apply-tracking-repair"},
        headers=headers,
    )
    assert response.status_code == 409

    other_client, _fake_root, _compose_path = _tracking_fixture(tmp_path / "readonly", mutations=False)
    other_target = other_client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    blocked = other_client.post(
        "/api/v1/tracking-repairs/apply",
        json={"target_id": other_target, "regex": r"^v\d+(?:\.\d+)+$", "plan_id": "invalid", "confirmation": "apply-tracking-repair"},
        headers=_csrf_headers(other_client),
    )
    assert blocked.status_code == 403


def test_repair_rejects_unsafe_regex_and_missing_csrf(tmp_path: Path) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    original = compose_path.read_text(encoding="utf-8")
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    headers = _csrf_headers(client)
    unsafe = client.post(
        "/api/v1/tracking-repairs",
        json={"target_id": target, "regex": r"^(v\d+)+$"},
        headers=headers,
    )
    assert unsafe.status_code == 422
    overlapping = client.post(
        "/api/v1/tracking-repairs",
        json={"target_id": target, "regex": r"^v\d+\d+\.\d+$"},
        headers=headers,
    )
    assert overlapping.status_code == 422
    missing_csrf = client.post(
        "/api/v1/tracking-repairs/apply",
        json={"target_id": target, "regex": r"^v\d+(?:\.\d+)+$", "plan_id": "invalid", "confirmation": "apply-tracking-repair"},
    )
    assert missing_csrf.status_code == 403
    assert compose_path.read_text(encoding="utf-8") == original


def test_repair_rejects_duplicate_tracking_labels(tmp_path: Path) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    original = compose_path.read_text(encoding="utf-8")
    compose_path.write_text(
        original + "      - wud.tag.include=^v1\\.36\\.2$$\n", encoding="utf-8"
    )
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]

    response = client.post(
        "/api/v1/tracking-repairs",
        json={"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 409
    assert "duplicate WUD tag filters" in response.json()["detail"]
    assert compose_path.read_text(encoding="utf-8") == original + "      - wud.tag.include=^v1\\.36\\.2$$\n"


def test_repair_writer_preserves_external_compose_edit(tmp_path: Path) -> None:
    _client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    source = compose_path.read_bytes()
    compose_path.write_bytes(source + b"# operator change\n")

    with pytest.raises(ComposeTagRewriteError, match="Compose file changed"):
        apply_compose_tracking_label(
            compose_path, "bindery", "repo/bindery:v1.36.2", r"^v1\.36\.2$",
            r"^v\d+(?:\.\d+)+$",
            expected_source_hash=hashlib.sha256(source).hexdigest(),
        )

    assert compose_path.read_bytes() == source + b"# operator change\n"


def test_compose_writers_serialize_validation_and_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "compose.yml"
    path.write_text("original", encoding="utf-8")
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    first_replacing = Event()
    finish_first = Event()
    original_replace = compose_persistence.os.replace

    def hold_first_replace(source, target):
        if ".first." in str(source):
            first_replacing.set()
            assert finish_first.wait(5)
        original_replace(source, target)

    monkeypatch.setattr(compose_persistence.os, "replace", hold_first_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            compose_rewrite._atomic_replace_compose,
            path, "first", prefix="first", expected_source_hash=source_hash,
        )
        try:
            assert first_replacing.wait(5)
            second = pool.submit(
                compose_rewrite._atomic_replace_compose,
                path, "second", prefix="second", expected_source_hash=source_hash,
            )
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            finish_first.set()
        first.result(timeout=5)
        with pytest.raises(ComposeTagRewriteError, match="Compose file changed"):
            second.result(timeout=5)
    assert path.read_text(encoding="utf-8") == "first"


def test_compose_rollback_preserves_newer_edit_and_backup_bytes(tmp_path: Path) -> None:
    path = tmp_path / "compose.yml"
    backup = tmp_path / "compose.backup.yml"
    backup.write_bytes(b"services:\r\n  app:\r\n    image: repo/app:v1\r\n")
    path.write_text("wudup rewrite\n", encoding="utf-8")
    written_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text("operator change\n", encoding="utf-8")

    with pytest.raises(ComposeTagRewriteError, match="Compose file changed"):
        compose_rewrite.restore_compose_backup(
            backup, path, expected_source_hash=written_hash,
        )
    assert path.read_text(encoding="utf-8") == "operator change\n"
    assert backup.exists()

    path.write_text("wudup rewrite\n", encoding="utf-8")
    compose_rewrite.restore_compose_backup(
        backup, path, expected_source_hash=written_hash,
    )
    assert path.read_bytes() == backup.read_bytes()


def test_tracking_audit_rolls_back_event_when_run_update_fails(tmp_path: Path) -> None:
    client, _fake_root, _compose_path = _tracking_fixture(tmp_path)
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    plan_response = client.post(
        "/api/v1/tracking-repairs",
        json={"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"},
        headers=_csrf_headers(client),
    )
    plan = TrackingRepairPlan.model_validate(plan_response.json())
    settings = client.app.state.web_settings
    with open_db(settings.config.db_path) as conn:
        init_db(conn)
        run_id = insert_update_run(conn, status="running", mode="web-tracking-repair")
        conn.execute(
            "CREATE TRIGGER fail_tracking_run_update BEFORE UPDATE ON update_runs "
            "BEGIN SELECT RAISE(FAIL, 'blocked'); END"
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="blocked"):
        web_tracking._finish_tracking_audit(settings, run_id, plan, "success")

    with open_db(settings.config.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM update_events WHERE run_id = ?", (run_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM update_runs WHERE id = ?", (run_id,)
        ).fetchone()[0] == "running"


def test_repair_failure_restores_compose_and_audits(tmp_path: Path) -> None:
    client, fake_root, compose_path = _tracking_fixture(tmp_path)
    original = compose_path.read_text(encoding="utf-8")
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    headers = _csrf_headers(client)
    payload = {"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"}
    plan = client.post("/api/v1/tracking-repairs", json=payload, headers=headers).json()
    (fake_root / "stacks" / "bindery" / "up_fail").touch()

    apply = client.post(
        "/api/v1/tracking-repairs/apply",
        json={**payload, "plan_id": plan["plan_id"], "confirmation": "apply-tracking-repair"},
        headers=headers,
    )
    assert apply.status_code == 202
    job = _wait_apply_job(client, apply.json()["job_id"])
    assert job["status"] == "failure"
    assert "backup was preserved" in job["error"]
    assert compose_path.read_text(encoding="utf-8") == original
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        run = conn.execute("SELECT mode, status FROM update_runs WHERE id = ?", (job["run_id"],)).fetchone()
    assert (run["mode"], run["status"]) == ("web-tracking-repair", "failure")


def test_repair_rollback_preserves_external_compose_edit(
    tmp_path: Path, monkeypatch
) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    headers = _csrf_headers(client)
    payload = {"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"}
    plan = client.post("/api/v1/tracking-repairs", json=payload, headers=headers).json()

    def external_edit_then_fail(*_args, **_kwargs) -> None:
        compose_path.write_text(
            compose_path.read_text(encoding="utf-8") + "# operator edit\n",
            encoding="utf-8",
        )
        raise RuntimeError("recreate failed")

    monkeypatch.setattr(web_tracking.ComposeCli, "up", external_edit_then_fail)
    response = client.post(
        "/api/v1/tracking-repairs/apply",
        json={**payload, "plan_id": plan["plan_id"], "confirmation": "apply-tracking-repair"},
        headers=headers,
    )
    job = _wait_apply_job(client, response.json()["job_id"])

    assert job["status"] == "failure"
    assert "backup was preserved" in job["error"]
    assert "# operator edit" in compose_path.read_text(encoding="utf-8")
    assert list(compose_path.parent.glob(".docker-compose.yml.backup.*"))


def test_repair_detects_operator_edit_during_successful_recreate(
    tmp_path: Path, monkeypatch
) -> None:
    client, _fake_root, compose_path = _tracking_fixture(tmp_path)
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    headers = _csrf_headers(client)
    payload = {"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"}
    plan = client.post("/api/v1/tracking-repairs", json=payload, headers=headers).json()
    original_up = web_tracking.ComposeCli.up

    def external_edit_after_up(self, *args, **kwargs):
        original_up(self, *args, **kwargs)
        compose_path.write_text(
            compose_path.read_text(encoding="utf-8") + "# operator edit\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(web_tracking.ComposeCli, "up", external_edit_after_up)
    response = client.post(
        "/api/v1/tracking-repairs/apply",
        json={**payload, "plan_id": plan["plan_id"], "confirmation": "apply-tracking-repair"},
        headers=headers,
    )
    job = _wait_apply_job(client, response.json()["job_id"])

    assert job["status"] == "failure"
    assert "Compose file changed during tracking repair" in job["error"]
    assert "backup was preserved" in job["error"]
    assert "# operator edit" in compose_path.read_text(encoding="utf-8")


def test_audit_failure_does_not_leave_repair_job_running(
    tmp_path: Path, monkeypatch
) -> None:
    client, fake_root, _compose_path = _tracking_fixture(tmp_path)
    target = client.get("/api/v1/retag-targets").json()["items"][0]["target_id"]
    headers = _csrf_headers(client)
    payload = {"target_id": target, "regex": r"^v\d+(?:\.\d+)+$"}
    plan = client.post("/api/v1/tracking-repairs", json=payload, headers=headers).json()
    (fake_root / "stacks" / "bindery" / "up_fail").touch()

    def fail_audit(*_args, **_kwargs) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(web_tracking, "_finish_tracking_audit", fail_audit)
    response = client.post(
        "/api/v1/tracking-repairs/apply",
        json={**payload, "plan_id": plan["plan_id"], "confirmation": "apply-tracking-repair"},
        headers=headers,
    )
    job = _wait_apply_job(client, response.json()["job_id"])
    assert job["status"] == "failure"
    assert "audit record could not be finalized" in job["error"]
