from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest
from starlette.requests import Request

from wudup import command, request_scope, web_request_scope
from wudup.command import CommandRunner


def _counting_docker(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "calls.log"
    docker = tmp_path / "bin" / "docker"
    docker.parent.mkdir()
    docker.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nprintf "ok\\n"\n',
        encoding="utf-8",
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    return docker, log


def _calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.parametrize(
    ("argv", "reusable"),
    [
        (("docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"), True),
        (("docker", "compose", "--project-directory", "/srv/app", "-f", "c.yml", "ps", "-q", "app"), True),
        (("docker", "compose", "version"), True),
        (("docker", "inspect", "-f", "{{.Id}}", "cid"), True),
        (("docker", "image", "inspect", "--format", "{{.Id}}", "repo/app:1"), True),
        (("docker", "ps", "--all", "--format", "{{.Names}}"), True),
        (("/usr/bin/docker", "version"), True),
        (("docker", "compose", "-f", "docker-compose.yml", "pull", "app"), False),
        (("docker", "compose", "-f", "docker-compose.yml", "up", "-d", "app"), False),
        (("docker", "compose", "-f", "config", "up", "-d"), False),
        (("docker", "pull", "repo/app:1"), False),
        (("docker", "restart", "cid"), False),
        (("docker", "manifest", "inspect", "repo/app:1"), False),
        # Options WUDup never passes fail closed even when their value is a read verb.
        (("docker", "--context", "ps", "run", "repo/app:1"), False),
        (("docker", "-H", "inspect", "restart", "cid"), False),
        (("docker", "--host", "tcp://docker:2375", "ps"), False),
        (("docker", "--config", "/tmp/cfg", "inspect", "cid"), False),
        (("docker", "compose", "--profile", "ps", "up", "-d"), False),
        (("docker", "compose", "--ansi", "never", "config"), False),
        (("docker", "compose", "-f", "c.yml", "--profile", "config", "pull"), False),
        (("docker", "image", "rm", "repo/app:1"), False),
        (("docker", "image"), False),
        (("trivy", "image", "repo/app:1"), False),
        ((), False),
    ],
)
def test_only_read_only_docker_commands_are_reusable(
    argv: tuple[str, ...],
    reusable: bool,
) -> None:
    assert command._is_reusable_read(argv) is reusable


def test_reuse_needs_a_read_only_request_scope() -> None:
    computed: list[int] = []

    def compute() -> int:
        computed.append(1)
        return len(computed)

    assert request_scope.reuse_read("key", compute) == 1
    assert request_scope.reuse_read("key", compute) == 2

    token = request_scope.begin(reuse_reads=False)
    try:
        assert request_scope.reuse_read("key", compute) == 3
        assert request_scope.reuse_read("key", compute) == 4
    finally:
        request_scope.end(token)

    token = request_scope.begin(reuse_reads=True)
    try:
        assert request_scope.reuse_read("key", compute) == 5
        assert request_scope.reuse_read("key", compute) == 5
        assert request_scope.reuse_read("other", compute) == 6
    finally:
        scope = request_scope.end(token)
    assert scope is not None
    assert scope.reused == 1
    assert request_scope.current() is None


def test_reuse_stops_when_the_request_scope_ends() -> None:
    import contextvars

    computed: list[int] = []

    def compute() -> int:
        computed.append(1)
        return len(computed)

    token = request_scope.begin(reuse_reads=True)
    outliving_context = contextvars.copy_context()
    assert request_scope.reuse_read("key", compute) == 1
    request_scope.end(token)

    # A streamed response body keeps running in a copy of the request context.
    assert outliving_context.run(request_scope.reuse_read, "key", compute) == 2
    assert outliving_context.run(request_scope.reuse_read, "key", compute) == 3


def test_capture_reuses_reads_but_never_mutations_within_a_request(
    tmp_path: Path,
) -> None:
    docker, log = _counting_docker(tmp_path)
    runner = CommandRunner(env={"PATH": os.environ.get("PATH", "")})
    read = (str(docker), "compose", "-f", "docker-compose.yml", "config", "--format", "json")
    write = (str(docker), "compose", "-f", "docker-compose.yml", "pull", "app")

    token = request_scope.begin(reuse_reads=True)
    try:
        first = runner.capture(read, cwd=tmp_path)
        second = runner.capture(read, cwd=tmp_path)
        runner.capture(read, cwd=tmp_path / "bin")
        runner.capture(write, cwd=tmp_path)
        runner.capture(write, cwd=tmp_path)
    finally:
        scope = request_scope.end(token)

    assert first is second
    assert _calls(log) == [
        "compose -f docker-compose.yml config --format json",
        "compose -f docker-compose.yml config --format json",
        "compose -f docker-compose.yml pull app",
        "compose -f docker-compose.yml pull app",
    ]
    assert scope is not None
    assert scope.phases["docker"].count == 4
    assert scope.reused == 1

    runner.capture(read, cwd=tmp_path)
    assert len(_calls(log)) == 5


def test_capture_reuses_failed_reads_and_still_raises(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    docker = tmp_path / "docker"
    docker.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 3\n',
        encoding="utf-8",
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    runner = CommandRunner()
    argv = (str(docker), "inspect", "cid")

    token = request_scope.begin(reuse_reads=True)
    try:
        for _ in range(2):
            with pytest.raises(command.CommandError):
                runner.capture(argv)
        assert runner.capture(argv, check=False).returncode == 3
    finally:
        request_scope.end(token)

    assert _calls(log) == ["inspect cid"]


def test_timing_is_recorded_from_worker_threads_that_copy_the_context() -> None:
    import contextvars

    token = request_scope.begin(reuse_reads=False)
    try:
        context = contextvars.copy_context()

        def work() -> None:
            with request_scope.timed("docker"):
                pass

        thread = threading.Thread(target=context.run, args=(work,))
        thread.start()
        thread.join()
    finally:
        scope = request_scope.end(token)

    assert scope is not None
    assert scope.phases["docker"].count == 1


def test_server_timing_header_lists_phases_and_reuse() -> None:
    assert request_scope.server_timing_header(None) == ""
    scope = request_scope.RequestScope(reuse_reads=True)
    assert request_scope.server_timing_header(scope) == ""

    scope.record("wud", 0.0125)
    scope.record("docker", 0.25)
    scope.record("docker", 0.5)
    scope.reused = 3

    assert request_scope.server_timing_header(scope) == (
        'docker;dur=750.0;desc="2 calls", '
        'wud;dur=12.5;desc="1 calls", '
        'reused;desc="3 reads"'
    )


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


@pytest.mark.parametrize(
    ("method", "path", "reuses"),
    [
        ("GET", "/api/v1/pending", True),
        ("HEAD", "/api/v1/status", True),
        ("POST", "/api/v1/plans", True),
        ("POST", "/api/v1/plans/", False),
        ("POST", "/prefix/api/v1/plans", False),
        ("POST", "/api/v1/future-plans", False),
        ("POST", "/api/v1/plans/apply", False),
        ("POST", "/api/v1/retag-plans", False),
        ("POST", "/api/v1/jobs", False),
        ("POST", "/api/v1/self-update", False),
        ("POST", "/api/v1/self-update/prepare", False),
        ("POST", "/api/v1/tracking-repairs/apply", False),
        ("POST", "/api/v1/doctor", False),
        ("POST", "/api/v1/container/restart", False),
        ("POST", "/api/v1/pending/rescan", False),
        ("PUT", "/api/v1/plans", False),
        ("DELETE", "/api/v1/plans", False),
    ],
)
def test_only_reads_and_plan_preview_reuse_docker_reads(
    method: str,
    path: str,
    reuses: bool,
) -> None:
    assert web_request_scope.reuses_reads(_request(method, path)) is reuses


def test_jobs_submitted_from_a_request_run_without_its_scope() -> None:
    from concurrent.futures import ThreadPoolExecutor

    token = request_scope.begin(reuse_reads=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            job_scope = executor.submit(request_scope.current).result()
    finally:
        request_scope.end(token)

    assert job_scope is None


def test_server_timing_header_is_built_from_a_locked_snapshot() -> None:
    scope = request_scope.RequestScope(reuse_reads=False)
    scope.record("docker", 0.001)
    acquired = scope._lock.acquire(blocking=False)
    assert acquired
    result: list[str] = []
    thread = threading.Thread(
        target=lambda: result.append(request_scope.server_timing_header(scope))
    )
    thread.start()
    thread.join(timeout=0.2)
    # The header waits for the lock instead of reading phases mid-update.
    assert thread.is_alive()
    scope._lock.release()
    thread.join(timeout=2)
    assert result == ['docker;dur=1.0;desc="1 calls"']
