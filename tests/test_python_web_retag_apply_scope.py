from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.web_retag_test_helpers import (
    _apply_retag_plan,
    _create_retag_plan,
    _make_retag_fixture,
    _RetagFixture,
    _wait_run_status,
)
from tests.web_test_helpers import (
    _csrf_headers,
    _fake_docker_calls,
    _wait_apply_job,
)

from wudup.command import CommandError, CommandResult
from wudup.compose import ComposeStack, ServiceImage
from wudup.docker_cli import DockerCli
from wudup.web_retag_scope import RetagScopeBlocked, validate_retag_recreate_scope

_LIVE_ENV = {
    "WUD_WEB_MUTATIONS_ENABLED": "true",
    "WUD_UPDATE_MODE": "live",
    "WUD_MAX_WAIT": "0",
}
# The retag fake Docker environment sets DOCKER_HOST=tcp://docker:2375.
_FAKE_ENDPOINT_HOST = "docker"
_MUTATION_CALLS = re.compile(r"compose -f \S+ (?:pull|stop|pause|up -d)")


def _command_error() -> CommandError:
    return CommandError(CommandResult(("docker", "inspect"), 1, "", "boom"))


def _assert_blocked_before_mutation(
    tmp_path: Path,
    fixture: _RetagFixture,
    before: str,
    job: dict[str, object],
    *,
    services: list[str],
) -> None:
    assert job["status"] == "failure"
    # Nothing was attempted, so the block is reported as a preflight failure.
    progress = job["progress"][-1]
    assert progress["phase"] == "preflight"
    assert progress["status"] == "failure"
    assert progress["stack"] == "stack"
    assert progress["services"] == services
    assert (fixture.compose_dir / "docker-compose.yml").read_text(
        encoding="utf-8"
    ) == before
    assert not _MUTATION_CALLS.search(_fake_docker_calls(fixture.fake_root))
    run = _wait_run_status(tmp_path / "state" / "wud.sqlite", job["run_id"], "failure")
    assert run["status"] == "failure"


def _apply(fixture: _RetagFixture) -> tuple[str, dict[str, object]]:
    headers = _csrf_headers(fixture.client)
    plan = _create_retag_plan(fixture.client, headers)
    before = (fixture.compose_dir / "docker-compose.yml").read_text(encoding="utf-8")
    response = _apply_retag_plan(fixture.client, headers, plan)
    assert response.status_code == 202
    return before, _wait_apply_job(fixture.client, response.json()["job_id"])


def test_retag_apply_blocks_service_serving_docker_endpoint(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(tmp_path, env=_LIVE_ENV)
    (fixture.fake_root / "containers" / "cid-app.dns.json").write_text(
        json.dumps({
            "Id": "cid-app",
            "Name": "/stack-app-1",
            "Service": "app",
            "Networks": {"stack": {"Aliases": [_FAKE_ENDPOINT_HOST]}},
        }),
        encoding="utf-8",
    )

    before, job = _apply(fixture)

    _assert_blocked_before_mutation(tmp_path, fixture, before, job, services=["app"])
    assert "[stack] This update would stop or recreate app, which serves" in job["error"]
    assert f"(DOCKER_HOST host {_FAKE_ENDPOINT_HOST})" in job["error"]
    assert "Update that service from the host instead" in job["error"]


def test_retag_apply_fails_closed_when_endpoint_inspect_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(tmp_path, env=_LIVE_ENV)

    def fail_inspect(self: DockerCli, container: str) -> dict[str, object]:
        raise _command_error()

    monkeypatch.setattr(DockerCli, "container_dns_details", fail_inspect)

    before, job = _apply(fixture)

    _assert_blocked_before_mutation(tmp_path, fixture, before, job, services=[])
    assert "Could not check whether this update would stop the Docker endpoint" in (
        job["error"]
    )


def test_retag_apply_blocks_wudup_container_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path, env={**_LIVE_ENV, "WUD_WEB_RESTART_CONTAINER": "wudup"}
    )
    monkeypatch.setattr(
        DockerCli,
        "container_id",
        lambda self, container: "cid-app" if container == "wudup" else "",
    )

    before, job = _apply(fixture)

    _assert_blocked_before_mutation(tmp_path, fixture, before, job, services=["app"])
    assert "[stack] This update would stop or recreate WUDup itself." in job["error"]
    assert "Use the self-update action for WUDup" in job["error"]


def test_retag_apply_unrelated_service_still_applies(tmp_path: Path) -> None:
    fixture = _make_retag_fixture(tmp_path, env=_LIVE_ENV)

    _before, job = _apply(fixture)

    assert job["status"] == "success"
    calls = _fake_docker_calls(fixture.fake_root)
    assert "inspect cid-app" in calls
    assert "compose -f docker-compose.yml pull app" in calls


def test_retag_apply_unix_socket_docker_host_skips_endpoint_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _make_retag_fixture(
        tmp_path,
        env={**_LIVE_ENV, "WUD_WEB_RESTART_CONTAINER": "wudup"},
        docker_host="unix:///var/run/docker.sock",
    )
    monkeypatch.setattr(
        DockerCli,
        "container_id",
        lambda self, container: "cid-wudup" if container == "wudup" else "",
    )

    def unexpected_inspect(self: DockerCli, container: str) -> dict[str, object]:
        raise AssertionError("a unix-socket DOCKER_HOST needs no endpoint inspect")

    monkeypatch.setattr(DockerCli, "container_dns_details", unexpected_inspect)

    _before, job = _apply(fixture)

    assert job["status"] == "success"
    calls = _fake_docker_calls(fixture.fake_root)
    assert "compose -f docker-compose.yml ps -q app" in calls
    assert "compose -f docker-compose.yml pull app" in calls


class _FakeCompose:
    def __init__(self, container_ids: dict[str, list[str]] | None = None) -> None:
        self.container_ids = container_ids or {}
        self.ps_calls: list[tuple[str, ...]] = []

    def ps_quiet_checked(self, directory, file, services, *, project_directory=None):
        self.ps_calls.append(tuple(services))
        return list(self.container_ids.get(Path(directory).name, []))


class _FakeDocker:
    def __init__(
        self,
        *,
        container_id: str = "",
        dns: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self._container_id = container_id
        self._dns = dns or {}

    def container_id(self, container: str) -> str:
        if not self._container_id:
            raise _command_error()
        return self._container_id

    def container_dns_details(self, container: str) -> dict[str, object]:
        return self._dns[container]


def _stack(*services: tuple[str, str]) -> ComposeStack:
    return ComposeStack(
        index=0,
        directory=Path("/srv/stack"),
        file="docker-compose.yml",
        name="stack",
        images=tuple(image for _service, image in services),
        service_images=tuple(ServiceImage(service, image) for service, image in services),
    )


def _settings(docker_host: str = "", restart_container: str = "") -> SimpleNamespace:
    env = {"DOCKER_HOST": docker_host} if docker_host else {}
    return SimpleNamespace(command_env=env, restart_container=restart_container)


def test_scope_blocks_wudup_image_without_container_identity() -> None:
    stack = _stack(("wud", "ghcr.io/magrhino/wudup:latest"), ("app", "repo/app:1"))
    compose = _FakeCompose()
    settings = _settings()
    docker = _FakeDocker()

    with pytest.raises(RetagScopeBlocked, match=r"would stop or recreate WUDup itself"):
        validate_retag_recreate_scope(settings, compose, docker, [(stack, ("wud",))])
    validate_retag_recreate_scope(settings, compose, docker, [(stack, ("app",))])
    assert compose.ps_calls == []


def test_scope_unix_socket_docker_host_skips_endpoint_inspect() -> None:
    stack = _stack(("socket-proxy", "repo/socket-proxy:1"))
    compose = _FakeCompose({"stack": ["cid-proxy"]})

    # No DNS details are configured, so any inspect would raise KeyError.
    validate_retag_recreate_scope(
        _settings("unix:///var/run/docker.sock"),
        compose,
        _FakeDocker(),
        [(stack, ("socket-proxy",))],
    )
    assert compose.ps_calls == []


def test_scope_checks_every_stack_before_any_mutation() -> None:
    safe = _stack(("app", "repo/app:1"))
    proxy = ComposeStack(
        index=1,
        directory=Path("/srv/proxy"),
        file="docker-compose.yml",
        name="proxy",
        images=("repo/socket-proxy:1",),
        service_images=(ServiceImage("socket-proxy", "repo/socket-proxy:1"),),
    )
    compose = _FakeCompose({"stack": ["cid-app"], "proxy": ["cid-proxy"]})
    docker = _FakeDocker(dns={
        "cid-app": {"Id": "cid-app", "Name": "/stack-app-1", "Networks": {}},
        "cid-proxy": {
            "Id": "cid-proxy",
            "Name": "/proxy-socket-proxy-1",
            "Service": "socket-proxy",
            "Networks": {"proxy": {"Aliases": ["socket-proxy"]}},
        },
    })

    settings = _settings("tcp://Socket-Proxy:2375")
    stacks = [(safe, ("app",)), (proxy, ("socket-proxy",))]

    with pytest.raises(RetagScopeBlocked, match=r"^\[proxy\] .*socket-proxy, which serves"):
        validate_retag_recreate_scope(settings, compose, docker, stacks)
    assert compose.ps_calls == [("app",), ("socket-proxy",)]


def test_scope_fails_closed_when_wudup_identity_lookup_fails() -> None:
    stack = _stack(("app", "repo/app:1"))
    settings = _settings(restart_container="wudup")
    compose = _FakeCompose()
    docker = _FakeDocker()

    with pytest.raises(RetagScopeBlocked, match=r"Could not verify the WUDup container"):
        validate_retag_recreate_scope(settings, compose, docker, [(stack, ("app",))])
