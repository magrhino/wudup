from __future__ import annotations

import json
from pathlib import Path

from tests.web_retag_test_helpers import (
    _apply_retag_plan,
    _make_retag_fixture,
    _seed_known_image,
    _switch_choice,
    _write_compose,
)
from tests.web_test_helpers import (
    _assert_pending_grouping_did_not_mutate,
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _make_fake_stack,
    _wait_apply_job,
)

from wudup.db import open_db
from wudup.web_models import RetagChoiceRequest
from wudup.web_retag_choices import validated_retag_choice_map


def test_retag_plan_accepts_duplicate_service_keys_with_target_ids(
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
    first_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app@sha256:old", "cid-app-one")],
        parent=tmp_path / "docker" / "one",
    )
    second_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app@sha256:old", "cid-app-two")],
        parent=tmp_path / "docker" / "two",
    )
    _write_compose(
        first_dir,
        "app",
        "repo/app@sha256:old",
        label_value="^latest$$",
    )
    _write_compose(
        second_dir,
        "app",
        "repo/app@sha256:old",
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
    targets_response = client.get("/api/v1/retag-targets")

    assert targets_response.status_code == 200
    items = targets_response.json()["items"]
    assert [item["service_key"] for item in items] == ["stack/app", "stack/app"]
    assert len({item["target_id"] for item in items}) == 2
    headers = _csrf_headers(client)

    ambiguous_response = client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                {"service_key": "stack/app", "choice": "switch-to-concrete"},
                {"service_key": "stack/app", "choice": "switch-to-concrete"},
            ]
        },
        headers=headers,
    )

    assert ambiguous_response.status_code == 422
    assert "target_id" in ambiguous_response.json()["detail"]

    plan_response = client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                _switch_choice(
                    service_key=item["service_key"],
                    target_id=item["target_id"],
                )
                for item in items
            ]
        },
        headers=headers,
    )

    assert plan_response.status_code == 200
    plan = plan_response.json()
    expected_target_ids = {item["target_id"] for item in items}
    assert plan["status"] == "ready"
    assert plan["selected_count"] == 2
    assert len(plan["stacks"]) == 2
    assert {
        update["target_id"]
        for stack in plan["stacks"]
        for update in stack["tag_updates"]
    } == expected_target_ids
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))

    choices = [
        _switch_choice(service_key=item["service_key"], target_id=item["target_id"])
        for item in items
    ]
    apply_response = _apply_retag_plan(client, headers, plan, choices=choices)
    assert apply_response.status_code == 202
    job = _wait_apply_job(client, apply_response.json()["job_id"])
    assert job["status"] == "success"
    # Both stacks share the "stack" project, so --remove-orphans would let each
    # recreate delete the other stack's containers.
    up_calls = [
        line for line in _fake_docker_calls(fake_root).splitlines()
        if "compose -f docker-compose.yml up -d" in line
    ]
    assert len(up_calls) == 2
    assert all("--remove-orphans" not in line for line in up_calls)
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        known = conn.execute(
            """
            SELECT digest_provenance_source, digest_watch_tag
            FROM known_images
            WHERE service_key = 'stack/app'
            """,
        ).fetchone()
        events = conn.execute(
            """
            SELECT metadata_json
            FROM update_events
            WHERE run_id = ?
            ORDER BY id
            """,
            (job["run_id"],),
        ).fetchall()

    assert known["digest_provenance_source"] == "apply"
    assert known["digest_watch_tag"] == "latest"
    event_metadata = [json.loads(row["metadata_json"]) for row in events]
    assert {event["target_id"] for event in event_metadata} == expected_target_ids
    assert all(event["known_image_recorded"] is False for event in event_metadata)
    assert {
        event["known_image_skip_reason"] for event in event_metadata
    } == {"duplicate service_key"}


def test_retag_plan_rejects_unknown_target_id(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_UPDATE_MODE": "live",
            "WUD_MAX_WAIT": "0",
        },
    )
    client = fixture.client
    targets_response = client.get("/api/v1/retag-targets")
    headers = _csrf_headers(client)

    assert targets_response.status_code == 200
    item = targets_response.json()["items"][0]

    response = client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                {
                    "service_key": item["service_key"],
                    "target_id": "non-existent-target-id",
                    "choice": "keep-current",
                }
            ]
        },
        headers=headers,
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "retag targets changed; reload retag targets before retrying. "
        "Affected service(s): stack/app"
    )
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fixture.fake_root))


def test_retag_plan_rejects_mismatched_service_key_and_target_id(
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
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("worker", "repo/worker:latest", "cid-worker"),
        ],
    )
    targets_response = client.get("/api/v1/retag-targets")
    headers = _csrf_headers(client)

    assert targets_response.status_code == 200
    items_by_service_key = {
        item["service_key"]: item for item in targets_response.json()["items"]
    }
    first = items_by_service_key["stack/app"]
    second = items_by_service_key["stack/worker"]

    response = client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                {
                    "service_key": first["service_key"],
                    "target_id": second["target_id"],
                    "choice": "keep-current",
                }
            ]
        },
        headers=headers,
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "retag choice target_id does not match service_key: "
        f"{first['service_key']} ({second['target_id']})"
    )
    _assert_pending_grouping_did_not_mutate(_fake_docker_calls(fake_root))


def test_retag_plan_rejects_duplicate_and_unknown_choices(tmp_path: Path) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env},
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    headers = _csrf_headers(client)

    duplicate = client.post(
        "/api/v1/retag-plans",
        json={
            "choices": [
                {"service_key": "stack/app", "choice": "keep-current"},
                {"service_key": "stack/app", "choice": "keep-current"},
            ]
        },
        headers=headers,
    )
    unknown = client.post(
        "/api/v1/retag-plans",
        json={"choices": [{"service_key": "stack/missing", "choice": "keep-current"}]},
        headers=headers,
    )

    assert duplicate.status_code == 422
    assert "duplicate" in duplicate.json()["detail"]
    assert unknown.status_code == 422
    assert "unknown service" in unknown.json()["detail"]


def test_validated_retag_choice_map_accepts_unique_service_key_only_choice() -> None:
    choice = RetagChoiceRequest(service_key="stack/app", choice="keep-current")

    values = validated_retag_choice_map(
        [choice],
        service_key_by_target_id={"target-one": "stack/app"},
    )

    assert values == {"target-one": choice}
