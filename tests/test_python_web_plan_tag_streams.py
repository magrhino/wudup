from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

import pytest
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _make_fake_stack,
    _manifest_index_digest,
    _write_fake_manifest,
)

from wudup.compose import ComposeStack, ServiceImage
from wudup.tag_streams import (
    pending_tag_stream_hint,
    plan_tag_stream_changes,
    retag_tag_include_regex,
    tag_stream_include_regex,
)
from wudup.updater_models import Match, TagStreamDecision
from wudup.wud_file import parse_wud_text


def _stream_client(
    tmp_path: Path,
    *,
    image: str = "n8nio/runners:2.33.5-distroless",
    target: str = "2.34.4",
) -> tuple[object, Path, Path]:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    (tmp_path / "state" / "images.todo").write_text(
        f"{image} tag={target}\n",
        encoding="utf-8",
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis",
        [("task-runner", image, "cid-task-runner")],
    )
    return client, fake_root, compose_dir


def _plan(client: object, payload: dict[str, object]):
    return client.post(  # type: ignore[attr-defined]
        "/api/v1/plans",
        json={"line_numbers": [1], "allow_tag_updates": True, **payload},
        headers=_csrf_headers(client),  # type: ignore[arg-type]
    )


def test_verified_distroless_change_requires_explicit_decision(tmp_path: Path) -> None:
    client, fake_root, compose_dir = _stream_client(tmp_path)
    before = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")

    unresolved = _plan(client, {})
    assert unresolved.status_code == 200
    unresolved_body = unresolved.json()
    assert unresolved_body["status"] == "blocked"
    issue = next(
        item for item in unresolved_body["issues"] if item["code"] == "tag-stream-change"
    )
    assert issue["details"]["same_stream_tag"] == "2.34.4-distroless"
    assert issue["details"]["current_stream"] == "distroless"
    assert issue["details"]["reported_stream"] == "default"

    preserved = _plan(
        client,
        {"tag_stream_decisions": [{"line_no": 1, "decision": "preserve"}]},
    )
    switched = _plan(
        client,
        {"tag_stream_decisions": [{"line_no": 1, "decision": "switch"}]},
    )

    assert preserved.status_code == 200
    preserve_body = preserved.json()
    assert preserve_body["status"] == "ready"
    assert preserve_body["stacks"][0]["tag_updates"][0]["new_image"] == (
        "n8nio/runners:2.34.4-distroless"
    )
    preserve_stream = preserve_body["stacks"][0]["tag_stream_updates"][0]
    assert preserve_stream["selected_tag"] == "2.34.4-distroless"
    assert preserve_stream["proposed_label_regex"] == (
        r"^\d+\.\d+\.\d+-distroless$"
    )
    assert preserve_stream["proposed_label_value"].endswith("$$")
    assert any(
        action["kind"] == "compose-tag-stream"
        for action in preserve_body["stacks"][0]["actions"]
    )

    assert switched.status_code == 200
    switch_body = switched.json()
    assert switch_body["status"] == "ready"
    assert switch_body["stacks"][0]["tag_updates"][0]["new_image"] == (
        "n8nio/runners:2.34.4"
    )
    switch_stream = switch_body["stacks"][0]["tag_stream_updates"][0]
    assert switch_stream["selected_tag"] == "2.34.4"
    assert switch_stream["proposed_label_regex"] == r"^\d+\.\d+\.\d+$"
    assert preserve_body["plan_id"] != switch_body["plan_id"]
    assert (compose_dir / "docker-compose.yml").read_text(encoding="utf-8") == before

    calls = _fake_docker_calls(fake_root)
    assert "manifest inspect n8nio/runners:2.34.4-distroless" in calls
    assert "manifest inspect n8nio/runners:2.34.4" in calls


def test_stream_decision_only_rewrites_candidate_services(tmp_path: Path) -> None:
    _fake_env, fake_root = _fake_docker_env(tmp_path)
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis",
        [
            ("distroless", "n8nio/runners:2.33.5-distroless", "cid-distroless"),
            ("default", "n8nio/runners:2.33.5", "cid-default"),
        ],
    )
    images = (
        "n8nio/runners:2.33.5-distroless",
        "n8nio/runners:2.33.5",
    )
    stack = ComposeStack(
        index=0,
        directory=compose_dir,
        file="docker-compose.yml",
        name="jarvis",
        images=images,
        service_images=(
            ServiceImage("distroless", images[0]),
            ServiceImage("default", images[1]),
        ),
    )
    target = parse_wud_text(
        "n8nio/runners:2.33.5-distroless tag=2.34.4\n"
    ).targets[0]
    matches = tuple(
        Match(
            stack=stack,
            target=target,
            resolved=image,
            compose_image=image,
            service=service,
        )
        for service, image in zip(("distroless", "default"), images, strict=True)
    )

    result = plan_tag_stream_changes(
        mock.Mock(),
        matches,
        decisions=(TagStreamDecision(line_no=1, decision="preserve"),),
    )

    assert [update.service for update in result.updates_by_stack[0]] == ["distroless"]
    assert {match.service: match.target.desired_tag for match in result.matches} == {
        "distroless": "2.34.4-distroless",
        "default": "2.34.4",
    }


def test_unapproved_stream_label_reports_deferred_digest_validation(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_DIGEST_PIN_UPDATES": "true",
            **fake_env,
        },
    )
    (tmp_path / "state" / "images.todo").write_text(
        "n8nio/runners:2.33.5-distroless tag=2.34.4\n",
        encoding="utf-8",
    )
    _write_fake_manifest(
        fake_root,
        "docker.io/n8nio/runners:2.34.4-distroless",
        _manifest_index_digest("sha256:index", "sha256:child"),
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "jarvis",
        [("task-runner", "n8nio/runners:2.33.5-distroless", "cid-task-runner")],
    )
    compose_path = compose_dir / "docker-compose.yml"
    compose_path.write_text(
        compose_path.read_text(encoding="utf-8").replace(
            "    image: n8nio/runners:2.33.5-distroless\n",
            "    image: n8nio/runners:2.33.5-distroless\n"
            "    labels:\n"
            "      wud.tag.include: ^stable-.+$$\n",
        ),
        encoding="utf-8",
    )

    response = _plan(
        client,
        {"tag_stream_decisions": [{"line_no": 1, "decision": "preserve"}]},
    )

    assert response.status_code == 200
    codes = {issue["code"] for issue in response.json()["issues"]}
    assert "compose-tag-stream-label-rewrite-unapproved" in codes
    assert "compose-digest-pin-validation-deferred" in codes


@pytest.mark.parametrize(
    ("image", "target"),
    [
        ("lscr.io/linuxserver/swag:2.6.0-ls224", "2.7.0-ls1"),
        ("mirror.example/swag:2.6.0-ls224", "2.7.0-ls1"),
    ],
)
def test_lsio_build_tags_do_not_trigger_stream_detection(
    tmp_path: Path,
    image: str,
    target: str,
) -> None:
    client, fake_root, _compose_dir = _stream_client(
        tmp_path,
        image=image,
        target=target,
    )

    response = _plan(client, {})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert not any("stream" in issue["code"] for issue in body["issues"])
    assert body["stacks"][0]["tag_stream_updates"] == []
    calls = _fake_docker_calls(fake_root)
    repo = image.rsplit(":", 1)[0]
    assert f"manifest inspect {repo}:2.7.0-ls1" in calls
    assert f"manifest inspect {repo}:2.7.0-ls224" not in calls


def test_unverified_same_stream_tag_warns_without_blocking(tmp_path: Path) -> None:
    client, fake_root, _compose_dir = _stream_client(tmp_path)
    (fake_root / "manifests" / "n8nio_runners_2.34.4-distroless.fail").touch()

    response = _plan(client, {})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    warning = next(
        item
        for item in body["issues"]
        if item["code"] == "possible-tag-stream-change"
    )
    assert "could not be verified" in warning["message"]
    assert "same_stream_tag" not in warning["details"]
    assert body["stacks"][0]["tag_stream_updates"] == []


def test_custom_stream_label_requires_exact_stale_bound_approval(
    tmp_path: Path,
) -> None:
    client, _fake_root, compose_dir = _stream_client(tmp_path)
    compose_path = compose_dir / "docker-compose.yml"
    compose_path.write_text(
        """services:
  task-runner:
    image: n8nio/runners:2.33.5-distroless
    labels:
      wud.tag.include: ^stable-.+$$
""",
        encoding="utf-8",
    )
    decision = {"line_no": 1, "decision": "preserve"}

    blocked = _plan(client, {"tag_stream_decisions": [decision]})
    assert blocked.status_code == 200
    body = blocked.json()
    issue = next(
        item
        for item in body["issues"]
        if item["code"] == "compose-tag-stream-label-rewrite-unapproved"
    )
    assert body["status"] == "blocked"
    approval = {
        "line_no": 1,
        "stack": issue["stack"],
        "stack_directory": issue["details"]["stack_directory"],
        "compose_file": issue["details"]["compose_file"],
        "service": issue["service"],
        "label_key": issue["details"]["label_key"],
        "current_label_value": issue["details"]["current_label_value"],
        "selected_tag": issue["details"]["selected_tag"],
        "proposed_label_value": issue["details"]["proposed_label_value"],
    }

    approved = _plan(
        client,
        {
            "tag_stream_decisions": [decision],
            "tag_stream_label_rewrite_approvals": [approval],
        },
    )
    assert approved.status_code == 200
    approved_body = approved.json()
    assert approved_body["status"] == "ready"
    assert approved_body["stacks"][0]["tag_stream_updates"][0]["approved"] is True

    forged = dict(approval, current_label_value="^other$")
    rejected = _plan(
        client,
        {
            "tag_stream_decisions": [decision],
            "tag_stream_label_rewrite_approvals": [forged],
        },
    )
    assert rejected.status_code == 422
    assert "stale or forged" in rejected.json()["detail"]


def test_stream_label_approval_retains_later_compose_unsafe_issue(
    tmp_path: Path,
) -> None:
    client, _fake_root, compose_dir = _stream_client(tmp_path)
    compose_path = compose_dir / "docker-compose.yml"
    compose_path.write_text(
        "services:\n"
        "  task-runner:\n"
        "    image: n8nio/runners:2.33.5-distroless\n"
        "    labels:\n"
        "      wud.tag.include: ^stable-.+$$\n",
        encoding="utf-8",
    )
    decision = {"line_no": 1, "decision": "preserve"}
    blocked = _plan(client, {"tag_stream_decisions": [decision]}).json()
    issue = next(
        item
        for item in blocked["issues"]
        if item["code"] == "compose-tag-stream-label-rewrite-unapproved"
    )
    approval = {
        "line_no": 1,
        "stack": issue["stack"],
        "stack_directory": issue["details"]["stack_directory"],
        "compose_file": issue["details"]["compose_file"],
        "service": issue["service"],
        "label_key": issue["details"]["label_key"],
        "current_label_value": issue["details"]["current_label_value"],
        "selected_tag": issue["details"]["selected_tag"],
        "proposed_label_value": issue["details"]["proposed_label_value"],
    }
    compose_path.write_text(
        "x-labels: &labels\n"
        "  wud.tag.include: ^stable-.+$$\n"
        "services:\n"
        "  task-runner:\n"
        "    image: n8nio/runners:2.33.5-distroless\n"
        "    labels: *labels\n",
        encoding="utf-8",
    )

    response = _plan(
        client,
        {
            "tag_stream_decisions": [decision],
            "tag_stream_label_rewrite_approvals": [approval],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert any(
        item["code"] == "compose-tag-stream-rewrite-unsafe"
        for item in body["issues"]
    )
    for field, value in (
        ("selected_tag", "9.9.9-forged"),
        ("proposed_label_value", "^forged$$"),
    ):
        forged = dict(approval, **{field: value})
        rejected = _plan(
            client,
            {
                "tag_stream_decisions": [decision],
                "tag_stream_label_rewrite_approvals": [forged],
            },
        )
        assert rejected.status_code == 422
        assert "stale or forged" in rejected.json()["detail"]


def test_stream_label_approval_is_bound_to_one_compose_file(
    tmp_path: Path,
) -> None:
    client, _fake_root, compose_dir = _stream_client(tmp_path)
    custom_compose = (
        "services:\n"
        "  task-runner:\n"
        "    image: n8nio/runners:2.33.5-distroless\n"
        "    labels:\n"
        "      wud.tag.include: ^stable-.+$$\n"
    )
    (compose_dir / "docker-compose.yml").write_text(
        custom_compose,
        encoding="utf-8",
    )
    (compose_dir / "compose.yml").write_text(custom_compose, encoding="utf-8")
    decision = {"line_no": 1, "decision": "preserve"}

    blocked = _plan(client, {"tag_stream_decisions": [decision]}).json()
    issues = [
        item
        for item in blocked["issues"]
        if item["code"] == "compose-tag-stream-label-rewrite-unapproved"
    ]
    assert blocked["status"] == "blocked"
    assert len(issues) == 2

    issue = next(
        item for item in issues if item["details"].get("compose_file") == "compose.yml"
    )
    approval = {
        "line_no": 1,
        "stack": issue["stack"],
        "stack_directory": str(compose_dir.resolve(strict=False)),
        "compose_file": "compose.yml",
        "service": issue["service"],
        "label_key": issue["details"]["label_key"],
        "current_label_value": issue["details"]["current_label_value"],
        "selected_tag": issue["details"]["selected_tag"],
        "proposed_label_value": issue["details"]["proposed_label_value"],
    }

    partial = _plan(
        client,
        {
            "tag_stream_decisions": [decision],
            "tag_stream_label_rewrite_approvals": [approval],
        },
    ).json()
    remaining = [
        item
        for item in partial["issues"]
        if item["code"] == "compose-tag-stream-label-rewrite-unapproved"
    ]

    assert partial["status"] == "blocked"
    assert len(remaining) == 1
    assert remaining[0]["details"]["compose_file"] == "docker-compose.yml"


@pytest.mark.parametrize(
    ("label", "reason"),
    [
        (r"^2\.33\.5-distroless$$", "exact-tag-normalized"),
        (r"^\d+\.\d+\.\d+-distroless$$", "label-matches"),
    ],
)
def test_managed_stream_labels_do_not_require_extra_approval(
    tmp_path: Path,
    label: str,
    reason: str,
) -> None:
    client, _fake_root, compose_dir = _stream_client(tmp_path)
    (compose_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  task-runner:\n"
        "    image: n8nio/runners:2.33.5-distroless\n"
        "    labels:\n"
        f"      - wud.tag.include={label}\n",
        encoding="utf-8",
    )

    response = _plan(
        client,
        {"tag_stream_decisions": [{"line_no": 1, "decision": "preserve"}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["stacks"][0]["tag_stream_updates"][0]["reason"] == reason


def test_stream_decision_cannot_be_bypassed_with_tag_override(tmp_path: Path) -> None:
    client, _fake_root, _compose_dir = _stream_client(tmp_path)

    response = _plan(
        client,
        {"tag_overrides": [{"line_no": 1, "tag": "2.34.4-distroless"}]},
    )

    assert response.status_code == 422
    assert "explicit tag_stream_decision" in response.json()["detail"]


@pytest.mark.parametrize(
    "decisions",
    [
        [
            {"line_no": 1, "decision": "preserve"},
            {"line_no": 1, "decision": "switch"},
        ],
        [{"line_no": 1, "decision": "ignore"}],
    ],
)
def test_duplicate_and_forged_stream_decisions_are_rejected(
    tmp_path: Path,
    decisions: list[dict[str, object]],
) -> None:
    client, _fake_root, _compose_dir = _stream_client(tmp_path)

    response = _plan(client, {"tag_stream_decisions": decisions})

    assert response.status_code == 422


def test_stream_decision_for_non_selected_line_is_rejected(tmp_path: Path) -> None:
    client, _fake_root, _compose_dir = _stream_client(tmp_path)
    (tmp_path / "state" / "images.todo").write_text(
        "n8nio/runners:2.33.5-distroless tag=2.34.4\n"
        "example/worker:1.0.0-slim tag=1.1.0\n",
        encoding="utf-8",
    )

    response = _plan(
        client,
        {"tag_stream_decisions": [{"line_no": 2, "decision": "preserve"}]},
    )

    assert response.status_code == 422
    assert "verified stream-change lines" in response.json()["detail"]


def test_duplicate_stream_label_approval_is_rejected(tmp_path: Path) -> None:
    client, _fake_root, compose_dir = _stream_client(tmp_path)
    (compose_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  task-runner:\n"
        "    image: n8nio/runners:2.33.5-distroless\n"
        "    labels:\n"
        "      wud.tag.include: ^stable-.+$$\n",
        encoding="utf-8",
    )
    decision = {"line_no": 1, "decision": "preserve"}
    blocked = _plan(client, {"tag_stream_decisions": [decision]}).json()
    issue = next(
        item
        for item in blocked["issues"]
        if item["code"] == "compose-tag-stream-label-rewrite-unapproved"
    )
    approval = {
        "line_no": 1,
        "stack": issue["stack"],
        "stack_directory": issue["details"]["stack_directory"],
        "compose_file": issue["details"]["compose_file"],
        "service": issue["service"],
        "label_key": issue["details"]["label_key"],
        "current_label_value": issue["details"]["current_label_value"],
        "selected_tag": issue["details"]["selected_tag"],
        "proposed_label_value": issue["details"]["proposed_label_value"],
    }

    response = _plan(
        client,
        {
            "tag_stream_decisions": [decision],
            "tag_stream_label_rewrite_approvals": [approval, approval],
        },
    )

    assert response.status_code == 422
    assert "duplicate approval" in response.json()["detail"]


@pytest.mark.parametrize(
    ("current", "reported", "expected"),
    [
        ("2.33.5-distroless", "2.34.4-distroless", None),
        ("latest", "2.34.4", None),
        ("2.33", "2.34.4", None),
        ("2.33.5", "2.34.4-gpu", ("default", "gpu")),
        ("2.33.5-gpu", "2.34.4-cuda", ("gpu", "cuda")),
    ],
)
def test_pending_stream_hint_is_strict(
    current: str,
    reported: str,
    expected: tuple[str, str] | None,
) -> None:
    hint = pending_tag_stream_hint(
        image_repo="example/app",
        current_tag=current,
        reported_tag=reported,
    )
    actual = None if hint is None else (hint.current_stream, hint.reported_stream)
    assert actual == expected


def test_stream_regex_preserves_leading_v() -> None:
    assert tag_stream_include_regex("v2.34.4-distroless") == (
        r"^v\d+\.\d+\.\d+-distroless$"
    )


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v1.2.3", r"^v\d+(?:\.\d+)+$"),
        ("v1.36.2", r"^v\d+(?:\.\d+)+$"),
        ("1.2.3.4-ls5", r"^\d+\.\d+\.\d+\.\d+-ls\d+$"),
        ("10.11.12ubu2404-ls44", r"^\d+\.\d+\.\d+ubu\d+-ls\d+$"),
        ("2026.8.3", r"^\d+(?:\.\d+)+$"),
        ("1", r"^\d+$"),
        ("2.7-alpine", r"^\d+\.\d+-alpine$"),
        ("2026-07-24-r1", r"^\d+-\d+-\d+-r\d+$"),
        ("v5", r"^v\d+$"),
        ("stable", r"^stable$"),
    ],
)
def test_retag_regex_generalizes_numeric_parts(tag: str, expected: str) -> None:
    assert retag_tag_include_regex(tag) == expected


def test_retag_regex_covers_bindery_shorter_dotted_release() -> None:
    assert re.fullmatch(retag_tag_include_regex("v1.36.2"), "v1.37") is not None
