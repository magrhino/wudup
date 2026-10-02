from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.health_gate_test_helpers import (
    FakeHealthCompose,
    FakeHealthDocker,
    health_stack,
)

from wudup.updater_lifecycle_health import (
    ALL_SERVICES_LOOKUP,
    _LifecycleHealthMixin,
    health_gate_passed,
    running_service_containers,
)


class _RecordingLog:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def error(self, message: str) -> None:
        self.lines.append(message)

    def plain(self, level: str, message: str) -> None:
        self.lines.append(f"{level} {message}")

    def text(self) -> str:
        return "\n".join(self.lines)


class _HealthHarness(_LifecycleHealthMixin):
    def __init__(
        self,
        compose: FakeHealthCompose,
        docker: FakeHealthDocker | None = None,
    ) -> None:
        self.compose = compose
        self.docker = docker or FakeHealthDocker()
        self.log = _RecordingLog()
        self.options = SimpleNamespace(max_wait=0)
        self.progress: list[str] = []

    def _progress(self, phase: str, status: str, message: str, **_: object) -> None:
        self.progress.append(f"{phase}:{status}")


def test_whole_stack_lookup_failure_is_reported_as_lookup_failure(
    tmp_path: Path,
) -> None:
    compose = FakeHealthCompose({"app": ["cid-app"]}, fail_all=True)
    harness = _HealthHarness(compose)

    assert running_service_containers(compose, health_stack(tmp_path), None) == (
        [],
        [],
        [ALL_SERVICES_LOOKUP],
    )
    assert harness._wait_for_health(health_stack(tmp_path), None) is False

    log = harness.log.text()
    assert (
        "Health blocker: could not list containers for service(s): "
        "(all services) because `docker compose ps` failed"
    ) in log
    assert "health: service=(all services) container lookup failed" in log
    assert "returned no containers" not in log
    assert harness.progress[-1] == "health:failure"


def test_whole_stack_without_containers_reports_empty_lookup(tmp_path: Path) -> None:
    harness = _HealthHarness(FakeHealthCompose({"app": []}))

    assert harness._wait_for_health(health_stack(tmp_path), None) is False

    log = harness.log.text()
    assert "Health blocker: docker compose ps -q returned no containers" in log
    assert "could not list containers" not in log


def test_health_details_name_failed_missing_and_unhealthy_services(
    tmp_path: Path,
) -> None:
    compose = FakeHealthCompose(
        {"app": ["cid-app"], "db": [], "worker": []}, failing=["db"]
    )
    docker = FakeHealthDocker(
        summaries={"cid-app": "/app|running|unhealthy|2|0"},
        health_logs={"cid-app": ["probe failed"]},
    )
    harness = _HealthHarness(compose, docker)

    assert harness._wait_for_health(health_stack(tmp_path), ["app", "db", "worker"]) is False

    log = harness.log.text()
    assert "could not list containers for service(s): db" in log
    assert "no running container for service(s): worker" in log
    assert "health: service=db container lookup failed" in log
    assert "health: service=worker has no running container" in log
    assert "health: container=app status=running health=unhealthy restarts=2" in log
    assert "health_output[app]: probe failed" in log


def test_non_health_failure_details_skip_intentionally_stopped_service(
    tmp_path: Path,
) -> None:
    harness = _HealthHarness(FakeHealthCompose({"app": ["cid-app"], "worker": []}))

    details = harness._capture_health_details(health_stack(tmp_path), ["app", "worker"])

    assert "health: container=cid-app status=running health=healthy" in details
    assert "service=worker" not in details


def test_wait_for_health_succeeds_when_every_selected_service_is_healthy(
    tmp_path: Path,
) -> None:
    harness = _HealthHarness(
        FakeHealthCompose({"app": ["cid-app"], "worker": ["cid-worker"]})
    )

    assert harness._wait_for_health(health_stack(tmp_path), ["app", "worker"]) is True
    assert harness.log.lines[-1].endswith("Health wait succeeded in 0s")
    assert harness.progress == ["health:running", "health:success"]


@pytest.mark.parametrize(
    ("cids", "missing", "failed", "summaries", "expected"),
    [
        (["a"], [], [], {"a": "/a|running|healthy|0|0"}, True),
        ([], [], [], {}, False),
        (["a"], ["b"], [], {"a": "/a|running|healthy|0|0"}, False),
        (["a"], [], ["b"], {"a": "/a|running|healthy|0|0"}, False),
        (["a"], [], [], {"a": ""}, False),
        (["a"], [], [], {"a": "/a|running|unhealthy|0|0"}, False),
    ],
    ids=["healthy", "none", "missing", "failed", "no-state", "unhealthy"],
)
def test_health_gate_passed(
    cids: list[str],
    missing: list[str],
    failed: list[str],
    summaries: dict[str, str],
    expected: bool,
) -> None:
    assert health_gate_passed(cids, missing, failed, summaries.__getitem__) is expected
