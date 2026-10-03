"""Docker call budgets for WebUI read endpoints.

These pin how many Docker subprocesses each read endpoint runs for a known
number of stacks, so a change that repeats Docker work per request fails here
instead of making the WebUI slower unnoticed. Lower the budgets as reads get
cheaper; never raise them without a reason recorded in the change.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest
from tests.web_test_helpers import (
    _client,
    _csrf_headers,
    _fake_docker_env,
    _install_wud_api,
    _make_fake_stack,
    _wud_api_container,
)

STACKS = 3


def _docker_call_kinds(fake_root: Path) -> Counter[str]:
    kinds: Counter[str] = Counter()
    for line in (fake_root / "calls.log").read_text(encoding="utf-8").splitlines():
        args = line.split("\t", 1)[-1]
        if args.startswith("compose") and "config --format json" in args:
            kinds["compose config json"] += 1
        elif args.startswith("compose") and re.search(r"\bconfig\b", args):
            kinds["compose config"] += 1
        elif args.startswith("compose") and " ps -q" in args:
            kinds["compose ps"] += 1
        else:
            kinds[args.split(" ", 1)[0]] += 1
    return kinds


@pytest.fixture
def stacks_client(tmp_path: Path, monkeypatch):
    fake_env, fake_root = _fake_docker_env(tmp_path)
    containers = []
    for index in range(STACKS):
        _make_fake_stack(
            tmp_path,
            fake_root,
            f"stack{index}",
            [("app", f"repo/app{index}:1.0", f"cid{index}")],
        )
        containers.append(
            _wud_api_container(
                name=f"app{index}",
                image=f"repo/app{index}",
                tag="1.0",
                remote_tag="2.0",
            )
        )
    _install_wud_api(monkeypatch, containers=containers)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.call-budget.test:3000",
            **fake_env,
        },
    )
    return client, fake_root


def _measure(fake_root: Path, request) -> tuple[object, Counter[str]]:
    (fake_root / "calls.log").write_text("", encoding="utf-8")
    response = request()
    return response, _docker_call_kinds(fake_root)


def test_pending_runs_each_runtime_lookup_once_per_service(stacks_client) -> None:
    client, fake_root = stacks_client

    for _ in range(2):
        response, kinds = _measure(fake_root, lambda: client.get("/api/v1/pending"))

        assert response.status_code == 200
        assert response.json()["count"] == STACKS
        # A second request repeats every lookup: nothing is reused across
        # requests, so the page never shows container state from an older read.
        assert kinds == Counter(
            {
                "compose config json": STACKS,
                "compose ps": STACKS,
                "inspect": STACKS,
                "ps": 2,
            }
        )


def test_plan_preview_shares_compose_renders_with_its_doctor_preflight(
    stacks_client,
) -> None:
    client, fake_root = stacks_client
    headers = _csrf_headers(client)

    response, kinds = _measure(
        fake_root,
        lambda: client.post(
            "/api/v1/plans",
            json={"line_numbers": [1]},
            headers=headers,
        ),
    )

    assert response.status_code == 200
    assert response.json()["apply_preflight"]["checks"]
    assert kinds["compose config json"] == STACKS
    assert kinds["compose config"] == 0
    assert sum(kinds.values()) == STACKS + 3


@pytest.mark.parametrize("path", ["/api/v1/tracked-containers", "/api/v1/update-targets"])
def test_inventory_reads_render_each_stack_once(stacks_client, path: str) -> None:
    client, fake_root = stacks_client

    response, kinds = _measure(fake_root, lambda: client.get(path))

    assert response.status_code == 200
    assert kinds["compose config json"] == STACKS


def test_server_timing_reports_docker_work_only_for_requests_that_run_it(
    stacks_client,
) -> None:
    client, _fake_root = stacks_client

    pending = client.get("/api/v1/pending")
    session = client.get("/api/v1/auth/session")

    docker_timing = re.search(
        r'docker;dur=[0-9.]+;desc="(\d+) calls"',
        pending.headers["server-timing"],
    )
    assert docker_timing is not None
    assert int(docker_timing.group(1)) == 3 * STACKS + 2
    assert "server-timing" not in session.headers
