"""Read-only API token for machine integrations.

One token at a time, from container config (``WUD_WEB_READONLY_TOKEN`` or its
``_FILE`` form) or generated in the WebUI. Container config wins. A generated
token is stored only as a SHA-256 hash, and the token is accepted only by the
read-only integrations router, never by ``require_auth``.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Mapping
from contextlib import closing
from typing import Annotated, Any

from fastapi import Header, HTTPException, Request, Response

from .credential_env import (
    WUD_WEB_READONLY_TOKEN_ENV,
    WUD_WEB_READONLY_TOKEN_FILE_ENV,
)
from .db import DatabaseError, init_db, open_db, utc_timestamp
from .web_auth import (
    WEB_TOKEN_RECOMMENDED_MIN_LENGTH,
    WebConfigError,
    _prune_login_throttle,
    _record_login_client_failure,
    _setup_required,
    request_actor_type,
)
from .web_database import (
    ReadOnlyDatabaseMissing,
    connect_readonly_db,
    delete_web_setting,
    immediate_transaction,
    set_web_setting,
    web_setting,
)
from .web_metadata import json_object
from .web_models import (
    ReadonlyTokenActionRequest,
    ReadonlyTokenActionResponse,
    ReadonlyTokenStatus,
    WebSettings,
)
from .web_redaction import GENERATED_READONLY_TOKEN_PREFIX, safe_exception_detail
from .web_request_context import request_client_address, request_settings
from .web_wud_transport import _configured_secret_value

READONLY_TOKEN_HASH_KEY = "readonly_token.hash"
READONLY_TOKEN_CREATED_AT_KEY = "readonly_token.created_at"
READONLY_TOKEN_FINGERPRINT_KEY = "readonly_token.fingerprint"
READ_METHODS = frozenset({"GET", "HEAD"})
ENV_MANAGED_REASON = (
    f"Set by {WUD_WEB_READONLY_TOKEN_ENV} or {WUD_WEB_READONLY_TOKEN_FILE_ENV} "
    "in container config. Change it there and restart WUDup."
)
MUTATIONS_DISABLED_REASON = (
    "WebUI changes are turned off. Set WUD_WEB_MUTATIONS_ENABLED=true to "
    f"generate a token here, or set {WUD_WEB_READONLY_TOKEN_FILE_ENV} in "
    "container config."
)


class ReadonlyTokenError(RuntimeError):
    """Raised when the stored read-only API token cannot be changed."""


def configured_readonly_token(environ: Mapping[str, str], *, auth_token: str) -> str:
    token = _configured_secret_value(
        environ,
        direct_name=WUD_WEB_READONLY_TOKEN_ENV,
        file_name=WUD_WEB_READONLY_TOKEN_FILE_ENV,
    )
    if not token:
        return ""
    if len(token) < WEB_TOKEN_RECOMMENDED_MIN_LENGTH:
        raise WebConfigError(
            f"{WUD_WEB_READONLY_TOKEN_ENV} must be at least "
            f"{WEB_TOKEN_RECOMMENDED_MIN_LENGTH} characters. Generate one with "
            "`openssl rand -hex 32`."
        )
    if auth_token and secrets.compare_digest(token, auth_token):
        raise WebConfigError(
            f"{WUD_WEB_READONLY_TOKEN_ENV} must differ from WUD_WEB_TOKEN, which "
            "has full admin access. Generate a separate read-only token."
        )
    return token


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def readonly_token_status(settings: WebSettings) -> ReadonlyTokenStatus:
    if settings.readonly_token:
        return ReadonlyTokenStatus(
            source="env",
            fingerprint=_fingerprint(token_hash(settings.readonly_token)),
            editable=False,
            disabled_reason=ENV_MANAGED_REASON,
        )
    try:
        with closing(connect_readonly_db(settings)) as conn:
            stored = _stored_token(conn)
    except ReadOnlyDatabaseMissing:
        stored = None
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=safe_exception_detail(
                settings, "could not read the read-only API token", exc
            ),
        ) from exc
    return _status_from_stored(settings, stored)


def require_readonly_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    # Defense in depth: the integrations router only registers GET routes.
    if request.method.upper() not in READ_METHODS:
        raise HTTPException(
            status_code=405,
            detail="method not allowed",
            headers={"Allow": "GET, HEAD"},
        )
    settings = request_settings(request)
    if not settings.dev_no_auth and _setup_required(settings):
        raise HTTPException(status_code=403, detail="setup required")
    if _readonly_token_accepted(settings, request, authorization):
        return
    raise HTTPException(
        status_code=401,
        detail="a valid read-only API token is required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def api_readonly_token(
    payload: ReadonlyTokenActionRequest,
    request: Request,
    response: Response,
) -> ReadonlyTokenActionResponse:
    settings = request_settings(request)
    if settings.readonly_token:
        raise HTTPException(status_code=409, detail=ENV_MANAGED_REASON)
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")
    response.headers["Cache-Control"] = "no-store"
    try:
        if payload.action == "rotate":
            token, status, audit_run_id = _rotate_stored_token(
                settings,
                source="webui",
                actor_type=request_actor_type(settings, request),
            )
        else:
            token = ""
            status, audit_run_id = revoke_stored_token(
                settings,
                source="webui",
                actor_type=request_actor_type(settings, request),
            )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=safe_exception_detail(
                settings, "could not change the read-only API token", exc
            ),
        ) from exc
    return ReadonlyTokenActionResponse(
        status=status,
        token=token,
        audit_run_id=audit_run_id,
    )


def revoke_stored_token(
    settings: WebSettings,
    *,
    source: str,
    actor_type: str,
) -> tuple[ReadonlyTokenStatus, int]:
    if settings.readonly_token:
        raise ReadonlyTokenError(ENV_MANAGED_REASON)
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with immediate_transaction(conn):
            before = _stored_token(conn)
            delete_web_setting(conn, READONLY_TOKEN_HASH_KEY)
            delete_web_setting(conn, READONLY_TOKEN_CREATED_AT_KEY)
            delete_web_setting(conn, READONLY_TOKEN_FINGERPRINT_KEY)
            audit_run_id = _insert_readonly_token_audit(
                conn,
                settings,
                source=source,
                actor_type=actor_type,
                operation="revoke_readonly_token",
                before=_audit_value(before),
                after=_audit_value(None),
            )
    return _status_from_stored(settings, None), audit_run_id


def run_readonly_token_revoke_cli(settings: WebSettings) -> str:
    try:
        _status, _run_id = revoke_stored_token(
            settings,
            source="cli",
            actor_type="cli",
        )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise ReadonlyTokenError(
            f"could not revoke the read-only API token: {exc}"
        ) from exc
    return (
        "Revoked the WebUI read-only API token. Integrations using it now get "
        "401 until you generate a new one."
    )


def _rotate_stored_token(
    settings: WebSettings,
    *,
    source: str,
    actor_type: str,
) -> tuple[str, ReadonlyTokenStatus, int]:
    token = f"{GENERATED_READONLY_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    digest = token_hash(token)
    stored = {
        "hash": digest,
        "created_at": utc_timestamp(),
        "fingerprint": _fingerprint(digest),
    }
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with immediate_transaction(conn):
            before = _stored_token(conn)
            set_web_setting(conn, READONLY_TOKEN_HASH_KEY, stored["hash"])
            set_web_setting(conn, READONLY_TOKEN_CREATED_AT_KEY, stored["created_at"])
            set_web_setting(conn, READONLY_TOKEN_FINGERPRINT_KEY, stored["fingerprint"])
            audit_run_id = _insert_readonly_token_audit(
                conn,
                settings,
                source=source,
                actor_type=actor_type,
                operation="rotate_readonly_token",
                before=_audit_value(before),
                after=_audit_value(stored),
            )
    return token, _status_from_stored(settings, stored), audit_run_id


def _readonly_token_accepted(
    settings: WebSettings,
    request: Request,
    authorization: str | None,
) -> bool:
    scheme, separator, token = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        return False
    client_address = request_client_address(request, settings)
    lock = request.app.state.web_login_throttle_lock
    throttle = request.app.state.web_readonly_throttle
    now = time.monotonic()
    with lock:
        _prune_login_throttle(throttle, now)
        entry = throttle.get(client_address)
        if entry is not None and entry.locked_until > now:
            return False
    expected = _active_token_hash(settings)
    if expected and secrets.compare_digest(token_hash(token), expected):
        return True
    with lock:
        _record_login_client_failure(throttle, client_address, now)
    return False


def _active_token_hash(settings: WebSettings) -> str:
    if settings.readonly_token:
        return token_hash(settings.readonly_token)
    # Read on every request, with no cache, so a rotate or revoke applies at once.
    try:
        with closing(connect_readonly_db(settings)) as conn:
            return web_setting(conn, READONLY_TOKEN_HASH_KEY)
    except ReadOnlyDatabaseMissing:
        return ""
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=503,
            detail=safe_exception_detail(
                settings, "could not read the read-only API token", exc
            ),
        ) from exc


def _stored_token(conn: sqlite3.Connection) -> dict[str, str] | None:
    digest = web_setting(conn, READONLY_TOKEN_HASH_KEY)
    if not digest:
        return None
    return {
        "hash": digest,
        "created_at": web_setting(conn, READONLY_TOKEN_CREATED_AT_KEY),
        "fingerprint": web_setting(conn, READONLY_TOKEN_FINGERPRINT_KEY)
        or _fingerprint(digest),
    }


def _status_from_stored(
    settings: WebSettings,
    stored: Mapping[str, str] | None,
) -> ReadonlyTokenStatus:
    editable = settings.mutations_enabled
    disabled_reason = "" if editable else MUTATIONS_DISABLED_REASON
    if stored is None:
        return ReadonlyTokenStatus(
            source="none",
            editable=editable,
            disabled_reason=disabled_reason,
        )
    return ReadonlyTokenStatus(
        source="webui",
        fingerprint=stored["fingerprint"],
        created_at=stored["created_at"],
        editable=editable,
        disabled_reason=disabled_reason,
    )


def _fingerprint(digest: str) -> str:
    return digest[:8]


def _audit_value(stored: Mapping[str, str] | None) -> dict[str, str]:
    # Never the token or its hash: the fingerprint identifies it in history.
    if stored is None:
        return {"fingerprint": ""}
    return {"fingerprint": stored["fingerprint"]}


def _insert_readonly_token_audit(
    conn: sqlite3.Connection,
    settings: WebSettings,
    *,
    source: str,
    actor_type: str,
    operation: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> int:
    now = utc_timestamp()
    metadata = {
        "source": source,
        "operation": operation,
        "actor_type": actor_type,
        "resource_type": "readonly_token",
        "resource_id": "integrations",
        "target": {"fingerprint": after.get("fingerprint") or before.get("fingerprint")},
    }
    cursor = conn.execute(
        """
        INSERT INTO update_runs (
            started_at,
            finished_at,
            status,
            dry_run,
            mode,
            wud_file,
            log_file,
            metadata_json
        )
        VALUES (?, ?, 'success', 0, 'web-settings', ?, '', ?)
        """,
        (now, now, str(settings.config.wud_out_file), json_object(metadata)),
    )
    run_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO update_events (
            run_id,
            created_at,
            service_name,
            stack_name,
            image,
            target_image,
            status,
            metadata_json
        )
        VALUES (?, ?, 'settings', 'webui', 'readonly-token', 'integrations', 'success', ?)
        """,
        (
            run_id,
            now,
            json_object({**metadata, "before": before, "after": after}),
        ),
    )
    return run_id
