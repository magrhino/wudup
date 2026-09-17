from __future__ import annotations

import asyncio
import errno
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import uvicorn
from tests.web_test_helpers import (
    _client,
    _doctor_client,
    _web_env,
)

from wudup import web as web_module
from wudup import web_startup


def test_web_startup_rejects_bind_host_missing_from_allowed_hosts(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    for key, value in _web_env(
        tmp_path,
        {"WUD_WEB_DEV_NO_AUTH": "true"},
    ).items():
        monkeypatch.setenv(key, value)

    status = web_module.run_web_from_namespace(
        SimpleNamespace(
            base=None,
            file=None,
            log_dir=None,
            db_path=None,
            host="192.0.2.10",
            port=None,
            static_dir=None,
        )
    )
    stderr = capsys.readouterr().err

    assert status == 1
    assert "WUD_WEB_PUBLIC_ORIGIN" in stderr
    assert "WUD_WEB_ALLOWED_HOSTS" in stderr


@pytest.mark.parametrize("host", [None, "0.0.0.0"])
def test_web_startup_prints_first_run_summary(
    tmp_path: Path,
    monkeypatch,
    capsys,
    host,
) -> None:
    secret = "github-token-secret"
    for key, value in _web_env(
        tmp_path,
        {
            "GITHUB_TOKEN": secret,
            "WUD_SCRIPT_SYNC_STATUS": "auto-detected",
            "WUD_WEB_HOST": "0.0.0.0",
        },
    ).items():
        monkeypatch.setenv(key, value)
    uvicorn_calls = []
    monkeypatch.setattr(
        web_startup,
        "run_web_server",
        lambda app, host, port: uvicorn_calls.append((app, host, port)),
    )

    status = web_module.run_web_from_namespace(
        SimpleNamespace(
            base=None,
            file=None,
            log_dir=None,
            db_path=None,
            host=host,
            port=12735,
            static_dir=None,
        )
    )
    stderr = capsys.readouterr().err

    assert status == 0
    assert uvicorn_calls
    assert uvicorn_calls[0][1:] == ("0.0.0.0", 12735)
    assert "WUDup WebUI startup summary" in stderr
    assert "Setup link: http://127.0.0.1:12735/#/setup?claim=" in stderr
    assert f"Docker base: {tmp_path / 'docker'}" in stderr
    assert f"WUD output: {tmp_path / 'state' / 'images.todo'}" in stderr
    assert "Script sync: auto-detected writable /managed-wud" in stderr
    assert "Doctor: docker compose exec wudup doctor" in stderr
    assert secret not in stderr


def test_listener_failure_has_actionable_startup_error(tmp_path, monkeypatch, capsys):
    for key, value in _web_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        web_startup, "run_web_server",
        MagicMock(side_effect=OSError(errno.EADDRINUSE, "Address already in use")),
    )

    status = web_module.run_web_from_namespace(
        SimpleNamespace(host="0.0.0.0", port=7417),
    )

    assert status == 1
    assert "Check that the bind address is available" in capsys.readouterr().err


def test_web_startup_summary_uses_public_origin_when_setup_not_required(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    for key, value in _web_env(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_PUBLIC_ORIGIN": "https://wud.example.test",
            "WUD_SCRIPT_SYNC_STATUS": "auto-not-detected",
        },
    ).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(web_startup, "run_web_server", lambda app, host, port: None)

    status = web_module.run_web_from_namespace(
        SimpleNamespace(
            base=None,
            file=None,
            log_dir=None,
            db_path=None,
            host="0.0.0.0",
            port=12735,
            static_dir=None,
        )
    )
    stderr = capsys.readouterr().err

    assert status == 0
    assert "Web URL: https://wud.example.test/" in stderr
    assert "Setup link:" not in stderr
    assert "Script sync: auto mode did not detect writable /managed-wud" in stderr


def test_script_sync_summary_treats_explicit_auto_as_auto(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scripts_dir = tmp_path / "managed-wud"
    scripts_dir.mkdir()
    monkeypatch.setattr(web_startup, "DEFAULT_CONTAINER_SCRIPTS_DIR", str(scripts_dir))

    summary = web_startup._script_sync_summary({"WUD_SYNC_SCRIPTS": "auto"})

    assert summary.startswith(f"auto fallback sees writable {scripts_dir}")


def test_script_sync_summary_reports_legacy_disabled() -> None:
    summary = web_startup._script_sync_summary({"WUDUP_LEGACY_SCRIPTS": "FALSE"})

    assert summary == "disabled by WUDUP_LEGACY_SCRIPTS"


def test_static_spa_mount_serves_index_when_configured(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><div>spa</div>")
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_STATIC_DIR": str(static_dir),
        },
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "spa" in response.text


@pytest.mark.parametrize(
    "host,family", [
        ("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6),
        ("::", socket.AF_INET6), ("192.0.2.10", socket.AF_INET),
    ],
)
def test_explicit_listener_address_is_preserved(monkeypatch, host, family) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (family, socket.SOCK_STREAM, 0, "", (host, 7417)),
    ])
    listener = MagicMock()
    listener.__enter__.return_value = listener
    create = MagicMock(return_value=listener)
    monkeypatch.setattr(socket, "create_server", create)
    run = MagicMock()
    monkeypatch.setattr(uvicorn.Server, "run", run)
    app = MagicMock()

    web_startup.run_web_server(app, host=host, port=7417)

    create.assert_called_once_with((host, 7417), family=family)
    run.assert_called_once_with(sockets=[listener])
    listener.__exit__.assert_called_once()


def test_hostname_binds_unique_addresses_and_preserves_ipv6_scope(monkeypatch):
    ipv4_info = (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 7417))
    ipv6_info = (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("fe80::1", 7417, 0, 7))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        ipv4_info, ipv6_info, ipv4_info,
    ])
    ipv4, ipv6 = MagicMock(), MagicMock()
    for listener in (ipv4, ipv6):
        listener.__enter__.return_value = listener
        listener.getsockname.return_value = ("", 7417)
    create = MagicMock(side_effect=[ipv4, ipv6])
    monkeypatch.setattr(socket, "create_server", create)
    run = MagicMock()
    monkeypatch.setattr(uvicorn.Server, "run", run)

    web_startup.run_web_server(MagicMock(), host="web.example.test", port=7417)

    assert create.call_count == 2
    assert create.call_args_list[0].args == (("127.0.0.1", 7417),)
    assert create.call_args_list[1].args == (("fe80::1", 7417, 0, 7),)
    run.assert_called_once_with(sockets=[ipv4, ipv6])
    for listener in (ipv4, ipv6):
        listener.__exit__.assert_called_once()


def test_server_system_exit_is_preserved_and_listener_closed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 7417)),
    ])
    listener = MagicMock()
    listener.__enter__.return_value = listener
    monkeypatch.setattr(socket, "create_server", MagicMock(return_value=listener))
    monkeypatch.setattr(uvicorn.Server, "run", MagicMock(side_effect=SystemExit(7)))
    app = MagicMock()

    with pytest.raises(SystemExit) as error:
        web_startup.run_web_server(app, host="127.0.0.1", port=7417)

    assert error.value.code == 7
    listener.__exit__.assert_called_once()


@pytest.mark.parametrize("host,family", [
    ("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6),
])
def test_occupied_explicit_bind_reports_actionable_error(
    tmp_path, monkeypatch, capsys, host, family,
) -> None:
    for key, value in _web_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    try:
        occupied = socket.create_server((host, 0), family=family)
    except OSError as exc:
        if family == socket.AF_INET6 and exc.errno in {
            errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL,
        }:
            pytest.skip("IPv6 loopback is unavailable")
        raise

    with occupied:
        status = web_module.run_web_from_namespace(
            SimpleNamespace(host=host, port=occupied.getsockname()[1]),
        )

    assert status == 1
    stderr = capsys.readouterr().err
    assert "The WebUI could not open its listening port" in stderr
    assert "Check that the bind address is available and the port is not in use" in stderr


@pytest.mark.parametrize(
    "error", [errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL],
)
def test_ipv6_unavailable_keeps_ipv4_and_closes_listener(
    monkeypatch, caplog, error,
) -> None:
    ipv4 = MagicMock()
    ipv4.__enter__.return_value = ipv4
    ipv4.getsockname.return_value = ("0.0.0.0", 7417)
    create = MagicMock(side_effect=[ipv4, OSError(error, "IPv6 unavailable")])
    monkeypatch.setattr(socket, "create_server", create)
    run = MagicMock()
    monkeypatch.setattr(uvicorn.Server, "run", run)

    web_startup.run_web_server(MagicMock(), host="0.0.0.0", port=7417)

    run.assert_called_once_with(sockets=[ipv4])
    ipv4.__exit__.assert_called_once()
    assert "A WebUI listening address is unavailable" in caplog.text


def test_ipv6_port_conflict_fails_and_closes_ipv4(monkeypatch) -> None:
    ipv4 = MagicMock()
    ipv4.__enter__.return_value = ipv4
    ipv4.getsockname.return_value = ("0.0.0.0", 7417)
    monkeypatch.setattr(
        socket, "create_server",
        MagicMock(side_effect=[ipv4, OSError(errno.EADDRINUSE, "Port in use")]),
    )
    run = MagicMock()
    monkeypatch.setattr(uvicorn.Server, "run", run)
    app = MagicMock()

    with pytest.raises(OSError, match="Port in use"):
        web_startup.run_web_server(app, host="0.0.0.0", port=7417)

    run.assert_not_called()
    ipv4.__exit__.assert_called_once()


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "127.0.0.1", "::1"])
def test_listener_serves_health_on_configured_families(
    tmp_path, monkeypatch, bind_host,
) -> None:
    try:
        with socket.create_server(("::1", 0), family=socket.AF_INET6):
            pass
    except OSError as exc:
        if exc.errno in {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}:
            pytest.skip("IPv6 loopback is unavailable")
        raise

    app = _doctor_client(tmp_path).app
    bound = []

    async def exercise(server, sockets):
        task = asyncio.create_task(server.serve(sockets=sockets))
        try:
            for _ in range(200):
                if server.started or task.done():
                    break
                await asyncio.sleep(0.01)
            assert server.started
            port = sockets[0].getsockname()[1]
            assert all(sock.getsockname()[1] == port for sock in sockets)
            hosts = ["[::1]" if sock.family == socket.AF_INET6 else "127.0.0.1"
                     for sock in sockets]
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                for host in hosts:
                    for path in ("/healthz", "/readyz"):
                        response = await client.get(
                            f"http://{host}:{port}{path}", headers={"Host": "localhost"},
                        )
                        assert response.status_code == 200, response.text
                response = await client.get(
                    f"http://{hosts[0]}:{port}/healthz",
                    headers={"Host": "untrusted.example"},
                )
                assert response.status_code == 400
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)

    def run(server, sockets):
        bound.extend(sockets)
        expected_families = {
            "0.0.0.0": [socket.AF_INET, socket.AF_INET6],
            "127.0.0.1": [socket.AF_INET],
            "::1": [socket.AF_INET6],
        }
        assert [sock.family for sock in sockets] == expected_families[bind_host]
        asyncio.run(exercise(server, sockets))

    monkeypatch.setattr(uvicorn.Server, "run", run)
    # Dockerfile supplies this wildcard by default; operators need no override.
    web_startup.run_web_server(app, host=bind_host, port=0)
    assert all(sock.fileno() == -1 for sock in bound)
