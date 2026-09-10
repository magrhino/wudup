"""WebUI auth, setup, CSRF, and request-safety helpers."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import socket
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import DatabaseError, init_db, open_db, utc_timestamp
from .web_metadata import json_object as _json_object
from .web_models import (
    PASSWORD_MIN_LENGTH,
    AdminRecoveryClaim,
    AuthSessionResponse,
    CsrfResponse,
    LoginRequest,
    LoginThrottleEntry,
    ResetAdminClaimRequest,
    SetupClaimRequest,
    SetupStatusResponse,
    WebSettings,
)

SESSION_MAX_AGE_SECONDS = 86_400
SETUP_CLAIM_MAX_AGE_SECONDS = 86_400
LOGIN_THROTTLE_MAX_FAILURES = 5
LOGIN_THROTTLE_COOLDOWN_SECONDS = 60.0
LOGIN_THROTTLE_MAX_ENTRIES = 1024
LOGIN_THROTTLE_MAX_CLIENT_ENTRIES = 1024
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
SECURE_COOKIE_MODES = frozenset({"auto", "true", "false"})
CSRF_HEADER = "x-wud-csrf-token"
CSRF_COOKIE = "wud_csrf_token"
SESSION_COOKIE = "wud_session"
SETUP_CLAIM_HASH_KEY = "setup_claim_hash"
SETUP_CLAIM_EXPIRES_KEY = "setup_claim_expires_at"
RESET_ADMIN_CLAIM_HASH_KEY = "reset_admin_claim_hash"
RESET_ADMIN_CLAIM_EXPIRES_KEY = "reset_admin_claim_expires_at"
RESET_ADMIN_CLAIM_USER_ID_KEY = "reset_admin_claim_user_id"
DEFAULT_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SENSITIVE_ENV_KEYS = (
    "WUD_WEB_TOKEN",
    "WUD_API_AUTH_BEARER_TOKEN",
    "WUD_API_AUTH_BEARER_TOKEN_FILE",
    "WUD_API_AUTH_BASIC_PASSWORD",
    "WUD_API_AUTH_BASIC_PASSWORD_FILE",
    "GITHUB_TOKEN",
    "DISCORD_WEBHOOK",
    "DISCORD_RELEASES_WEBHOOK",
    "ADMIN_WEBHOOK",
)
SENSITIVE_FIELD_KEY_PARTS = frozenset(
    {
        "auth",
        "authorization",
        "credential",
        "header",
        "key",
        "pass",
        "password",
        "secret",
        "token",
        "webhook",
    }
)
PASSWORD_HASHER = PasswordHasher()


class WebConfigError(ValueError):
    """Raised when WebUI configuration is invalid."""


class WebAdminResetError(RuntimeError):
    """Raised when local admin recovery cannot be issued."""


def _password_hasher() -> Any:
    return PASSWORD_HASHER


def _login_throttle_max_failures() -> int:
    return LOGIN_THROTTLE_MAX_FAILURES


def _login_throttle_cooldown_seconds() -> float:
    return LOGIN_THROTTLE_COOLDOWN_SECONDS


def _login_throttle_max_entries() -> int:
    return LOGIN_THROTTLE_MAX_ENTRIES


def _login_throttle_max_client_entries() -> int:
    return LOGIN_THROTTLE_MAX_CLIENT_ENTRIES


async def request_safety_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
    settings: WebSettings,
) -> Response:
    host_error = _host_header_error(request, settings)
    if host_error is not None:
        return host_error
    if _requires_csrf_origin_check(request):
        error = _csrf_origin_error(request, settings)
        if error is not None:
            return error
    return await call_next(request)


async def require_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    settings = _settings(request)
    if settings.dev_no_auth:
        return
    if _setup_required(settings):
        raise HTTPException(status_code=403, detail="setup required")
    if _bearer_token_valid(settings, authorization):
        return
    if _session_user(settings, request) is not None:
        return
    raise HTTPException(
        status_code=401,
        detail="authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def api_setup_status(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> SetupStatusResponse:
    settings = _settings(request)
    setup_required = _setup_required(settings)
    return SetupStatusResponse(
        setup_required=setup_required,
        claim_required=setup_required and not settings.dev_no_auth,
        authenticated=_request_authenticated(settings, request, authorization),
        auth_required=settings.auth_required,
        dev_auth_bypass=settings.dev_no_auth,
        mutations_enabled=settings.mutations_enabled,
        password_min_length=PASSWORD_MIN_LENGTH,
    )


def api_setup_claim(
    payload: SetupClaimRequest,
    request: Request,
    response: Response,
) -> AuthSessionResponse:
    settings = _settings(request)
    if settings.dev_no_auth:
        return _auth_session_response(
            settings,
            authenticated=True,
            setup_required=False,
        )
    username = _normalize_username(payload.username)
    if not username:
        raise HTTPException(status_code=422, detail="username is required")
    user_id, password_hash = _claim_initial_admin(
        settings, payload.claim, username, payload.password
    )
    session_id = _create_web_session(
        settings, user_id=user_id, password_hash=password_hash, request=request
    )
    if session_id is None:
        raise _auth_failed()
    _set_session_cookie(response, session_id, request, settings)
    return _auth_session_response(
        settings,
        authenticated=True,
        setup_required=False,
        username=username,
    )


def api_auth_csrf(request: Request, response: Response) -> CsrfResponse:
    csrf_token = secrets.token_urlsafe(32)
    _set_csrf_cookie(response, csrf_token, request, _settings(request))
    return CsrfResponse(csrf_token=csrf_token)


def api_auth_login(
    payload: LoginRequest,
    request: Request,
    response: Response,
) -> AuthSessionResponse:
    settings = _settings(request)
    if settings.dev_no_auth:
        return _auth_session_response(
            settings,
            authenticated=True,
            setup_required=False,
        )
    if _setup_required(settings):
        raise HTTPException(status_code=403, detail="setup required")
    username = _normalize_username(payload.username)
    if _login_throttle_blocked(request, settings, username):
        raise _auth_failed()
    user = _verify_web_user(settings, payload.username, payload.password)
    if user is None:
        _record_login_failure(request, settings, username)
        raise _auth_failed()
    session_id = _create_web_session(
        settings,
        user_id=int(user["id"]),
        password_hash=str(user["password_hash"]),
        request=request,
    )
    if session_id is None:
        _record_login_failure(request, settings, username)
        raise _auth_failed()
    _clear_login_throttle(request, settings, username)
    _set_session_cookie(response, session_id, request, settings)
    return _auth_session_response(
        settings,
        authenticated=True,
        setup_required=False,
        username=str(user["username"]),
    )


def api_auth_reset_admin_claim(
    payload: ResetAdminClaimRequest,
    request: Request,
    response: Response,
) -> AuthSessionResponse:
    settings = _settings(request)
    username = _normalize_username(payload.username)
    if not username:
        raise HTTPException(status_code=422, detail="username is required")
    user_id, password_hash = _redeem_admin_recovery_claim(
        settings,
        claim=payload.claim,
        username=username,
        password=payload.password,
    )
    session_id = _create_web_session(
        settings, user_id=user_id, password_hash=password_hash, request=request
    )
    if session_id is None:
        raise _auth_failed()
    _set_session_cookie(response, session_id, request, settings)
    return _auth_session_response(
        settings,
        authenticated=True,
        setup_required=False,
        username=username,
    )


def api_auth_logout(
    request: Request,
    response: Response,
) -> AuthSessionResponse:
    settings = _settings(request)
    _revoke_web_session(settings, request.cookies.get(SESSION_COOKIE, ""))
    _clear_session_cookie(response)
    _clear_csrf_cookie(response)
    return _auth_session_response(
        settings,
        authenticated=settings.dev_no_auth,
        setup_required=_setup_required(settings),
    )


def api_auth_session(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthSessionResponse:
    settings = _settings(request)
    setup_required = _setup_required(settings)
    user = _session_user(settings, request)
    authenticated = (
        settings.dev_no_auth
        or (not setup_required and _bearer_token_valid(settings, authorization))
        or user is not None
    )
    return _auth_session_response(
        settings,
        authenticated=authenticated,
        setup_required=setup_required,
        username=None if user is None else str(user["username"]),
    )


def _settings(request: Request) -> WebSettings:
    return request.app.state.web_settings


def _auth_session_response(
    settings: WebSettings,
    *,
    authenticated: bool,
    setup_required: bool,
    username: str | None = None,
) -> AuthSessionResponse:
    return AuthSessionResponse(
        authenticated=authenticated,
        setup_required=setup_required,
        auth_required=settings.auth_required,
        dev_auth_bypass=settings.dev_no_auth,
        mutations_enabled=settings.mutations_enabled,
        username=username,
    )


async def _validation_exception_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(_strip_validation_inputs(exc.errors()))},
    )


def _strip_validation_inputs(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_validation_inputs(item)
            for key, item in value.items()
            if key != "input"
        }
    if isinstance(value, list):
        return [_strip_validation_inputs(item) for item in value]
    return value


def _redact_sensitive_text(
    settings: WebSettings,
    value: str,
    extra_secrets: Sequence[str] = (),
) -> str:
    redacted = value
    for secret in _sensitive_redaction_values(settings, extra_secrets):
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _sensitive_mapping_key(key: str) -> bool:
    normalized = key.lower().replace("-", "").replace("_", "")
    return any(part in normalized for part in SENSITIVE_FIELD_KEY_PARTS)


def _sanitize_support_bundle_value(settings: WebSettings, value: Any) -> Any:
    return _sanitize_support_bundle_value_with_secrets(settings, value, ())


def _sanitize_support_bundle_value_with_secrets(
    settings: WebSettings,
    value: Any,
    extra_secrets: Sequence[str],
) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_support_bundle_value_with_secrets(
                settings,
                item,
                extra_secrets,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_support_bundle_value_with_secrets(settings, item, extra_secrets)
            for item in value
        ]
    if isinstance(value, str):
        return _sanitize_support_bundle_text(settings, value, extra_secrets)
    return value


def _sanitize_support_bundle_text(
    settings: WebSettings,
    value: str,
    extra_secrets: Sequence[str] = (),
) -> str:
    replacements = _support_bundle_path_replacements(settings)
    redacted = _redact_sensitive_text(settings, value, extra_secrets=extra_secrets)
    for source, target in replacements:
        redacted = redacted.replace(source, target)
    return _redact_unknown_absolute_paths(redacted)


def _support_bundle_path_replacements(settings: WebSettings) -> list[tuple[str, str]]:
    config = settings.config
    exact_paths: list[tuple[Path, str]] = [
        (config.wud_out_file, "<WUD_OUT_FILE>"),
        (config.log_dir, "<WUD_LOG_DIR>"),
        (config.db_path, "<WUD_DB_PATH>"),
    ]
    root_paths: list[tuple[Path, str]] = [(config.docker_base, "<DOCKER_BASE>")]
    if settings.host_docker_base is not None:
        root_paths.append((settings.host_docker_base, "<HOST_DOCKER_BASE>"))

    replacements: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, label in (*exact_paths, *root_paths):
        text = str(path)
        if text and text not in seen:
            seen.add(text)
            replacements.append((text, label))

    for root, label in root_paths:
        root_text = str(root).rstrip("/")
        if not root_text or root_text in seen:
            continue
        seen.add(root_text)
        replacements.append((root_text, label))

    return sorted(replacements, key=lambda item: len(item[0]), reverse=True)


def _redact_unknown_absolute_paths(value: str) -> str:
    absolute_path_pattern = (
        r"(?<![:/<>\w-])/(?:[^\s\"'`,;)\]}]+)"
        r"|(?<![\w<])(?:[A-Za-z]:[\\/][^\s\"'`,;)\]}]+"
        r"|\\\\[^\s\"'`,;)\]}]+)"
    )
    return re.sub(
        absolute_path_pattern,
        "[REDACTED_PATH]",
        value,
    )


def _sensitive_redaction_values(
    settings: WebSettings,
    extra_secrets: Sequence[str],
) -> list[str]:
    values: list[str] = []
    env = settings.command_env or {}
    values.extend(extra_secrets)
    values.append(settings.auth_token)
    values.extend(settings.wud_api_client.secret_values)
    values.extend(env.get(key, "") for key in SENSITIVE_ENV_KEYS)

    expanded: list[str] = []
    for value in values:
        if value:
            expanded.append(value)
            expanded.extend(_secret_url_fragments(value))

    seen: set[str] = set()
    result: list[str] = []
    for value in sorted(expanded, key=len, reverse=True):
        if len(value) < 4 or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _secret_url_fragments(value: str) -> list[str]:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return []
    fragments: list[str] = []
    path = parsed.path.strip("/")
    if len(path) >= 8:
        fragments.append(path)
    fragments.extend(segment for segment in path.split("/") if len(segment) >= 8)
    if parsed.query and len(parsed.query) >= 8:
        fragments.append(parsed.query)
    return fragments


def _safe_exception_detail(
    settings: WebSettings,
    message: str,
    exc: BaseException,
) -> str:
    detail = _redact_sensitive_text(settings, str(exc))
    return f"{message}: {_redact_unknown_absolute_paths(detail)}"


def _prepare_web_auth_state(settings: WebSettings) -> str:
    with open_db(settings.config.db_path) as conn:
        init_db(conn)
        user_count = _web_user_count(conn)
        if user_count > 0:
            _delete_web_setting(conn, SETUP_CLAIM_HASH_KEY)
            _delete_web_setting(conn, SETUP_CLAIM_EXPIRES_KEY)
            return ""
        claim = secrets.token_urlsafe(32)
        with conn:
            _set_web_setting(conn, SETUP_CLAIM_HASH_KEY, _secret_hash(claim))
            _set_web_setting(
                conn,
                SETUP_CLAIM_EXPIRES_KEY,
                _utc_timestamp_after(SETUP_CLAIM_MAX_AGE_SECONDS),
            )
        return claim


def _setup_required(settings: WebSettings) -> bool:
    if settings.dev_no_auth:
        return False
    try:
        with open_db(settings.config.db_path) as conn:
            init_db(conn)
            return _web_user_count(conn) == 0
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read web auth state",
                exc,
            ),
        ) from exc


def _claim_initial_admin(
    settings: WebSettings,
    claim: str,
    username: str,
    password: str,
) -> tuple[int, str]:
    now = utc_timestamp()
    try:
        with open_db(settings.config.db_path) as conn:
            init_db(conn)
            with _immediate_transaction(conn):
                if _web_user_count(conn) > 0:
                    raise HTTPException(status_code=409, detail="setup is complete")
                expected_hash = _web_setting(conn, SETUP_CLAIM_HASH_KEY)
                expires_at = _web_setting(conn, SETUP_CLAIM_EXPIRES_KEY)
                if not expected_hash or not expires_at:
                    raise HTTPException(status_code=403, detail="setup claim is invalid")
                if expires_at < now:
                    raise HTTPException(status_code=403, detail="setup claim expired")
                if not secrets.compare_digest(expected_hash, _secret_hash(claim)):
                    raise HTTPException(status_code=403, detail="setup claim is invalid")
                password_hash = _password_hasher().hash(password)
                cursor = conn.execute(
                    """
                    INSERT INTO web_users (
                        username,
                        password_hash,
                        role,
                        created_at,
                        password_updated_at
                    )
                    VALUES (?, ?, 'admin', ?, ?)
                    """,
                    (username, password_hash, now, now),
                )
                _delete_web_setting(conn, SETUP_CLAIM_HASH_KEY)
                _delete_web_setting(conn, SETUP_CLAIM_EXPIRES_KEY)
                return int(cursor.lastrowid), password_hash
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="username is unavailable") from exc
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(settings, "could not complete setup", exc),
        ) from exc


def issue_admin_recovery_claim(
    settings: WebSettings,
    username: str,
) -> AdminRecoveryClaim:
    normalized = _normalize_username(username)
    if not normalized:
        raise WebAdminResetError("username is required")

    db_path = settings.config.db_path
    if str(db_path) != ":memory:" and not db_path.is_file():
        raise WebAdminResetError(f"database file does not exist: {db_path}")

    now = utc_timestamp()
    claim = secrets.token_urlsafe(32)
    expires_at = _utc_timestamp_after(SETUP_CLAIM_MAX_AGE_SECONDS)
    disabled_password_hash = _password_hasher().hash(secrets.token_urlsafe(96))
    try:
        with open_db(db_path) as conn:
            init_db(conn)
            with _immediate_transaction(conn):
                user = _active_admin_user(conn, normalized)
                if user is None:
                    if _web_user_count(conn) == 0:
                        raise WebAdminResetError("WebUI setup is not complete")
                    raise WebAdminResetError(f"active admin user not found: {normalized}")
                user_id = int(user["id"])
                conn.execute(
                    """
                    UPDATE web_users
                    SET password_hash = ?,
                        password_updated_at = ?
                    WHERE id = ?
                    """,
                    (disabled_password_hash, now, user_id),
                )
                revoked_sessions = conn.execute(
                    """
                    UPDATE web_sessions
                    SET revoked_at = ?
                    WHERE user_id = ?
                      AND revoked_at IS NULL
                    """,
                    (now, user_id),
                ).rowcount
                _set_web_setting(conn, RESET_ADMIN_CLAIM_HASH_KEY, _secret_hash(claim))
                _set_web_setting(conn, RESET_ADMIN_CLAIM_EXPIRES_KEY, expires_at)
                _set_web_setting(conn, RESET_ADMIN_CLAIM_USER_ID_KEY, str(user_id))
                audit_run_id = _insert_auth_audit(
                    conn,
                    settings,
                    source="cli",
                    operation="admin_reset_claim_issued",
                    username=normalized,
                    user_id=user_id,
                    before={
                        "password_updated_at": str(user["password_updated_at"]),
                    },
                    after={
                        "claim_expires_at": expires_at,
                        "password_invalidated": True,
                        "revoked_sessions": max(0, int(revoked_sessions)),
                    },
                )
                return AdminRecoveryClaim(
                    username=normalized,
                    claim=claim,
                    expires_at=expires_at,
                    revoked_sessions=max(0, int(revoked_sessions)),
                    audit_run_id=audit_run_id,
                )
    except WebAdminResetError:
        raise
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise WebAdminResetError(f"could not issue admin recovery claim: {exc}") from exc


def _redeem_admin_recovery_claim(
    settings: WebSettings,
    *,
    claim: str,
    username: str,
    password: str,
) -> tuple[int, str]:
    now = utc_timestamp()
    try:
        with open_db(settings.config.db_path) as conn:
            init_db(conn)
            with _immediate_transaction(conn):
                expected_hash = _web_setting(conn, RESET_ADMIN_CLAIM_HASH_KEY)
                expires_at = _web_setting(conn, RESET_ADMIN_CLAIM_EXPIRES_KEY)
                user_id_raw = _web_setting(conn, RESET_ADMIN_CLAIM_USER_ID_KEY)
                if not expected_hash or not expires_at or not user_id_raw:
                    raise HTTPException(
                        status_code=403,
                        detail="admin recovery claim is invalid",
                    )
                if expires_at < now:
                    raise HTTPException(
                        status_code=403,
                        detail="admin recovery claim expired",
                    )
                if not secrets.compare_digest(expected_hash, _secret_hash(claim)):
                    raise HTTPException(
                        status_code=403,
                        detail="admin recovery claim is invalid",
                    )
                try:
                    user_id = int(user_id_raw)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=403,
                        detail="admin recovery claim is invalid",
                    ) from exc
                user = conn.execute(
                    """
                    SELECT *
                    FROM web_users
                    WHERE id = ?
                      AND username = ?
                      AND role = 'admin'
                      AND disabled_at IS NULL
                    LIMIT 1
                    """,
                    (user_id, username),
                ).fetchone()
                if user is None:
                    raise HTTPException(
                        status_code=403,
                        detail="admin recovery claim is invalid",
                    )
                password_hash = _password_hasher().hash(password)
                conn.execute(
                    """
                    UPDATE web_users
                    SET password_hash = ?,
                        password_updated_at = ?
                    WHERE id = ?
                    """,
                    (password_hash, now, user_id),
                )
                _delete_admin_recovery_claim(conn)
                _insert_auth_audit(
                    conn,
                    settings,
                    source="webui",
                    operation="admin_reset_password_changed",
                    username=username,
                    user_id=user_id,
                    before={
                        "claim_expires_at": expires_at,
                    },
                    after={
                        "password_updated_at": now,
                    },
                )
                return user_id, password_hash
    except HTTPException:
        raise
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not reset admin password",
                exc,
            ),
        ) from exc


@contextmanager
def _immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _verify_web_user(
    settings: WebSettings,
    username: str,
    password: str,
) -> sqlite3.Row | None:
    normalized = _normalize_username(username)
    if not normalized:
        return None
    try:
        with open_db(settings.config.db_path) as conn:
            init_db(conn)
            user = conn.execute(
                """
                SELECT *
                FROM web_users
                WHERE username = ?
                  AND disabled_at IS NULL
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            if user is None:
                return None
            try:
                verified = _password_hasher().verify(
                    str(user["password_hash"]),
                    password,
                )
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                return None
            if not verified:
                return None
            if _password_hasher().check_needs_rehash(
                str(user["password_hash"])
            ):
                return _rehash_web_user(conn, user, password)
            return user
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not verify credentials",
                exc,
            ),
        ) from exc


def _rehash_web_user(
    conn: sqlite3.Connection,
    user: sqlite3.Row,
    password: str,
) -> sqlite3.Row | None:
    with conn:
        cursor = conn.execute(
            """
            UPDATE web_users
            SET password_hash = ?,
                password_updated_at = ?
            WHERE id = ?
              AND password_hash = ?
              AND disabled_at IS NULL
            """,
            (
                _password_hasher().hash(password),
                utc_timestamp(),
                user["id"],
                user["password_hash"],
            ),
        )
        # Capture the current credential before releasing the write transaction.
        user = conn.execute(
            "SELECT * FROM web_users WHERE id = ?", (user["id"],)
        ).fetchone()
    if cursor.rowcount == 1:
        return user
    # Another login may have rehashed the same password. Verify the current
    # credential before allowing session issuance.
    if user is None or user["disabled_at"] is not None:
        return None
    try:
        if not _password_hasher().verify(user["password_hash"], password):
            return None
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return None
    return user


def _auth_failed() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _login_throttle_blocked(
    request: Request,
    settings: WebSettings,
    username: str,
) -> bool:
    client_address = _request_client_address(request, settings)
    key = _login_throttle_key(username, client_address)
    now = time.monotonic()
    with request.app.state.web_login_throttle_lock:
        throttle: dict[tuple[str, str], LoginThrottleEntry] = (
            request.app.state.web_login_throttle
        )
        client_throttle: dict[str, LoginThrottleEntry] = (
            request.app.state.web_login_client_throttle
        )
        _prune_login_throttle(throttle, now)
        _prune_login_throttle(client_throttle, now)
        entry = throttle.get(key)
        client_entry = client_throttle.get(client_address)
        return (
            (entry is not None and entry.locked_until > now)
            or (client_entry is not None and client_entry.locked_until > now)
        )


def _record_login_failure(
    request: Request,
    settings: WebSettings,
    username: str,
) -> None:
    client_address = _request_client_address(request, settings)
    key = _login_throttle_key(username, client_address)
    now = time.monotonic()
    with request.app.state.web_login_throttle_lock:
        throttle: dict[tuple[str, str], LoginThrottleEntry] = (
            request.app.state.web_login_throttle
        )
        client_throttle: dict[str, LoginThrottleEntry] = (
            request.app.state.web_login_client_throttle
        )
        _prune_login_throttle(throttle, now)
        _prune_login_throttle(client_throttle, now)
        _record_login_client_failure(
            client_throttle,
            client_address,
            now,
        )
        entry = throttle.get(key)
        if entry is None:
            if (
                len(throttle) >= _login_throttle_max_entries()
                and not _evict_login_throttle_entry(throttle, now)
            ):
                return
            entry = LoginThrottleEntry(
                failures=1,
                first_failed_at=now,
                last_failed_at=now,
            )
            throttle[key] = entry
        else:
            entry.failures += 1
            entry.last_failed_at = now
        if entry.failures >= _login_throttle_max_failures():
            entry.locked_until = now + _login_throttle_cooldown_seconds()


def _clear_login_throttle(
    request: Request,
    settings: WebSettings,
    username: str,
) -> None:
    client_address = _request_client_address(request, settings)
    key = _login_throttle_key(username, client_address)
    with request.app.state.web_login_throttle_lock:
        request.app.state.web_login_throttle.pop(key, None)
        request.app.state.web_login_client_throttle.pop(client_address, None)


def _login_throttle_key(
    username: str,
    client_address: str,
) -> tuple[str, str]:
    return (
        _normalize_username(username).casefold(),
        client_address,
    )


def _record_login_client_failure(
    throttle: dict[str, LoginThrottleEntry],
    client_address: str,
    now: float,
) -> None:
    entry = throttle.get(client_address)
    if entry is None:
        if len(throttle) >= _login_throttle_max_client_entries():
            _evict_login_throttle_entry(throttle, now, preserve_locked=False)
        entry = LoginThrottleEntry(
            failures=1,
            first_failed_at=now,
            last_failed_at=now,
        )
        throttle[client_address] = entry
    else:
        entry.failures += 1
        entry.last_failed_at = now
    if entry.failures >= _login_throttle_max_failures():
        entry.locked_until = now + _login_throttle_cooldown_seconds()


def _prune_login_throttle(
    throttle: dict[Any, LoginThrottleEntry],
    now: float,
) -> None:
    for key, entry in list(throttle.items()):
        if now - entry.last_failed_at >= _login_throttle_cooldown_seconds():
            throttle.pop(key, None)


def _evict_login_throttle_entry(
    throttle: dict[Any, LoginThrottleEntry],
    now: float,
    *,
    preserve_locked: bool = True,
) -> bool:
    evictable = [
        (key, entry)
        for key, entry in throttle.items()
        if not preserve_locked or entry.locked_until <= now
    ]
    if not evictable:
        return False
    key, _entry = min(
        evictable,
        key=lambda item: (item[1].last_failed_at, item[1].first_failed_at),
    )
    throttle.pop(key, None)
    return True


def _create_web_session(
    settings: WebSettings,
    *,
    user_id: int,
    password_hash: str,
    request: Request,
) -> str | None:
    session_id = secrets.token_urlsafe(48)
    now = utc_timestamp()
    expires_at = _utc_timestamp_after(SESSION_MAX_AGE_SECONDS)
    try:
        with open_db(settings.config.db_path) as conn:
            init_db(conn)
            with conn:
                # Recovery either changes the hash first, or revokes this session
                # after insertion. The credential check and insert must be atomic.
                cursor = conn.execute(
                    """
                    INSERT INTO web_sessions (
                        id_hash,
                        user_id,
                        created_at,
                        last_seen_at,
                        expires_at,
                        user_agent_hash
                    )
                    SELECT ?, id, ?, ?, ?, ?
                    FROM web_users
                    WHERE id = ?
                      AND password_hash = ?
                      AND disabled_at IS NULL
                    """,
                    (
                        _secret_hash(session_id),
                        now,
                        now,
                        expires_at,
                        _user_agent_hash(request),
                        user_id,
                        password_hash,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not create session",
                exc,
            ),
        ) from exc
    return session_id


def _session_user(
    settings: WebSettings,
    request: Request,
    *,
    touch: bool | None = None,
) -> sqlite3.Row | None:
    session_id = request.cookies.get(SESSION_COOKIE, "")
    if not session_id:
        return None
    try:
        with open_db(settings.config.db_path) as conn:
            init_db(conn)
            row = conn.execute(
                """
                SELECT web_sessions.id_hash, web_users.*
                FROM web_sessions
                JOIN web_users ON web_users.id = web_sessions.user_id
                WHERE web_sessions.id_hash = ?
                  AND web_sessions.revoked_at IS NULL
                  AND web_sessions.expires_at >= ?
                  AND web_users.disabled_at IS NULL
                LIMIT 1
                """,
                (_secret_hash(session_id), utc_timestamp()),
            ).fetchone()
            if row is None:
                return None
            if touch is None:
                touch = str(getattr(request, "method", "")).upper() not in SAFE_METHODS
            if touch:
                with conn:
                    conn.execute(
                        """
                        UPDATE web_sessions
                        SET last_seen_at = ?
                        WHERE id_hash = ?
                        """,
                        (utc_timestamp(), row["id_hash"]),
                    )
            return row
    except (OSError, sqlite3.Error, DatabaseError):
        return None


def _revoke_web_session(settings: WebSettings, session_id: str) -> None:
    if not session_id:
        return
    try:
        with open_db(settings.config.db_path) as conn:
            init_db(conn)
            with conn:
                conn.execute(
                    """
                    UPDATE web_sessions
                    SET revoked_at = ?
                    WHERE id_hash = ?
                    """,
                    (utc_timestamp(), _secret_hash(session_id)),
                )
    except (OSError, sqlite3.Error, DatabaseError):
        return


def _request_authenticated(
    settings: WebSettings,
    request: Request,
    authorization: str | None,
) -> bool:
    if settings.dev_no_auth:
        return True
    if _setup_required(settings):
        return False
    return _bearer_token_valid(settings, authorization) or _session_user(
        settings,
        request,
    ) is not None


def _active_admin_user(
    conn: sqlite3.Connection,
    username: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM web_users
        WHERE username = ?
          AND role = 'admin'
          AND disabled_at IS NULL
        LIMIT 1
        """,
        (username,),
    ).fetchone()


def _web_user_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM web_users").fetchone()
    return int(row[0])


def _web_setting(conn: sqlite3.Connection, key: str) -> str:
    return _web_setting_or_none(conn, key) or ""


def _web_setting_or_none(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        """
        SELECT value
        FROM web_settings
        WHERE key = ?
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    if row is None:
        return None
    return str(row["value"] if isinstance(row, sqlite3.Row) else row[0])


def _set_web_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO web_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, utc_timestamp()),
    )


def _delete_web_setting(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM web_settings WHERE key = ?", (key,))


def _delete_admin_recovery_claim(conn: sqlite3.Connection) -> None:
    _delete_web_setting(conn, RESET_ADMIN_CLAIM_HASH_KEY)
    _delete_web_setting(conn, RESET_ADMIN_CLAIM_EXPIRES_KEY)
    _delete_web_setting(conn, RESET_ADMIN_CLAIM_USER_ID_KEY)


def _insert_auth_audit(
    conn: sqlite3.Connection,
    settings: WebSettings,
    *,
    source: str,
    operation: str,
    username: str,
    user_id: int,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> int:
    now = utc_timestamp()
    metadata = {
        "source": source,
        "operation": operation,
        "actor_type": "cli" if source == "cli" else "reset_claim",
        "resource_type": "web_user",
        "resource_id": str(user_id),
        "target": {
            "user_id": user_id,
            "username": username,
        },
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
        VALUES (?, ?, 'success', 0, 'web-auth', ?, '', ?)
        """,
        (
            now,
            now,
            str(settings.config.wud_out_file),
            _json_object(metadata),
        ),
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
        VALUES (?, ?, ?, '', 'web_user', ?, 'success', ?)
        """,
        (
            run_id,
            now,
            username,
            str(user_id),
            _json_object({**metadata, "before": before, "after": after}),
        ),
    )
    return run_id


def _bearer_token_valid(settings: WebSettings, authorization: str | None) -> bool:
    if not settings.auth_token:
        return False
    scheme, separator, token = (authorization or "").partition(" ")
    return (
        separator == " "
        and scheme.lower() == "bearer"
        and secrets.compare_digest(token, settings.auth_token)
    )


def _request_actor_type(settings: WebSettings, request: Request) -> str:
    if settings.dev_no_auth:
        return "dev"
    authorization = request.headers.get("authorization")
    if _bearer_token_valid(settings, authorization):
        return "bearer"
    if request.cookies.get(SESSION_COOKIE):
        return "session"
    return "unknown"


def _normalize_username(value: str) -> str:
    return value.strip()


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _user_agent_hash(request: Request) -> str:
    user_agent = request.headers.get("user-agent", "")
    return "" if not user_agent else _secret_hash(user_agent)


def _utc_timestamp_after(seconds: int) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(seconds=seconds)
    ).isoformat()


def _set_csrf_cookie(
    response: Response,
    csrf_token: str,
    request: Request,
    settings: WebSettings,
) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=False,
        samesite="strict",
        path="/",
        secure=_secure_cookie(settings, request),
    )


def _clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(CSRF_COOKIE, path="/", samesite="strict")


def _set_session_cookie(
    response: Response,
    session: str,
    request: Request,
    settings: WebSettings,
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="strict",
        path="/",
        secure=_secure_cookie(settings, request),
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="strict")


def _requires_csrf_origin_check(request: Request) -> bool:
    return request.method.upper() not in SAFE_METHODS and request.url.path.startswith(
        "/api/v1/"
    )


def _csrf_origin_error(
    request: Request,
    settings: WebSettings,
) -> JSONResponse | None:
    origin = request.headers.get("origin", "")
    if not origin:
        return _forbidden("origin header is required")
    if not _origin_allowed(request, settings, origin):
        return _forbidden("origin is not allowed")
    csrf_header = request.headers.get(CSRF_HEADER, "")
    csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
    if not csrf_header or not csrf_cookie:
        return _forbidden("csrf token is required")
    if not secrets.compare_digest(csrf_header, csrf_cookie):
        return _forbidden("csrf token is invalid")
    return None


def _host_header_error(
    request: Request,
    settings: WebSettings,
) -> JSONResponse | None:
    host = request.headers.get("host", "")
    normalized = _normalize_host(host)
    if normalized and normalized in settings.allowed_hosts:
        return None
    return JSONResponse({"detail": "host is not allowed"}, status_code=400)


def _origin_allowed(
    request: Request,
    settings: WebSettings,
    origin: str,
) -> bool:
    normalized = _normalize_origin(origin)
    if not normalized:
        return False
    if normalized in settings.allowed_origins:
        return True
    same_origin = _effective_origin(request, settings)
    return secrets.compare_digest(normalized, same_origin)


def _secure_cookie(settings: WebSettings, request: Request) -> bool:
    if settings.secure_cookies == "true":
        return True
    if settings.secure_cookies == "false":
        return False
    return _effective_origin(request, settings).startswith("https://")


def _effective_origin(request: Request, settings: WebSettings) -> str:
    if settings.public_origin:
        return settings.public_origin
    forwarded = _trusted_forwarded_origin(request, settings)
    if forwarded:
        return forwarded
    host = request.headers.get("host", "")
    return _normalize_origin(f"{request.url.scheme}://{host}")


def _trusted_forwarded_origin(request: Request, settings: WebSettings) -> str:
    if not _client_is_trusted_proxy(request, settings):
        return ""
    forwarded_origin = _origin_from_forwarded_header(
        request.headers.get("forwarded", "")
    )
    if forwarded_origin:
        return forwarded_origin
    proto = _last_forwarded_header_value(
        request.headers.get("x-forwarded-proto", "")
    )
    host = _last_forwarded_header_value(
        request.headers.get("x-forwarded-host", "")
    )
    if proto and host:
        return _normalize_origin(f"{proto}://{host}")
    return ""


def _request_client_address(request: Request, settings: WebSettings) -> str:
    forwarded = _trusted_forwarded_client_address(request, settings)
    if forwarded:
        return forwarded
    if request.client is None:
        return ""
    return request.client.host


def _trusted_forwarded_client_address(
    request: Request,
    settings: WebSettings,
) -> str:
    if not _client_is_trusted_proxy(request, settings):
        return ""
    forwarded = _client_address_from_forwarded_header(
        request.headers.get("forwarded", "")
    )
    if forwarded:
        return forwarded
    forwarded_for = _last_forwarded_header_value(
        request.headers.get("x-forwarded-for", "")
    )
    return _normalize_forwarded_client_address(forwarded_for)


def _last_forwarded_header_value(value: str) -> str:
    for item in reversed(value.split(",")):
        stripped = item.strip()
        if stripped:
            return stripped
    return ""


def _client_address_from_forwarded_header(value: str) -> str:
    if not value:
        return ""
    hop = _last_forwarded_header_value(value)
    for segment in hop.split(";"):
        key, separator, raw = segment.strip().partition("=")
        if separator and key.lower() == "for":
            return _normalize_forwarded_client_address(raw)
    return ""


def _normalize_forwarded_client_address(value: str) -> str:
    raw = value.strip().strip('"')
    if not raw or raw.lower() == "unknown":
        return ""
    if raw.startswith("["):
        host, separator, _port = raw[1:].partition("]")
        return host if separator else raw
    host, separator, port = raw.rpartition(":")
    if separator and port.isdigit():
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return raw
        return host
    return raw


def _origin_from_forwarded_header(value: str) -> str:
    if not value:
        return ""
    hop = _last_forwarded_header_value(value)
    parts: dict[str, str] = {}
    for segment in hop.split(";"):
        key, separator, raw = segment.strip().partition("=")
        if separator:
            parts[key.lower()] = raw.strip().strip('"')
    proto = parts.get("proto", "")
    host = parts.get("host", "")
    if proto and host:
        return _normalize_origin(f"{proto}://{host}")
    return ""


def _client_is_trusted_proxy(request: Request, settings: WebSettings) -> bool:
    if request.client is None:
        return False
    try:
        address = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return any(address in network for network in settings.trusted_proxies)


def _raw_client_is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=403)


def _validate_startup_auth(settings: WebSettings) -> None:
    if settings.secure_cookies not in SECURE_COOKIE_MODES:
        raise WebConfigError("WUD_WEB_SECURE_COOKIES must be auto, true, or false")


def _validate_bind_host_allowed(settings: WebSettings, host: str) -> None:
    normalized = _normalize_host(host)
    if not normalized or normalized in {"0.0.0.0", "::"}:
        return
    if normalized in settings.allowed_hosts:
        return
    raise WebConfigError(
        f"WUD_WEB_ALLOWED_HOSTS must include {normalized} when binding "
        "the WebUI to that host. Set WUD_WEB_PUBLIC_ORIGIN to the "
        "browser-visible origin, or add extra aliases to WUD_WEB_ALLOWED_HOSTS."
    )


def _print_setup_claim(
    settings: WebSettings,
    *,
    host: str,
    port: int,
    claim: str,
) -> None:
    setup_url = _setup_url(settings, host=host, port=port, claim=claim)
    print("WUDup WebUI is not configured.", file=sys.stderr)
    print(file=sys.stderr)
    print("Open this one-time setup link to create the first admin account:", file=sys.stderr)
    print(file=sys.stderr)
    print(setup_url, file=sys.stderr)
    if host in {"0.0.0.0", "::"} and not settings.public_origin:
        print(file=sys.stderr)
        print(
            "Set WUD_WEB_PUBLIC_ORIGIN when exposing the WebUI through a "
            "LAN address or reverse proxy.",
            file=sys.stderr,
        )


def _setup_url(settings: WebSettings, *, host: str, port: int, claim: str) -> str:
    origin = settings.public_origin
    if not origin:
        display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        if ":" in display_host and not display_host.startswith("["):
            display_host = f"[{display_host}]"
        origin = f"http://{display_host}:{port}"
    query = urlencode({"claim": claim})
    return f"{origin}/#/setup?{query}"


def _reset_admin_url(
    settings: WebSettings,
    *,
    host: str,
    port: int,
    claim: str,
    username: str,
) -> str:
    origin = settings.public_origin
    if not origin:
        display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        if ":" in display_host and not display_host.startswith("["):
            display_host = f"[{display_host}]"
        origin = f"http://{display_host}:{port}"
    query = urlencode({"claim": claim, "user": username})
    return f"{origin}/#/reset-admin?{query}"


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise WebConfigError(f"invalid boolean value: {value}")


def _parse_origins(value: str) -> frozenset[str]:
    return frozenset(
        origin
        for origin in (_normalize_origin(item) for item in value.split(","))
        if origin
    )


def _parse_public_origin(value: str) -> str:
    if not value.strip():
        return ""
    origin = _normalize_origin(value)
    if not origin:
        raise WebConfigError("WUD_WEB_PUBLIC_ORIGIN must be an http(s) origin")
    return origin


def _parse_allowed_hosts(
    value: str,
    *,
    public_origin: str,
    bind_host: str,
) -> frozenset[str]:
    hosts = set(DEFAULT_ALLOWED_HOSTS)
    if public_origin:
        public_host = _host_from_origin(public_origin)
        if public_host:
            hosts.add(public_host)
    bind = _normalize_host(bind_host)
    if bind and bind not in {"0.0.0.0", "::"}:
        hosts.add(bind)
    for item in value.split(","):
        host = _normalize_host(item)
        if host:
            hosts.add(host)
    return frozenset(hosts)


def _parse_trusted_proxies(
    value: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    seen: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
    for item in value.split(","):
        raw = item.strip()
        if not raw:
            continue
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except ValueError as exc:
            if "/" in raw:
                raise WebConfigError(
                    f"WUD_WEB_TRUSTED_PROXIES contains invalid address: {raw}"
                ) from exc
            for network in _resolve_trusted_proxy_hostname(raw):
                if network not in seen:
                    networks.append(network)
                    seen.add(network)
            continue
        if network not in seen:
            networks.append(network)
            seen.add(network)
    return tuple(networks)


def _resolve_trusted_proxy_hostname(
    hostname: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    try:
        results = socket.getaddrinfo(
            hostname,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise WebConfigError(
            f"WUD_WEB_TRUSTED_PROXIES contains unresolvable hostname: {hostname}"
        ) from exc

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for family, _socktype, _proto, _canonname, sockaddr in results:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except (IndexError, ValueError):
            continue
        prefix = 32 if address.version == 4 else 128
        network = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
        networks.append(network)

    if not networks:
        raise WebConfigError(
            "WUD_WEB_TRUSTED_PROXIES hostname did not resolve to an IP address: "
            f"{hostname}"
        )
    return tuple(networks)


def _parse_secure_cookie_mode(value: str) -> str:
    normalized = value.strip().lower() or "auto"
    if normalized not in SECURE_COOKIE_MODES:
        raise WebConfigError("WUD_WEB_SECURE_COOKIES must be auto, true, or false")
    return normalized


def _normalize_origin(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        return ""
    host = parsed.hostname
    if not host:
        return ""
    netloc = parsed.netloc.lower()
    return f"{parsed.scheme.lower()}://{netloc}"


def _host_from_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    return _normalize_host(parsed.hostname or "")


def _normalize_host(value: str) -> str:
    raw = value.strip().lower().rstrip(".")
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end != -1 else ""
    if raw.count(":") == 1:
        host, _, port = raw.partition(":")
        if port.isdigit():
            return host
    return raw
