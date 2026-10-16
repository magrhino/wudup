from __future__ import annotations

from unittest import mock

import pytest
from tests.web_test_helpers import _fake_docker_env, _wait_apply_job

from wudup.container_identity import docker_endpoint, resolve_docker_endpoint


class _Response:
    status_code = 200

    def __init__(self, body: dict[str, object]) -> None:
        self._body = body

    def json(self) -> dict[str, object]:
        return self._body


class _StuckJobClient:
    def get(self, _url: str) -> _Response:
        return _Response(
            {
                "job_id": "job-1",
                "status": "running",
                "progress": [
                    {"phase": "pull", "status": "success", "message": "Pulled."},
                    {"phase": "health", "status": "running", "message": "Waiting."},
                ],
            }
        )


def test_wait_apply_job_timeout_reports_last_status_and_progress() -> None:
    with pytest.raises(AssertionError) as excinfo:
        _wait_apply_job(_StuckJobClient(), "job-1", timeout_seconds=0.05)

    message = str(excinfo.value)
    assert "apply job job-1 did not finish within 0.05s" in message
    assert "last status='running'" in message
    assert "health=running: Waiting." in message


def test_fake_docker_host_is_not_resolved(tmp_path) -> None:
    env, _fake_root = _fake_docker_env(tmp_path)

    with mock.patch("socket.getaddrinfo", side_effect=AssertionError("real DNS lookup")):
        endpoint = docker_endpoint(env)
        assert endpoint is not None
        endpoint = resolve_docker_endpoint(endpoint)

    assert endpoint.host == "docker"
    assert endpoint.addresses == frozenset()
