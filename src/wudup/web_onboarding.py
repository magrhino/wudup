"""WebUI onboarding checklist and tour route behavior."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from typing import cast

from fastapi import HTTPException, Request

from .db import DatabaseError, init_db, open_db, utc_timestamp
from .doctor import DoctorResult as DoctorDataResult
from .web_auth import (
    DEFAULT_ALLOWED_HOSTS,
    _safe_exception_detail,
    _set_web_setting,
    _settings,
    _web_setting,
)
from .web_database import ReadOnlyDatabaseMissing, connect_readonly_db
from .web_health import doctor_response, web_doctor_result
from .web_models import (
    DEFAULT_CORE_UPDATE_TOUR_STEP,
    CoreUpdateTourResponse,
    CoreUpdateTourStatus,
    CoreUpdateTourStep,
    CoreUpdateTourUpdateRequest,
    DoctorCheckResponse,
    DoctorCheckStatus,
    DoctorSuggestionResponse,
    OnboardingChecklistItem,
    OnboardingChecklistResponse,
    OnboardingDismissResponse,
    OnboardingDocLink,
    WebSettings,
)

ONBOARDING_DISMISSED_AT_KEY = "onboarding_checklist_dismissed_at"
CORE_UPDATE_TOUR_KEY = "onboarding_core_update_tour"
CORE_UPDATE_TOUR_STATUS_VALUES = (
    "not_started",
    "in_progress",
    "completed",
    "dismissed",
)
CORE_UPDATE_TOUR_STEP_VALUES = (
    "dashboard",
    "pending_select",
    "pending_preflight",
    "pending_apply",
    "runs_history",
)
ONBOARDING_REQUIRED_KEYS = frozenset(
    {
        "admin-setup",
        "wud-output",
        "wud-scripts",
        "docker-access",
        "compose-discovery",
        "persistence",
        "browser-access",
        "mutation-mode",
    }
)
ONBOARDING_STATUS_RANK: Mapping[DoctorCheckStatus, int] = {
    "PASS": 0,
    "WARN": 1,
    "FAIL": 2,
}
ONBOARDING_DOC_BASE = "https://github.com/magrhino/wudup/blob/main/docs"


def api_onboarding_checklist(request: Request) -> OnboardingChecklistResponse:
    settings = _settings(request)
    dismissed_at = onboarding_dismissed_at(settings)
    if dismissed_at:
        return OnboardingChecklistResponse(
            dismissed=True,
            dismissed_at=dismissed_at,
            all_passed=False,
            visible=False,
            items=[],
        )
    result = web_doctor_result(settings, request)
    return onboarding_checklist_response(
        settings,
        request,
        result,
        dismissed_at=dismissed_at,
    )


def api_onboarding_dismiss(request: Request) -> OnboardingDismissResponse:
    settings = _settings(request)
    dismissed_at = utc_timestamp()
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            with conn:
                _set_web_setting(conn, ONBOARDING_DISMISSED_AT_KEY, dismissed_at)
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not dismiss onboarding checklist",
                exc,
            ),
        ) from exc
    return OnboardingDismissResponse(dismissed=True, dismissed_at=dismissed_at)


def api_core_update_tour(request: Request) -> CoreUpdateTourResponse:
    return core_update_tour_response(_settings(request))


def api_update_core_update_tour(
    payload: CoreUpdateTourUpdateRequest,
    request: Request,
) -> CoreUpdateTourResponse:
    settings = _settings(request)
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            with conn:
                return set_core_update_tour_state(
                    conn,
                    status=payload.status,
                    step=payload.step,
                )
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not update core update tour",
                exc,
            ),
        ) from exc


def onboarding_checklist_response(
    settings: WebSettings,
    request: Request,
    result: DoctorDataResult,
    *,
    dismissed_at: str,
) -> OnboardingChecklistResponse:
    doctor = doctor_response(settings, result)
    checks = doctor.checks
    items = [
        _onboarding_admin_item(settings),
        _onboarding_item_from_checks(
            key="wud-output",
            title="Shared WUD output file",
            checks=_checks_by_code(
                checks,
                {"wud-out-file-directory", "wud-out-file"},
            ),
            pass_detail=(
                "WUD_OUT_FILE points at a writable shared location that WUD can "
                "create or update."
            ),
            missing_detail="WUD_OUT_FILE readiness was not reported by doctor.",
            docs=[
                _onboarding_doc(
                    "WebUI container setup",
                    f"{ONBOARDING_DOC_BASE}/wiki/webui-container.md#start-the-webui",
                )
            ],
        ),
        _onboarding_item_from_checks(
            key="wud-scripts",
            title="WUD callback scripts",
            checks=_checks_by_code(
                checks,
                {"packaged-wud-scripts", "wud-script-sync"},
            ),
            pass_detail=(
                "Packaged WUD scripts are available and script sync can update "
                "the managed trigger directory."
            ),
            missing_detail="WUD script sync readiness was not reported by doctor.",
            docs=[
                _onboarding_doc(
                    "Script sync notes",
                    f"{ONBOARDING_DOC_BASE}/wiki/container-script-sync.md",
                )
            ],
        ),
        _onboarding_item_from_checks(
            key="docker-access",
            title="Docker daemon access",
            checks=_checks_by_code(
                checks,
                {
                    "docker-endpoint",
                    "docker-socket",
                    "docker-daemon-version",
                    "docker-daemon-info",
                    "docker-container-listing",
                },
            ),
            pass_detail="The WebUI helper can reach Docker and list containers.",
            missing_detail="Docker daemon readiness was not reported by doctor.",
            docs=[
                _onboarding_doc(
                    "Deployment Docker access",
                    f"{ONBOARDING_DOC_BASE}/DEPLOYMENT.md#requirements",
                )
            ],
        ),
        _onboarding_item_from_checks(
            key="compose-discovery",
            title="Compose stack discovery",
            checks=_compose_onboarding_checks(checks),
            pass_detail=(
                "Compose stacks render under DOCKER_BASE and any HOST_DOCKER_BASE "
                "mapping is usable."
            ),
            missing_detail="Compose discovery readiness was not reported by doctor.",
            docs=[
                _onboarding_doc(
                    "Path mapping",
                    f"{ONBOARDING_DOC_BASE}/DEPLOYMENT.md#docker-compose",
                )
            ],
        ),
        _onboarding_item_from_checks(
            key="persistence",
            title="Logs and SQLite persistence",
            checks=_checks_by_code(checks, {"wud-log-dir", "webui-database"}),
            pass_detail=(
                "The log directory is writable and the WebUI database is ready."
            ),
            missing_detail="Persistence readiness was not reported by doctor.",
            docs=[
                _onboarding_doc(
                    "First login",
                    f"{ONBOARDING_DOC_BASE}/wiki/webui-container.md#first-login",
                )
            ],
        ),
        _browser_access_onboarding_item(settings, checks),
        _mutation_onboarding_item(settings, checks),
    ]
    all_passed = all(
        item.status == "PASS"
        for item in items
        if item.key in ONBOARDING_REQUIRED_KEYS
    )
    dismissed = bool(dismissed_at)
    return OnboardingChecklistResponse(
        dismissed=dismissed,
        dismissed_at=dismissed_at,
        all_passed=all_passed,
        visible=not dismissed and not all_passed,
        items=items,
    )


def core_update_tour_response(settings: WebSettings) -> CoreUpdateTourResponse:
    try:
        with closing(connect_readonly_db(settings)) as conn:
            return core_update_tour_response_from_conn(conn)
    except ReadOnlyDatabaseMissing:
        return _default_core_update_tour_response()
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read core update tour state",
                exc,
            ),
        ) from exc


def core_update_tour_response_from_conn(
    conn: sqlite3.Connection,
) -> CoreUpdateTourResponse:
    row = conn.execute(
        """
        SELECT value, updated_at
        FROM web_settings
        WHERE key = ?
        LIMIT 1
        """,
        (CORE_UPDATE_TOUR_KEY,),
    ).fetchone()
    if row is None:
        return _default_core_update_tour_response()
    return _core_update_tour_response_from_value(
        str(row["value"]),
        str(row["updated_at"] or ""),
    )


def set_core_update_tour_state(
    conn: sqlite3.Connection,
    *,
    status: CoreUpdateTourStatus,
    step: CoreUpdateTourStep,
) -> CoreUpdateTourResponse:
    value = json.dumps({"status": status, "step": step}, sort_keys=True)
    _set_web_setting(conn, CORE_UPDATE_TOUR_KEY, value)
    return core_update_tour_response_from_conn(conn)


def onboarding_dismissed_at(settings: WebSettings) -> str:
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            return _web_setting(conn, ONBOARDING_DISMISSED_AT_KEY)
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        raise HTTPException(
            status_code=500,
            detail=_safe_exception_detail(
                settings,
                "could not read onboarding checklist state",
                exc,
            ),
        ) from exc


def _onboarding_admin_item(settings: WebSettings) -> OnboardingChecklistItem:
    if settings.dev_no_auth:
        status: DoctorCheckStatus = "WARN"
        detail = (
            "Development auth bypass is active; first-admin setup is skipped for "
            "this process."
        )
    else:
        status = "PASS"
        detail = "The first admin account exists and browser authentication is active."
    return OnboardingChecklistItem(
        key="admin-setup",
        title="Admin setup",
        status=status,
        detail=detail,
        check_codes=["webui-authentication"],
        suggestions=()
        if not settings.dev_no_auth
        else [
            DoctorSuggestionResponse(
                label="Require browser authentication",
                description=(
                    "Disable the local development auth bypass before exposing "
                    "the WebUI."
                ),
                snippet="WUD_WEB_DEV_NO_AUTH=false",
            )
        ],
        docs=[
            _onboarding_doc(
                "First login",
                f"{ONBOARDING_DOC_BASE}/wiki/webui-container.md#first-login",
            )
        ],
    )


def _browser_access_onboarding_item(
    settings: WebSettings,
    checks: Sequence[DoctorCheckResponse],
) -> OnboardingChecklistItem:
    relevant = _checks_by_code(
        checks,
        {
            "webui-authentication",
            "webui-allowed-hosts",
            "webui-public-origin",
            "webui-secure-cookies",
            "webui-trusted-proxies",
        },
    )
    failures = [check for check in relevant if check.status == "FAIL"]
    if failures:
        return _onboarding_item_from_checks(
            key="browser-access",
            title="Browser access safety",
            checks=relevant,
            pass_detail="Browser access safety checks passed.",
            missing_detail="Browser access readiness was not reported by doctor.",
            docs=_browser_access_docs(),
        )

    if _loopback_only_browser_access(settings):
        status: DoctorCheckStatus = "PASS"
        detail = "Browser access is limited to loopback hosts for first run."
    elif settings.public_origin:
        status = "PASS"
        detail = "Public origin and allowed hosts are configured for browser access."
    else:
        status = "WARN"
        detail = (
            "Browser origin is derived from the request. Configure "
            "WUD_WEB_PUBLIC_ORIGIN before LAN or reverse-proxy exposure."
        )
    return OnboardingChecklistItem(
        key="browser-access",
        title="Browser access safety",
        status=status,
        detail=detail,
        check_codes=_check_codes(relevant),
        suggestions=[]
        if status == "PASS"
        else _dedupe_suggestions(
            suggestion
            for check in relevant
            for suggestion in check.suggestions
            if check.status != "PASS"
        ),
        docs=_browser_access_docs(),
    )


def _mutation_onboarding_item(
    settings: WebSettings,
    checks: Sequence[DoctorCheckResponse],
) -> OnboardingChecklistItem:
    relevant = _checks_by_code(checks, {"webui-mutation-gate"})
    check = relevant[0] if relevant else None
    suggestions = check.suggestions if check is not None else []
    return OnboardingChecklistItem(
        key="mutation-mode",
        title="Browser mutation mode",
        status="WARN" if settings.mutations_enabled else "PASS",
        detail=(
            "Browser apply controls are server-side enabled; keep this intentional."
            if settings.mutations_enabled
            else "Browser apply controls are disabled server-side, so the WebUI is read-only."
        ),
        check_codes=_check_codes(relevant) or ["webui-mutation-gate"],
        suggestions=suggestions,
        docs=[
            _onboarding_doc(
                "Read-only and mutations",
                f"{ONBOARDING_DOC_BASE}/wiki/webui-container.md#read-only-and-mutations",
            )
        ],
    )


def _onboarding_item_from_checks(
    *,
    key: str,
    title: str,
    checks: Sequence[DoctorCheckResponse],
    pass_detail: str,
    missing_detail: str,
    docs: Sequence[OnboardingDocLink],
) -> OnboardingChecklistItem:
    status = _aggregate_onboarding_status(checks)
    return OnboardingChecklistItem(
        key=key,
        title=title,
        status=status,
        detail=_onboarding_detail(checks, status, pass_detail, missing_detail),
        check_codes=_check_codes(checks),
        suggestions=_dedupe_suggestions(
            suggestion
            for check in checks
            for suggestion in check.suggestions
            if check.status != "PASS"
        ),
        docs=list(docs),
    )


def _aggregate_onboarding_status(
    checks: Sequence[DoctorCheckResponse],
) -> DoctorCheckStatus:
    if not checks:
        return "WARN"
    return max(checks, key=lambda check: ONBOARDING_STATUS_RANK[check.status]).status


def _onboarding_detail(
    checks: Sequence[DoctorCheckResponse],
    status: DoctorCheckStatus,
    pass_detail: str,
    missing_detail: str,
) -> str:
    if not checks:
        return missing_detail
    if status == "PASS":
        return pass_detail
    details = [
        f"{check.name}: {check.detail}" if check.detail else check.name
        for check in checks
        if check.status == status
    ]
    return "; ".join(details[:3]) or missing_detail


def _checks_by_code(
    checks: Sequence[DoctorCheckResponse],
    codes: set[str],
) -> list[DoctorCheckResponse]:
    return [check for check in checks if check.code in codes]


def _compose_onboarding_checks(
    checks: Sequence[DoctorCheckResponse],
) -> list[DoctorCheckResponse]:
    return [
        check
        for check in checks
        if check.category == "compose"
        or check.code.startswith("host-docker-base-mapping")
    ]


def _check_codes(checks: Sequence[DoctorCheckResponse]) -> list[str]:
    return [check.code for check in checks]


def _dedupe_suggestions(
    suggestions: Sequence[DoctorSuggestionResponse] | Iterator[DoctorSuggestionResponse],
) -> list[DoctorSuggestionResponse]:
    seen: set[tuple[str, str]] = set()
    deduped: list[DoctorSuggestionResponse] = []
    for suggestion in suggestions:
        key = (suggestion.label, suggestion.snippet)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(suggestion)
    return deduped


def _browser_access_docs() -> list[OnboardingDocLink]:
    return [
        _onboarding_doc(
            "Network exposure",
            f"{ONBOARDING_DOC_BASE}/wiki/webui-container.md#network-exposure",
        )
    ]


def _onboarding_doc(label: str, url: str) -> OnboardingDocLink:
    return OnboardingDocLink(label=label, url=url)


def _default_core_update_tour_response() -> CoreUpdateTourResponse:
    return CoreUpdateTourResponse(
        status="not_started",
        step=DEFAULT_CORE_UPDATE_TOUR_STEP,
        updated_at="",
    )


def _core_update_tour_response_from_value(
    raw_value: str,
    updated_at: str,
) -> CoreUpdateTourResponse:
    try:
        decoded = json.loads(raw_value) if raw_value else {}
    except json.JSONDecodeError:
        decoded = {}
    if not isinstance(decoded, Mapping):
        decoded = {}
    status = str(decoded.get("status", ""))
    step = str(decoded.get("step", ""))
    if status not in CORE_UPDATE_TOUR_STATUS_VALUES:
        status = "not_started"
    if step not in CORE_UPDATE_TOUR_STEP_VALUES:
        step = DEFAULT_CORE_UPDATE_TOUR_STEP
    return CoreUpdateTourResponse(
        status=cast(CoreUpdateTourStatus, status),
        step=cast(CoreUpdateTourStep, step),
        updated_at=updated_at,
    )


def _loopback_only_browser_access(settings: WebSettings) -> bool:
    return (
        not settings.public_origin
        and bool(settings.allowed_hosts)
        and settings.allowed_hosts.issubset(DEFAULT_ALLOWED_HOSTS)
    )
