"""TrueNAS integration for checking system and alert status."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .container_identity import container_identity_candidates

if TYPE_CHECKING:
    from .updates import UpdatesOptions

@dataclass(frozen=True)
class TrueNasCallResult:
    ok: bool
    data: object | None = None
    reason: str = ""


@dataclass(frozen=True)
class TrueNasStatusSnapshot:
    update: TrueNasCallResult
    alerts: TrueNasCallResult


DEFAULT_TRUENAS_STATUS_TIMEOUT = "5"
TRUENAS_MIDDLEWARE_MOUNT = "/var/run/middleware"
_INVALID_STATUS_RESPONSE = "invalid status response"


def run_truenas_status_export_from_namespace(
    _args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    env = dict(os.environ if environ is None else environ)
    timeout = env.get("TRUENAS_STATUS_TIMEOUT") or DEFAULT_TRUENAS_STATUS_TIMEOUT

    snapshot = TrueNasStatusSnapshot(
        update=_midclt_json("update.status", timeout, env),
        alerts=_midclt_json("alert.list", timeout, env),
    )
    print(_truenas_status_payload_json(snapshot))
    return 0


def _has_command(command: str, environ: Mapping[str, str]) -> bool:
    return shutil.which(command, path=environ.get("PATH")) is not None


def _refresh_truenas_status(
    options: UpdatesOptions,
    environ: Mapping[str, str],
) -> TrueNasStatusSnapshot:
    result = _run_truenas_status_helper(options, environ)
    if not result.ok:
        return _truenas_unavailable_snapshot(result.reason)
    return _truenas_snapshot_from_payload(result.data)


def _run_truenas_status_helper(
    options: UpdatesOptions,
    environ: Mapping[str, str],
) -> TrueNasCallResult:
    if not _has_command("docker", environ):
        return TrueNasCallResult(ok=False, reason="docker not available")

    candidates = container_identity_candidates(environ)
    if not candidates:
        return TrueNasCallResult(ok=False, reason="HOSTNAME not available")

    from .updates import UpdatesError
    try:
        helper_timeout = _truenas_helper_timeout_seconds(
            options.truenas_status_timeout or DEFAULT_TRUENAS_STATUS_TIMEOUT
        )
    except UpdatesError as exc:
        return TrueNasCallResult(ok=False, reason=str(exc))

    inspect_result = _docker_inspect_current_container(
        candidates,
        environ,
        helper_timeout,
    )
    if not inspect_result.ok:
        return inspect_result

    container = inspect_result.data
    image = (
        _inspected_container_image(container) if isinstance(container, Mapping) else ""
    )
    if image == "":
        return TrueNasCallResult(
            ok=False,
            reason="docker inspect returned no image",
        )

    return _run_truenas_status_container(image, options, environ, helper_timeout)


def _docker_inspect_current_container(
    candidates: Sequence[str],
    environ: Mapping[str, str],
    helper_timeout: int,
) -> TrueNasCallResult:
    from .updates import _format_os_error

    inspect_failure = ""
    inspect_result: subprocess.CompletedProcess[str] | None = None
    for candidate in candidates:
        try:
            candidate_result = subprocess.run(
                ["docker", "container", "inspect", candidate],
                env=dict(environ),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=helper_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return TrueNasCallResult(ok=False, reason="docker inspect timed out")
        except OSError as exc:
            return TrueNasCallResult(
                ok=False,
                reason=f"docker inspect failed: {_format_os_error(exc)}",
            )

        if candidate_result.returncode == 0:
            inspect_result = candidate_result
            break
        inspect_failure = _subprocess_failure_reason(
            "docker inspect",
            candidate_result,
        )
    if inspect_result is None:
        return TrueNasCallResult(ok=False, reason=inspect_failure)

    return _truenas_container_from_inspect_stdout(inspect_result.stdout)


def _truenas_container_from_inspect_stdout(stdout: str) -> TrueNasCallResult:
    try:
        inspect_data = json.loads(stdout)
    except json.JSONDecodeError:
        return TrueNasCallResult(
            ok=False,
            reason="docker inspect returned invalid JSON",
        )
    container = _first_inspected_container(inspect_data)
    if container is None:
        return TrueNasCallResult(
            ok=False,
            reason="docker inspect returned no container",
        )
    return TrueNasCallResult(ok=True, data=container)


def _run_truenas_status_container(
    image: str,
    options: UpdatesOptions,
    environ: Mapping[str, str],
    helper_timeout: int,
) -> TrueNasCallResult:
    from .updates import _format_os_error

    run_command = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "-e",
        "TRUENAS_STATUS_CHECK=false",
        "-e",
        "WUD_SYNC_SCRIPTS=false",
        "-e",
        f"TRUENAS_STATUS_TIMEOUT={options.truenas_status_timeout}",
        "--mount",
        (
            "type=bind,"
            f"src={TRUENAS_MIDDLEWARE_MOUNT},"
            f"dst={TRUENAS_MIDDLEWARE_MOUNT},readonly"
        ),
        image,
        "truenas-status-export",
    ]
    try:
        run_result = subprocess.run(
            run_command,
            env=dict(environ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=helper_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TrueNasCallResult(ok=False, reason="docker run timed out")
    except OSError as exc:
        return TrueNasCallResult(
            ok=False,
            reason=f"docker run failed: {_format_os_error(exc)}",
        )

    if run_result.returncode != 0:
        return TrueNasCallResult(
            ok=False,
            reason=_subprocess_failure_reason("docker run", run_result),
        )
    return _truenas_status_result_from_stdout(run_result.stdout)


def _truenas_helper_timeout_seconds(value: str) -> int:
    from .updates import _parse_seconds
    call_timeout = _parse_seconds(value, "TRUENAS_STATUS_TIMEOUT")
    return max(5, call_timeout * 2 + 5)


def _subprocess_failure_reason(
    label: str,
    result: subprocess.CompletedProcess[str],
) -> str:
    reason = f"{label} exited {result.returncode}"
    detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
    if detail:
        reason = f"{reason}: {detail[0][:200]}"
    return reason


def _first_inspected_container(data: object) -> dict[str, object] | None:
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, dict) else None


def _inspected_container_image(container: Mapping[str, object]) -> str:
    image = container.get("Image")
    if isinstance(image, str) and image:
        return image
    config = container.get("Config")
    if isinstance(config, dict):
        image = config.get("Image")
        if isinstance(image, str) and image:
            return image
    return ""


def _truenas_unavailable_snapshot(reason: str) -> TrueNasStatusSnapshot:
    return TrueNasStatusSnapshot(
        update=TrueNasCallResult(ok=False, reason=reason),
        alerts=TrueNasCallResult(ok=False, reason=reason),
    )


def _truenas_status_result_from_stdout(stdout: str) -> TrueNasCallResult:
    text = stdout.strip()
    if text == "":
        return TrueNasCallResult(ok=False, reason="empty helper response")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return TrueNasCallResult(ok=False, reason="invalid JSON response")

    if not isinstance(payload, dict):
        return TrueNasCallResult(ok=False, reason=_INVALID_STATUS_RESPONSE)
    return TrueNasCallResult(ok=True, data=payload)


def _truenas_snapshot_from_payload(payload: object | None) -> TrueNasStatusSnapshot:
    if not isinstance(payload, dict):
        return _truenas_unavailable_snapshot(_INVALID_STATUS_RESPONSE)

    return TrueNasStatusSnapshot(
        update=_truenas_result_from_payload(payload.get("update")),
        alerts=_truenas_result_from_payload(payload.get("alerts")),
    )


def _truenas_result_from_payload(value: object) -> TrueNasCallResult:
    if not isinstance(value, dict):
        return TrueNasCallResult(ok=False, reason=_INVALID_STATUS_RESPONSE)
    ok = value.get("ok")
    if ok is True:
        return TrueNasCallResult(ok=True, data=value.get("data"))
    if ok is False:
        reason = value.get("reason")
        return TrueNasCallResult(
            ok=False,
            reason=reason if isinstance(reason, str) and reason else "unknown error",
        )
    return TrueNasCallResult(ok=False, reason=_INVALID_STATUS_RESPONSE)


def _truenas_status_payload_json(snapshot: TrueNasStatusSnapshot) -> str:
    return json.dumps(
        _truenas_status_payload(snapshot),
        sort_keys=True,
        separators=(",", ":"),
    )


def _truenas_status_payload(snapshot: TrueNasStatusSnapshot) -> dict[str, object]:
    return {
        "update": _truenas_update_result_to_payload(snapshot.update),
        "alerts": _truenas_alerts_result_to_payload(snapshot.alerts),
    }


def _truenas_update_result_to_payload(
    result: TrueNasCallResult,
) -> dict[str, object]:
    data: object | None = None
    if result.ok:
        data = _truenas_update_summary(result.data)
    return {"ok": result.ok, "data": data, "reason": result.reason}


def _truenas_alerts_result_to_payload(
    result: TrueNasCallResult,
) -> dict[str, object]:
    data: object | None = None
    if result.ok:
        data = _truenas_active_alerts(result.data)
    return {"ok": result.ok, "data": data, "reason": result.reason}


def _truenas_update_summary(data: object | None) -> dict[str, str]:
    summary: dict[str, str] = {}
    status = _truenas_update_status(data)
    version = _truenas_update_version(data)
    reason = _truenas_update_error_reason(data)
    if status:
        summary["status"] = status
    if version:
        summary["version"] = version
    if reason:
        summary["reason"] = reason
    return summary


def _midclt_json(
    method: str,
    status_timeout: str,
    environ: Mapping[str, str],
) -> TrueNasCallResult:
    if not _has_command("midclt", environ):
        return TrueNasCallResult(ok=False, reason="midclt not available")
    from .updates import UpdatesError, _format_os_error, _parse_seconds
    try:
        timeout = _parse_seconds(
            status_timeout or DEFAULT_TRUENAS_STATUS_TIMEOUT,
            "TRUENAS_STATUS_TIMEOUT",
        )
    except UpdatesError as exc:
        return TrueNasCallResult(ok=False, reason=str(exc))

    command = _midclt_command(method)
    try:
        # Intentionally check=False to propagate midclt exit codes for detailed error matching
        result = subprocess.run(
            command,
            env=dict(environ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TrueNasCallResult(ok=False, reason="midclt timed out")
    except OSError as exc:
        return TrueNasCallResult(
            ok=False,
            reason=f"midclt failed: {_format_os_error(exc)}",
        )

    if result.returncode != 0:
        return TrueNasCallResult(
            ok=False,
            reason=f"midclt exited {result.returncode}",
        )

    stdout = result.stdout.strip()
    if stdout == "":
        return TrueNasCallResult(ok=False, reason="empty midclt response")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return TrueNasCallResult(ok=False, reason="invalid JSON response")

    if method == "update.status":
        if not isinstance(data, dict):
            return TrueNasCallResult(
                ok=False,
                reason=f"unexpected midclt payload for update.status: {type(data).__name__}",
            )
    elif method == "alert.list" and not isinstance(data, list):
        return TrueNasCallResult(
            ok=False,
            reason=f"unexpected midclt payload for alert.list: {type(data).__name__}",
        )

    return TrueNasCallResult(ok=True, data=data)


def _midclt_command(method: str) -> list[str]:
    return ["midclt", "call", method]


def _truenas_unreachable_message(check: str, reason: str = "") -> str:
    suffix = f" ({reason})" if reason else ""
    return f"ℹ️  TrueNAS not reachable; skipping {check}.{suffix}"


def _truenas_update_status(data: object | None) -> str:
    if not isinstance(data, dict):
        return ""

    legacy_status = data.get("status")
    if isinstance(legacy_status, str):
        return legacy_status

    code = data.get("code")
    if code == "ERROR":
        return "ERROR"
    if code != "NORMAL":
        return str(code or "")

    status = data.get("status")
    if not isinstance(status, dict):
        return ""
    new_version = status.get("new_version")
    if isinstance(new_version, dict) and new_version:
        return "AVAILABLE"
    if new_version is None:
        return "UNAVAILABLE"
    return ""


def _truenas_update_version(data: object | None) -> str:
    if not isinstance(data, dict):
        return ""
    version = data.get("version")
    if isinstance(version, str):
        return version
    status = data.get("status")
    if not isinstance(status, dict):
        return ""
    new_version = status.get("new_version")
    if not isinstance(new_version, dict):
        return ""
    version = new_version.get("version")
    return version if isinstance(version, str) else ""


def _truenas_update_error_reason(data: object | None) -> str:
    if not isinstance(data, dict):
        return ""
    reason = data.get("reason")
    if isinstance(reason, str):
        return reason
    error = data.get("error")
    if not isinstance(error, dict):
        return ""
    reason = error.get("reason")
    return reason if isinstance(reason, str) else ""


def _truenas_active_alerts(data: object | None) -> list[str] | None:
    if not isinstance(data, list):
        return None

    alerts: list[str] = []
    for item in data:
        if isinstance(item, str) and item:
            alerts.append(item)
            continue
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            continue
        if item.get("dismissed") is True:
            continue
        formatted = item.get("formatted")
        if isinstance(formatted, str) and formatted:
            alerts.append(formatted)
    return alerts
