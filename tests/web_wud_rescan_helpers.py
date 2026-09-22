from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.web_test_helpers import _web_env

from wudup import web_wud_transport
from wudup.web import load_web_settings


def settings(tmp_path: Path, base_url: str):
    return load_web_settings(
        environ=_web_env(tmp_path, {"WUD_API_BASE_URL": base_url}),
    )


def container_payload(
    *,
    name: str = "app",
    image: str = "registry.example/acme/app",
    tag: str = "1.0.0",
    remote_tag: str = "1.1.0",
    source: str = "https://github.com/acme/app",
    link: str = "https://github.com/acme/app/releases/tag/v1.1.0",
    update_available: bool = True,
) -> dict[str, Any]:
    return {
        "id": f"docker.local.{name}",
        "name": name,
        "displayName": name.title(),
        "status": "running",
        "watcher": "local",
        "image": {
            "name": image,
            "tag": {"value": tag},
            "digest": {"value": "sha256:local"},
        },
        "result": {
            "tag": remote_tag,
            "digest": "sha256:remote",
            "link": link,
        },
        "updateKind": {
            "kind": "tag",
            "localValue": tag,
            "remoteValue": remote_tag,
            "semverDiff": "minor",
        },
        "labels": {
            "org.opencontainers.image.source": source,
        },
        "error": {"message": ""},
        "updateAvailable": update_available,
    }


def degraded_container_payload(
    *,
    name: str,
    image: str,
    error: str = "Request failed with status code 429",
) -> dict[str, Any]:
    payload = container_payload(name=name, image=image, update_available=False)
    image_payload = payload["image"]
    assert isinstance(image_payload, dict)
    image_payload["id"] = f"sha256:{name}-local-image"
    image_payload["digest"] = {
        "repo": "sha256:repo-digest",
        "watch": "sha256:watch-digest",
    }
    payload["result"] = None
    payload["updateKind"] = {
        "kind": "unknown",
        "localValue": None,
        "remoteValue": None,
        "semverDiff": None,
    }
    payload["platform"] = {"os": "linux", "architecture": "amd64"}
    payload["error"] = {"message": error}
    return payload


def install_recording_wud_api(
    monkeypatch,
    containers: list[dict[str, Any]],
    *,
    post_container: Callable[[str], object] | None = None,
):
    calls: list[tuple[str, str]] = []

    def fake_request_json(url: str, _client_config=None) -> object:
        path = urllib.parse.urlsplit(url).path
        calls.append(("GET", path))
        if path == "/health":
            return {"status": "ok"}
        if path == "/api/containers":
            return containers
        raise AssertionError(f"unexpected WUD API URL: {url}")

    def fake_post_json(url: str, _client_config=None, **_kwargs) -> object:
        path = urllib.parse.urlsplit(url).path
        calls.append(("POST", path))
        if post_container is not None:
            return post_container(path)
        return {"status": "ok"}

    monkeypatch.setattr(web_wud_transport, "_request_json", fake_request_json)
    monkeypatch.setattr(web_wud_transport, "_post_json", fake_post_json)
    return calls


def rescan_payload(
    scope: str = "all",
    line_numbers: list[int] | None = None,
    lines: list[dict[str, Any]] | None = None,
):
    return {
        "confirmation": "rescan_wud",
        "scope": scope,
        "line_numbers": [] if line_numbers is None else line_numbers,
        "lines": [] if lines is None else lines,
    }


def rescan_lines_from_pending(
    pending_body: dict[str, Any],
    line_numbers: list[int],
) -> list[dict[str, Any]]:
    by_line = {item["line_no"]: item for item in pending_body["items"]}
    lines: list[dict[str, Any]] = []
    for line_no in line_numbers:
        item = by_line[line_no]
        metadata = item.get("wud_metadata")
        lines.append(
            {
                "line_no": line_no,
                "raw": item["raw"],
                "source_id": item["source_id"],
                "source_hash": pending_body["source_hash"],
                "container_id": "" if metadata is None else metadata["id"],
            }
        )
    return lines
