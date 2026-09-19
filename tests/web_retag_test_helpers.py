from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from tests.web_test_helpers import (
    _client,
    _fake_docker_env,
    _make_fake_stack,
    _poll_until,
)

from wudup import web_retags as web_retags_module
from wudup.config import UpdaterConfig
from wudup.db import init_db, open_db, upsert_known_image
from wudup.digest_provenance import DigestTagProvenance
from wudup.digest_verifier import DigestResolveResult
from wudup.web_models import WebSettings


@dataclass(frozen=True)
class _RetagFixture:
    client: TestClient
    compose_dir: Path
    fake_root: Path


def _patch_digest_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_image: str,
    digest: str,
) -> None:
    class FakeDigestVerifier:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            # Test double accepts production constructor arguments but needs no setup.
            pass

        def resolve_tag_digest(self, image: str) -> DigestResolveResult:
            assert image == expected_image
            return DigestResolveResult(
                ok=True,
                status="resolved",
                reason="tag-digest-resolved",
                digest=digest,
                source="test",
            )

    monkeypatch.setattr(web_retags_module, "DigestVerifier", FakeDigestVerifier)


def _patch_digest_resolution_map(
    monkeypatch: pytest.MonkeyPatch,
    digests_by_image: dict[str, str],
) -> None:
    class FakeDigestVerifier:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            # Test double accepts production constructor arguments but needs no setup.
            pass

        def resolve_tag_digest(self, image: str) -> DigestResolveResult:
            return DigestResolveResult(
                ok=True,
                status="resolved",
                reason="tag-digest-resolved",
                digest=digests_by_image[image],
                source="test",
            )

    monkeypatch.setattr(web_retags_module, "DigestVerifier", FakeDigestVerifier)


def _patch_digest_resolution_results(
    monkeypatch: pytest.MonkeyPatch,
    results_by_image: dict[str, DigestResolveResult],
) -> None:
    class FakeDigestVerifier:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            # Test double accepts production constructor arguments but needs no setup.
            pass

        def resolve_tag_digest(self, image: str) -> DigestResolveResult:
            assert image in results_by_image, (
                f"Unexpected digest resolution for {image!r}"
            )
            return results_by_image[image]

    monkeypatch.setattr(web_retags_module, "DigestVerifier", FakeDigestVerifier)


def _wait_retag_preview_job(
    client: TestClient,
    preview_job_id: str,
) -> dict[str, object]:
    last_status = None

    def fetch_job() -> dict[str, object] | None:
        nonlocal last_status
        response = client.get(f"/api/v1/retag-plans/preview/{preview_job_id}")
        assert response.status_code == 200
        body = response.json()
        last_status = body["status"]
        if last_status in {"success", "failure"}:
            return body
        return None

    return _poll_until(
        fetch_job,
        timeout_message=lambda: (
            f"retag preview job {preview_job_id} did not finish; "
            f"last status was {last_status}"
        ),
    )


def _make_retag_fixture(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    image: str = "repo/app@sha256:old",
    label_value: str = "^latest$$",
    resolved_tag: str = "2.0",
    retag_digest_pins: bool = False,
) -> _RetagFixture:
    fake_env, fake_root = _fake_docker_env(tmp_path)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            **(env or {}),
            **fake_env,
        },
    )
    compose_dir = _make_fake_stack(
        tmp_path,
        fake_root,
        "stack",
        [("app", image, "cid-app")],
    )
    _write_compose(
        compose_dir,
        "app",
        image,
        label_value=label_value,
    )
    _seed_known_image(
        tmp_path,
        service_key="stack/app",
        image=image,
        source_image="repo/app:latest",
        resolved_tag=resolved_tag,
        watch_tag="latest",
        target_digest="sha256:old",
        final_image="repo/app@sha256:old",
    )
    if retag_digest_pins:
        _set_retag_digest_pins(tmp_path)
    return _RetagFixture(client=client, compose_dir=compose_dir, fake_root=fake_root)


def _audit_settings(tmp_path: Path) -> WebSettings:
    root = tmp_path / "state"
    root.mkdir(exist_ok=True)
    return WebSettings(
        config=UpdaterConfig(
            docker_base=tmp_path / "docker",
            wud_out_file=root / "images.todo",
            log_dir=root / "logs",
            db_path=root / "wud.sqlite",
            update_mode="live",
            max_wait=0,
            lock_timeout=0,
            timezone_name="UTC",
            compose_ignore_paths=(),
            digest_pin_updates=False,
            out_uid=None,
            out_gid=None,
        ),
        auth_token="",
        mutations_enabled=True,
    )


def _switch_choice(
    service_key: str = "stack/app",
    *,
    target_id: str | None = None,
) -> dict[str, str]:
    choice = {"service_key": service_key, "choice": "switch-to-concrete"}
    if target_id is not None:
        choice["target_id"] = target_id
    return choice


def _create_retag_plan(
    client: TestClient,
    headers: dict[str, str],
    choices: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/retag-plans",
        json={"choices": choices if choices is not None else [_switch_choice()]},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def _apply_retag_plan(
    client: TestClient,
    headers: dict[str, str],
    plan: dict[str, object],
    choices: list[dict[str, str]] | None = None,
) -> Response:
    return client.post(
        "/api/v1/retag-plans/apply",
        json={
            "plan_id": plan["plan_id"],
            "choices": choices if choices is not None else [_switch_choice()],
            "confirmation": "apply-retags",
        },
        headers=headers,
    )


def _wait_run_status(
    db_path: Path,
    run_id: object,
    status: str,
) -> sqlite3.Row:
    last_status = None

    def fetch_run() -> sqlite3.Row | None:
        nonlocal last_status
        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT status FROM update_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is not None:
            last_status = row["status"]
            if last_status == status:
                return row
        return None

    return _poll_until(
        fetch_run,
        timeout_message=lambda: (
            f"run {run_id} did not reach status {status}; "
            f"last status was {last_status}"
        ),
    )


def _seed_known_image(
    tmp_path: Path,
    *,
    service_key: str,
    image: str,
    source_image: str,
    resolved_tag: str,
    watch_tag: str,
    target_digest: str,
    final_image: str,
) -> None:
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        upsert_known_image(
            conn,
            service_key=service_key,
            image=image,
            digest_provenance=DigestTagProvenance(
                source_image=source_image,
                resolved_tag=resolved_tag,
                watch_tag=watch_tag,
                target_digest=target_digest,
                final_image=final_image,
                provenance_source="apply",
                provenance_confidence="verified",
            ),
        )


def _set_retag_digest_pins(tmp_path: Path, enabled: bool = True) -> None:
    with open_db(tmp_path / "state" / "wud.sqlite") as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO web_settings(key, value, updated_at)
            VALUES ('retag.digest_pins', ?, '2026-08-08T00:00:00+00:00')
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            ("true" if enabled else "false",),
        )
        conn.commit()


def _write_compose(
    compose_dir: Path,
    service: str,
    image: str,
    *,
    label_value: str,
) -> None:
    (compose_dir / "docker-compose.yml").write_text(
        "\n".join(
            [
                "services:",
                f"  {service}:",
                f"    image: {image}",
                "    labels:",
                f"      - wud.tag.include={label_value}",
                "",
            ]
        ),
        encoding="utf-8",
    )
