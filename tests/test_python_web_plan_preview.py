from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_calls,
    _fake_docker_env,
    _install_wud_api,
    _make_fake_stack,
    _write_fake_container_labels,
    _wud_api_container,
)

from wudup import plans as core_plans
from wudup import web_plans as plans_module
from wudup.compose import ComposeStack, ServiceImage
from wudup.config import ConfigError
from wudup.plan_matching import (
    selection_id_for_matches,
)
from wudup.plan_models import DryRunPlanSource
from wudup.updater_models import Match
from wudup.wud_file import parse_wud_text


def test_pending_source_plan_builder_preserves_source_hash_keyword(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_plan_builder(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(build=lambda: sentinel)

    monkeypatch.setattr(core_plans, "_PlanBuilder", fake_plan_builder)

    result = core_plans.build_dry_run_plan_from_pending_source(
        object(),
        parse_wud_text(""),
        source_file="WUD API",
        source_hash="authoritative",
        source=DryRunPlanSource(source_hash="embedded"),
        line_numbers=(),
    )

    assert result is sentinel
    assert captured["source_hash"] == "authoritative"


def test_plan_endpoint_rejects_unauthenticated_requests(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "setup required"


def test_plan_endpoint_requires_csrf_origin_headers(tmp_path: Path) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.post("/api/v1/plans", json={"line_numbers": [1]})

    assert response.status_code == 403
    assert response.json()["detail"] == "origin header is required"


def test_plan_endpoint_wraps_config_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    redacted_value = "plan-redaction-fixture"

    def invalid_config(_settings):
        raise ConfigError(
            f"failed to parse {tmp_path / 'state' / 'config.env'} with {redacted_value}"
        )

    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_TOKEN": redacted_value,
        },
    )
    monkeypatch.setattr(plans_module, "_effective_config_loader", invalid_config)

    response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )
    detail = response.json()["detail"]

    assert response.status_code == 500
    assert detail.startswith("could not create plan: ")
    assert redacted_value not in detail
    assert str(tmp_path) not in detail
    assert "<redacted>" in detail
    assert "[REDACTED_PATH]" in detail


def test_plan_endpoint_wraps_source_oserror(
    tmp_path: Path,
    monkeypatch,
) -> None:
    redacted_value = "plan-oserror-redaction-fixture"

    def fail_pending_source(*_args, **_kwargs):
        raise OSError(
            f"open failed for {tmp_path / 'state' / 'images.todo'} with {redacted_value}"
        )

    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_TOKEN": redacted_value,
        },
    )
    monkeypatch.setattr(
        plans_module.web_pending_sources,
        "resolve_pending_source",
        fail_pending_source,
    )

    response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )
    detail = response.json()["detail"]

    assert response.status_code == 500
    assert detail.startswith("could not create plan: ")
    assert redacted_value not in detail
    assert str(tmp_path) not in detail
    assert "<redacted>" in detail
    assert "[REDACTED_PATH]" in detail


def test_plan_endpoint_returns_selected_dry_run_without_mutation(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    db_path = tmp_path / "state" / "wud.sqlite"
    log_dir = tmp_path / "state" / "logs"
    original = "repo/app:latest\nrepo/db:latest\n"
    wud_file.write_text(original, encoding="utf-8")
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("db", "repo/db:latest", "cid-db"),
        ],
    )
    compose_before = (compose_dir / "docker-compose.yml").read_text(encoding="utf-8")

    response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [2]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["can_apply"] is False
    assert body["plan_id"]
    assert body["status"] == "ready"
    assert body["selected_line_numbers"] == [2]
    assert body["summary"]["target_count"] == 1
    assert body["summary"]["matched_target_count"] == 1
    assert [target["line_no"] for target in body["targets"]] == [2]
    assert body["stacks"][0]["name"] == "stack"
    assert body["stacks"][0]["services"] == ["db"]
    assert body["stacks"][0]["lines"][0]["service"] == "db"
    assert body["stacks"][0]["actions"][0]["kind"] == "pull"
    assert body["stacks"][0]["actions"][0]["args"][-1] == "db"
    assert [
        action["args"]
        for action in body["stacks"][0]["actions"]
        if action["kind"] == "up"
    ] == [[
        "docker", "compose", "-f", "docker-compose.yml",
        "up", "-d", "--remove-orphans", "--pull", "never", "--no-build",
        "--no-deps", "db",
    ]]
    assert body["issues"] == []
    assert wud_file.read_text(encoding="utf-8") == original
    assert (compose_dir / "docker-compose.yml").read_text(encoding="utf-8") == compose_before
    assert not db_path.exists()
    assert not log_dir.exists()
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls


def test_plan_endpoint_scopes_shared_line_by_selection_id(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/shared:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "active",
        [("app", "repo/shared:latest", "cid-active")],
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "backup",
        [("app", "repo/shared:latest", "cid-backup")],
    )
    headers = _csrf_headers(client)
    pending = client.get("/api/v1/pending").json()
    selections = {
        group["name"]: {
            "line_no": group["items"][0]["line_no"],
            "selection_id": group["items"][0]["selection_id"],
        }
        for group in pending["grouping"]["groups"]
    }

    active_only = client.post(
        "/api/v1/plans",
        json={"selections": [selections["active"]]},
        headers=headers,
    )
    both = client.post(
        "/api/v1/plans",
        json={"selections": [selections["active"], selections["backup"]]},
        headers=headers,
    )
    legacy = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    )

    assert active_only.status_code == 200
    active_body = active_only.json()
    assert active_body["selected_line_numbers"] == [1]
    assert active_body["selected_selections"] == [selections["active"]]
    assert [stack["name"] for stack in active_body["stacks"]] == ["active"]
    assert both.status_code == 200
    assert {stack["name"] for stack in both.json()["stacks"]} == {
        "active",
        "backup",
    }
    assert {
        (item["line_no"], item["selection_id"])
        for item in both.json()["selected_selections"]
    } == {
        (item["line_no"], item["selection_id"])
        for item in selections.values()
    }
    assert legacy.status_code == 200
    assert legacy.json()["selected_selections"] == []
    assert {stack["name"] for stack in legacy.json()["stacks"]} == {
        "active",
        "backup",
    }


def test_selection_identity_changes_when_compose_image_is_rewritten(
    tmp_path: Path,
) -> None:
    target = parse_wud_text("repo/shared:1.0 tag=2.0\n").targets[0]

    def match(compose_image: str) -> Match:
        stack = ComposeStack(
            index=1,
            directory=tmp_path / "shared",
            file="docker-compose.yml",
            name="shared",
            images=(compose_image,),
            service_images=(ServiceImage("app", compose_image),),
            project_directory=tmp_path / "shared",
            project_name="shared",
        )
        return Match(
            stack=stack,
            target=target,
            resolved=target.first,
            compose_image=compose_image,
            service="app",
        )

    original = match("repo/shared:1.0")
    tag_rewrite = match("repo/shared:2.0")
    digest_rewrite = match(f"repo/shared@sha256:{'a' * 64}")

    assert selection_id_for_matches((original,)) != selection_id_for_matches(
        (tag_rewrite,)
    )
    assert selection_id_for_matches((original,)) != selection_id_for_matches(
        (digest_rewrite,)
    )


def test_plan_endpoint_rejects_invalid_scoped_selections(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("repo/shared:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "active",
        [("app", "repo/shared:latest", "cid-active")],
    )
    headers = _csrf_headers(client)
    selection = client.get("/api/v1/pending").json()["grouping"]["groups"][0][
        "items"
    ][0]
    valid = {
        "line_no": selection["line_no"],
        "selection_id": selection["selection_id"],
    }

    duplicate = client.post(
        "/api/v1/plans",
        json={"selections": [valid, valid]},
        headers=headers,
    )
    forged = client.post(
        "/api/v1/plans",
        json={
            "selections": [
                {"line_no": 1, "selection_id": f"sel-v1-{'0' * 64}"}
            ]
        },
        headers=headers,
    )
    mixed_scope = client.post(
        "/api/v1/plans",
        json={
            "selections": [
                valid,
                {"line_no": 1, "selection_id": ""},
            ]
        },
        headers=headers,
    )
    mixed_contract = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1], "selections": [valid]},
        headers=headers,
    )
    unknown_line = client.post(
        "/api/v1/plans",
        json={"selections": [{"line_no": 2, "selection_id": ""}]},
        headers=headers,
    )

    assert duplicate.status_code == 422
    assert "more than once" in duplicate.json()["detail"]
    assert forged.status_code == 422
    assert "stale or no longer available" in forged.json()["detail"]
    assert mixed_scope.status_code == 422
    assert "cannot mix line-wide and stack-scoped" in mixed_scope.json()["detail"]
    assert mixed_contract.status_code == 422
    assert unknown_line.status_code == 422
    assert "actionable WUD target lines" in unknown_line.json()["detail"]


def test_plan_endpoint_uses_api_pending_source_without_wud_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    remote_digest = f"sha256:{'b' * 64}"
    _install_wud_api(
        monkeypatch,
        containers=[
            _wud_api_container(
                tag="latest",
                remote_tag="",
                remote_digest=remote_digest,
                update_kind="digest",
            )
        ],
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.plan-api-source.test:3000",
            **fake_env,
        },
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [
            ("app", "repo/app:latest", "cid-app"),
            ("worker", "repo/app:stable", "cid-worker"),
        ],
    )

    response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["active"] == "api"
    assert body["source_file"] == "WUD API"
    assert body["status"] == "ready"
    assert body["selected_line_numbers"] == [1]
    assert body["targets"][0]["raw"] == f"repo/app:latest@{remote_digest}"
    assert body["stacks"][0]["lines"][0]["service"] == "app"
    assert "worker" not in {
        line["service"] for stack in body["stacks"] for line in stack["lines"]
    }
    assert not (tmp_path / "state" / "images.todo").exists()
    calls = _fake_docker_calls(fake_root)
    assert " pull " not in calls
    assert " up -d " not in calls


def test_plan_endpoint_returns_apply_preflight_summary(tmp_path: Path) -> None:
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

    response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    preflight = body["apply_preflight"]
    assert body["can_apply"] is True
    assert preflight["ok"] is True
    assert preflight["failures"] == 0
    assert preflight["warnings"] == 0
    assert [check["code"] for check in preflight["checks"]] == [
        "docker-reachable",
        "compose-renders",
        "wud-file-writable",
        "database-ready",
        "logs-writable",
        "mutations-enabled",
        "wud-metadata-current",
        "bind-mounts-safe",
        "selected-services-matched",
    ]
    assert {check["status"] for check in preflight["checks"]} == {"PASS"}


def test_plan_apply_preflight_ignores_unselected_compose_render_failure(
    tmp_path: Path,
) -> None:
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
        "selected",
        [("app", "repo/app:latest", "cid-app")],
    )
    _make_fake_stack(
        tmp_path,
        fake_root,
        "broken",
        [("ignored", "repo/ignored:latest", "cid-ignored")],
    )
    (fake_root / "stacks" / "broken" / "config_fail").write_text(
        "",
        encoding="utf-8",
    )
    (fake_root / "stacks" / "broken" / "config_stderr").write_text(
        "broken compose config\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    checks = {check["code"]: check for check in body["apply_preflight"]["checks"]}
    assert body["status"] == "ready"
    assert body["can_apply"] is True
    assert checks["compose-renders"]["status"] == "PASS"
    assert "broken compose config" not in json.dumps(body["apply_preflight"])


def test_plan_endpoint_normalizes_digest_line_when_pinning_disabled(
    tmp_path: Path,
) -> None:
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
    wud_file.write_text("repo/app:latest@sha256:new\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )

    response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    line = body["stacks"][0]["lines"][0]
    assert body["digest_pin_updates"] is False
    assert body["stacks"][0]["digest_pin_updates"] == []
    assert body["stacks"][0]["digest_unpin_updates"] == []
    assert line["image"] == "repo/app:latest@sha256:new"
    assert line["resolved_image"] == "repo/app:latest"
    assert line["target_image"] == "repo/app:latest"
    assert line["action"] == "update"
    assert not any(
        action["kind"] == "compose-digest-pin"
        for action in body["stacks"][0]["actions"]
    )


def test_plan_endpoint_returns_unmatched_cleanup_preview(
    tmp_path: Path,
) -> None:
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
    wud_file.write_text("homarr-labs/homarr:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "active",
        [("app", "repo/app:latest", "cid-app")],
    )
    archived = tmp_path / "docker" / "homarr" / "docker-compose.archive.yml"
    archived.parent.mkdir(parents=True)
    archived.write_text(
        "services:\n  homarr:\n    image: ghcr.io/homarr-labs/homarr:latest\n",
        encoding="utf-8",
    )
    with (fake_root / "containers.tsv").open("a", encoding="utf-8") as file:
        file.write("homarr\tghcr.io/homarr-labs/homarr:latest\n")
    _write_fake_container_labels(
        fake_root,
        "homarr",
        {
            "com.docker.compose.project": "homarr",
            "com.docker.compose.project.working_dir": str(tmp_path / "docker" / "homarr"),
            "com.docker.compose.project.config_files": str(
                tmp_path / "docker" / "homarr" / "docker-compose.yml"
            ),
            "com.docker.compose.service": "homarr",
        },
    )

    response = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["can_apply"] is False
    assert body["issues"][0]["code"] == "compose-label-active-file-missing"
    assert "homarr/docker-compose.archive.yml" in body["issues"][0]["message"]
    assert body["issues"][0]["hint"]
    assert set(body["cleanup"]) == {"items"}
    assert body["cleanup"]["items"][0]["line_no"] == 1
    assert body["cleanup"]["items"][0]["raw"] == "homarr-labs/homarr:latest"
    cleanup_diagnostic = body["cleanup"]["items"][0]["diagnostic"]
    assert cleanup_diagnostic["stack"] == "homarr"
    assert (
        "The active Compose file was renamed to an archived or nonstandard filename."
        in cleanup_diagnostic["details"]["possible_reasons"]
    )
    assert (
        "Update Docker base or ignore paths if the stack moved."
        in cleanup_diagnostic["details"]["recommended_actions"]
    )
    assert str(tmp_path) not in json.dumps(body["cleanup"])
    assert wud_file.read_text(encoding="utf-8") == "homarr-labs/homarr:latest\n"


def test_plan_endpoint_rejects_invalid_or_non_actionable_lines(
    tmp_path: Path,
) -> None:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true", **fake_env})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text("# ignored\nrepo/app:latest\n", encoding="utf-8")
    _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", "repo/app:latest", "cid-app")],
    )
    headers = _csrf_headers(client)

    zero = client.post(
        "/api/v1/plans",
        json={"line_numbers": [0]},
        headers=headers,
    )
    comment = client.post(
        "/api/v1/plans",
        json={"line_numbers": [1]},
        headers=headers,
    )
    missing = client.post(
        "/api/v1/plans",
        json={"line_numbers": [3]},
        headers=headers,
    )

    assert zero.status_code == 422
    assert comment.status_code == 422
    assert "actionable WUD target lines" in comment.json()["detail"]
    assert missing.status_code == 422
    assert "actionable WUD target lines" in missing.json()["detail"]
