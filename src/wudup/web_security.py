"""WebUI candidate security scan route handlers."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, replace
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from . import web_jobs
from .command import CommandRunner
from .config import ConfigError, parse_bool_env
from .db import DatabaseError, init_db, open_db, utc_timestamp
from .digest_verifier import ResolvedImageSubject
from .platforms import platform_value
from .security_scanner import SecurityScanResult, TrivyScanner, _http_url
from .security_severity import normalize_security_severity
from .security_store import (
    cached_scan_by_request,
    cached_scan_by_request_or_unambiguous_platform,
    upsert_scan_result,
)
from .security_subjects import (
    PENDING_SECURITY_CACHE_OPTIONS,
    PENDING_SECURITY_READ_OPTIONS,
    PendingSecurityContext,
    PendingSecurityRequest,
    current_security_request,
    default_digest_verifier,
    pending_security_context,
    resolve_current_security_subject,
    resolve_security_subject,
)
from .web_auth import (
    _redact_sensitive_text,
    _redact_unknown_absolute_paths,
    _safe_exception_detail,
    _settings,
)
from .web_database import (
    ReadOnlyDatabaseMissing,
)
from .web_database import (
    connect_readonly_db as _connect_readonly_db,
)
from .web_models import (
    DEFAULT_SECURITY_SCAN_CACHE_DIR,
    SecurityScanComparison,
    SecurityScanConfig,
    SecurityScanFinding,
    SecurityScanInfo,
    SecurityScanJobResponse,
    SecurityScanSeverityCounts,
    SecurityScansResponse,
    SecurityScanSubject,
    WebSettings,
)

WUD_SECURITY_SCANNING_ENABLED_ENV = "WUD_SECURITY_SCANNING_ENABLED"
WUD_SECURITY_SCANNER_EXECUTABLE_ENV = "WUD_SECURITY_SCANNER_EXECUTABLE"
WUD_SECURITY_SCAN_CACHE_DIR_ENV = "WUD_SECURITY_SCAN_CACHE_DIR"
WUD_SECURITY_SCAN_TIMEOUT_SECONDS_ENV = "WUD_SECURITY_SCAN_TIMEOUT_SECONDS"
DEFAULT_SECURITY_SCAN_TIMEOUT_SECONDS = 300
INSTALLED_DIGEST_UNAVAILABLE_MESSAGE = "Installed digest is unavailable."


@dataclass
class WebSecurityScanJob:
    id: str
    status: str
    total_count: int = 0
    completed_count: int = 0
    result: SecurityScansResponse | None = None
    error: str = ""


def configured_security_scan_config(
    environ: Mapping[str, str],
) -> SecurityScanConfig:
    return SecurityScanConfig(
        enabled=parse_bool_env(
            WUD_SECURITY_SCANNING_ENABLED_ENV,
            environ.get(WUD_SECURITY_SCANNING_ENABLED_ENV),
            default=False,
        ),
        executable=(
            environ.get(WUD_SECURITY_SCANNER_EXECUTABLE_ENV, "").strip() or "trivy"
        ),
        cache_dir=(
            environ.get(WUD_SECURITY_SCAN_CACHE_DIR_ENV, "").strip()
            or DEFAULT_SECURITY_SCAN_CACHE_DIR
        ),
        timeout_seconds=_parse_positive_int(
            WUD_SECURITY_SCAN_TIMEOUT_SECONDS_ENV,
            environ.get(WUD_SECURITY_SCAN_TIMEOUT_SECONDS_ENV),
            DEFAULT_SECURITY_SCAN_TIMEOUT_SECONDS,
        ),
    )


def initialize_security_scan_state(state: Any, _settings: WebSettings) -> None:
    state.web_security_scan_executor = ThreadPoolExecutor(max_workers=1)
    state.web_security_scan_lock = Lock()
    state.web_security_scan_jobs = {}


def shutdown_security_scan_state(state: Any) -> None:
    executor: ThreadPoolExecutor = state.web_security_scan_executor
    executor.shutdown(wait=False, cancel_futures=True)


def api_security_scans(request: Request) -> SecurityScansResponse:
    settings = _settings(request)
    return security_scans_response(settings)


def register_security_scan_routes(
    router: APIRouter,
    post_only_method_not_allowed: Callable[[], object],
) -> None:
    router.add_api_route(
        "/security-scans",
        api_security_scans,
        methods=["GET"],
        response_model=SecurityScansResponse,
    )
    router.add_api_route(
        "/security-scans/refresh",
        api_refresh_security_scans,
        methods=["POST"],
        response_model=SecurityScanJobResponse,
    )
    router.add_api_route(
        "/security-scans/refresh",
        post_only_method_not_allowed,
        methods=["GET"],
    )
    router.add_api_route(
        "/security-scans/jobs/{job_id}",
        api_security_scan_job,
        methods=["GET"],
        response_model=SecurityScanJobResponse,
    )


def api_refresh_security_scans(request: Request) -> SecurityScanJobResponse:
    settings = _settings(request)
    if not settings.security_scan.enabled:
        raise HTTPException(status_code=403, detail="security scanning is disabled")
    if not settings.mutations_enabled:
        raise HTTPException(status_code=403, detail="mutations are disabled")

    state = request.app.state
    job: WebSecurityScanJob | None = None

    def reserve_scan_job() -> None:
        nonlocal job
        with state.web_security_scan_lock:
            jobs: dict[str, WebSecurityScanJob] = state.web_security_scan_jobs
            if _active_security_scan_jobs_unlocked(jobs):
                raise HTTPException(
                    status_code=409,
                    detail="security scan refresh is already running",
                )
            job = WebSecurityScanJob(
                id=secrets.token_urlsafe(18),
                status="queued",
            )
            jobs[job.id] = job
            _prune_security_scan_jobs_unlocked(jobs)

    active_error = web_jobs._reserve_mutation_state(
        state,
        reserve_scan_job,
        include_security_scan_jobs=False,
    )
    if active_error:
        raise HTTPException(status_code=409, detail=active_error)
    assert job is not None
    try:
        state.web_security_scan_executor.submit(
            _run_security_scan_job,
            state,
            settings,
            job.id,
        )
    except Exception:
        with state.web_security_scan_lock:
            state.web_security_scan_jobs.pop(job.id, None)
        raise
    return _job_response(job)


def api_security_scan_job(job_id: str, request: Request) -> SecurityScanJobResponse:
    lock: Lock = request.app.state.web_security_scan_lock
    jobs: dict[str, WebSecurityScanJob] = request.app.state.web_security_scan_jobs
    with lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="security scan job not found")
        return _job_response(job)


def security_scans_response(settings: WebSettings) -> SecurityScansResponse:
    context = pending_security_context(
        settings,
        options=(
            PENDING_SECURITY_READ_OPTIONS
            if settings.security_scan.enabled
            else PENDING_SECURITY_CACHE_OPTIONS
        ),
    )
    if not settings.security_scan.enabled:
        return _response_from_items(
            settings,
            context,
            [_placeholder_info(request, state="disabled") for request in context.requests],
        )
    try:
        with closing(_connect_readonly_db(settings)) as conn:
            items = [_cached_scan_info(conn, request) for request in context.requests]
    except ReadOnlyDatabaseMissing:
        items = _placeholder_items(context)
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        if _missing_security_scan_cache_table(exc):
            items = _placeholder_items(context)
        else:
            raise HTTPException(
                status_code=500,
                detail=_safe_exception_detail(
                    settings,
                    "could not read security scan cache",
                    exc,
                ),
            ) from exc
    return _response_from_items(settings, context, items)


def _cached_scan_info(
    conn: sqlite3.Connection,
    request: PendingSecurityRequest,
) -> SecurityScanInfo:
    info = cached_scan_by_request_or_unambiguous_platform(conn, request)
    if info is None:
        return _placeholder_info(request, state=_placeholder_state(request))
    return _attach_cached_comparison(conn, request, info)


def _placeholder_items(context: PendingSecurityContext) -> list[SecurityScanInfo]:
    return [
        _placeholder_info(request, state=_placeholder_state(request))
        for request in context.requests
    ]


def _missing_security_scan_cache_table(exc: BaseException) -> bool:
    message = str(exc)
    return (
        "Missing expected table: security_scan_cache" in message
        or "no such table: security_scan_cache" in message
    )


def _run_security_scan_job(
    state: Any,
    settings: WebSettings,
    job_id: str,
) -> None:
    _update_job(state, job_id, status="running")
    items: list[SecurityScanInfo] = []
    current_scans: dict[tuple[str, str, str], SecurityScanInfo] = {}
    try:
        context = pending_security_context(settings)
        _update_job(state, job_id, total_count=len(context.requests))
        scanner = TrivyScanner(
            settings.security_scan,
            runner=CommandRunner(env=settings.command_env),
        )
        verifier = default_digest_verifier(settings)
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            for request in context.requests:
                info = _scan_request(
                    settings,
                    conn,
                    scanner,
                    verifier,
                    request,
                    current_scans,
                )
                items.append(info)
                _update_job(
                    state,
                    job_id,
                    completed_count=len(items),
                )
        response = _response_from_items(settings, context, items)
        _update_job(state, job_id, status="success", result=response)
    except Exception as exc:  # noqa: BLE001 - surfaced as sanitized job error.
        _update_job(
            state,
            job_id,
            status="failure",
            error=_safe_exception_detail(settings, "security scan refresh failed", exc),
        )


def _scan_request(
    settings: WebSettings,
    conn: sqlite3.Connection,
    scanner: TrivyScanner,
    verifier: Any,
    request: PendingSecurityRequest,
    current_scans: dict[tuple[str, str, str], SecurityScanInfo],
) -> SecurityScanInfo:
    cached = cached_scan_by_request(conn, request)
    # ponytail: demo fixtures use synthetic digests; keep them stable on refresh.
    if cached is not None and cached.scanner_version == "demo":
        return _attach_cached_comparison(conn, request, cached)
    if request.identity_status != "pending":
        subject = resolve_security_subject(request, verifier)
        return _cache_subject_resolution(settings, conn, request, subject)
    subject = resolve_security_subject(request, verifier)
    if subject.identity_status != "exact":
        return _cache_subject_resolution(settings, conn, request, subject)
    result = scanner.scan(subject)
    result = _sanitize_scan_result(settings, result)
    upsert_scan_result(
        conn,
        request,
        subject,
        result,
        timestamp=utc_timestamp(),
    )
    cached = cached_scan_by_request(conn, request)
    if cached is not None:
        return _attach_refreshed_comparison(
            settings,
            conn,
            scanner,
            verifier,
            request,
            cached,
            subject,
            current_scans,
        )
    return _attach_refreshed_comparison(
        settings,
        conn,
        scanner,
        verifier,
        request,
        _result_info(request, subject, result),
        subject,
        current_scans,
    )


def _cache_subject_resolution(
    settings: WebSettings,
    conn: sqlite3.Connection,
    request: PendingSecurityRequest,
    subject: ResolvedImageSubject,
) -> SecurityScanInfo:
    state = _state_from_subject(subject)
    result = SecurityScanResult(
        state=state,
        error_code=subject.identity_status,
        error_message=subject.error,
    )
    result = _sanitize_scan_result(settings, result)
    upsert_scan_result(
        conn,
        request,
        subject,
        result,
        timestamp=utc_timestamp(),
    )
    cached = cached_scan_by_request(conn, request)
    if cached is not None:
        return cached
    return _subject_info(request, subject, state=state)


def _response_from_items(
    settings: WebSettings,
    context: PendingSecurityContext,
    items: Sequence[SecurityScanInfo],
) -> SecurityScansResponse:
    return SecurityScansResponse(
        source_file=context.source.source_file,
        source=context.source.response_source(),
        source_hash=context.source.source_hash,
        scanning_enabled=settings.security_scan.enabled,
        count=len(items),
        items=_sanitize_scan_items(settings, items),
        warnings=[
            _sanitize_text(settings, warning)
            for warning in (*context.source.warnings, *context.warnings)
        ],
    )


def _placeholder_info(
    request: PendingSecurityRequest,
    *,
    state: str,
) -> SecurityScanInfo:
    return SecurityScanInfo(
        line_no=request.line_no,
        state=state,  # type: ignore[arg-type]
        verdict="unknown",
        error_code="" if request.identity_status == "pending" else request.identity_status,
        error_message=request.error,
        warnings=list(request.warnings),
    )


def _subject_info(
    request: PendingSecurityRequest,
    subject: ResolvedImageSubject,
    *,
    state: str,
) -> SecurityScanInfo:
    return SecurityScanInfo(
        line_no=request.line_no,
        state=state,  # type: ignore[arg-type]
        verdict="unknown",
        error_code=subject.identity_status,
        error_message=subject.error,
        subject=_subject_model(subject),
        warnings=list(subject.warnings),
    )


def _result_info(
    request: PendingSecurityRequest,
    subject: ResolvedImageSubject,
    result: SecurityScanResult,
) -> SecurityScanInfo:
    return SecurityScanInfo(
        line_no=request.line_no,
        state=result.state,  # type: ignore[arg-type]
        verdict=result.verdict,  # type: ignore[arg-type]
        scanner=result.scanner,
        scanner_version=result.scanner_version,
        scanner_schema=result.scanner_schema,
        db_revision=result.db_revision,
        db_updated_at=result.db_updated_at,
        severity_counts=_counts_model(result.severity_counts),
        advisory_counts=_counts_model(result.advisory_counts),
        advisory_counts_known=result.state == "complete" and bool(result.advisory_counts),
        fixable_counts=_counts_model(result.fixable_counts),
        unfixed_count=result.unfixed_count,
        subject=_subject_model(subject),
        findings=[
            SecurityScanFinding(
                target=finding.target,
                target_class=finding.target_class,
                target_type=finding.target_type,
                vulnerability_id=finding.vulnerability_id,
                package_name=finding.package_name,
                installed_version=finding.installed_version,
                fixed_version=finding.fixed_version,
                severity=normalize_security_severity(finding.severity),
                title=finding.title,
                primary_url=finding.primary_url,
            )
            for finding in result.findings
        ],
        warnings=[*subject.warnings, *result.warnings],
        error_code=result.error_code,
        error_message=result.error_message,
    )


def _subject_model(subject: ResolvedImageSubject) -> SecurityScanSubject:
    return SecurityScanSubject(
        requested_ref=subject.requested_ref,
        reported_digest=subject.reported_digest,
        index_digest=subject.index_digest,
        manifest_digest=subject.manifest_digest,
        immutable_ref=subject.immutable_ref,
        platform=subject.platform,
    )


def _attach_cached_comparison(
    conn: sqlite3.Connection,
    request: PendingSecurityRequest,
    candidate: SecurityScanInfo,
) -> SecurityScanInfo:
    return _attach_current_comparison(
        request,
        candidate,
        candidate.subject,
        lambda current_request: cached_scan_by_request_or_unambiguous_platform(
            conn,
            current_request,
        ),
        missing_current_message="Installed digest has not been scanned yet.",
    )


def _attach_refreshed_comparison(
    settings: WebSettings,
    conn: sqlite3.Connection,
    scanner: TrivyScanner,
    verifier: Any,
    request: PendingSecurityRequest,
    candidate: SecurityScanInfo,
    candidate_subject: ResolvedImageSubject,
    current_scans: dict[tuple[str, str, str], SecurityScanInfo],
) -> SecurityScanInfo:
    if candidate.state != "complete":
        return candidate
    return _attach_current_comparison(
        request,
        candidate,
        _subject_model(candidate_subject),
        lambda current_request: _scan_current_request(
            settings,
            conn,
            scanner,
            verifier,
            request,
            current_request,
            current_scans,
        ),
        missing_current_message=INSTALLED_DIGEST_UNAVAILABLE_MESSAGE,
    )


def _attach_current_comparison(
    request: PendingSecurityRequest,
    candidate: SecurityScanInfo,
    candidate_subject: SecurityScanSubject,
    current_info: Callable[[PendingSecurityRequest], SecurityScanInfo | None],
    *,
    missing_current_message: str,
) -> SecurityScanInfo:
    current_request = current_security_request(request)
    if current_request is None:
        return _with_comparison(
            candidate,
            _unknown_comparison(INSTALLED_DIGEST_UNAVAILABLE_MESSAGE),
        )
    if _same_subject(candidate_subject, current_request):
        return _with_comparison(candidate, _comparison(candidate, candidate))
    current = current_info(current_request)
    if current is None:
        return _with_comparison(
            candidate,
            _unknown_comparison(missing_current_message),
        )
    return _with_comparison(candidate, _comparison(current, candidate))


def _scan_current_request(
    settings: WebSettings,
    conn: sqlite3.Connection,
    scanner: TrivyScanner,
    verifier: Any,
    request: PendingSecurityRequest,
    current_request: PendingSecurityRequest,
    current_scans: dict[tuple[str, str, str], SecurityScanInfo],
) -> SecurityScanInfo | None:
    cached = cached_scan_by_request(conn, current_request)
    # ponytail: demo fixtures use synthetic digests; keep them stable on refresh.
    if cached is not None and cached.scanner_version == "demo":
        current_scans[_current_scan_cache_key(current_request)] = cached
        return cached
    cache_key = _current_scan_cache_key(current_request)
    if cache_key in current_scans:
        return current_scans[cache_key]
    subject = resolve_current_security_subject(request, verifier)
    if subject.identity_status != "exact":
        current = _cache_subject_resolution(settings, conn, current_request, subject)
        current_scans[cache_key] = current
        return current
    result = scanner.scan(subject)
    result = _sanitize_scan_result(settings, result)
    upsert_scan_result(
        conn,
        current_request,
        subject,
        result,
        timestamp=utc_timestamp(),
    )
    current = cached_scan_by_request(conn, current_request) or _result_info(
        current_request,
        subject,
        result,
    )
    current_scans[cache_key] = current
    return current


def _current_scan_cache_key(
    current_request: PendingSecurityRequest,
) -> tuple[str, str, str]:
    return (
        current_request.candidate_image,
        current_request.reported_digest,
        platform_value(current_request.platform),
    )


def _comparison(
    current: SecurityScanInfo,
    candidate: SecurityScanInfo,
) -> SecurityScanComparison:
    if current.state != "complete" or candidate.state != "complete":
        return _unknown_comparison("Both installed and candidate scans must complete.")
    mismatch_message = _comparison_mismatch_message(current, candidate)
    if mismatch_message:
        return _unknown_comparison(mismatch_message)
    if (current.verdict == "findings" and not current.findings) or (
        candidate.verdict == "findings" and not candidate.findings
    ):
        return _unknown_comparison("Refresh scans to collect vulnerability rows.")

    current_by_key = _findings_by_key(current.findings)
    candidate_by_key = _findings_by_key(candidate.findings)
    fixed: list[SecurityScanFinding] = []
    remaining: list[SecurityScanFinding] = []
    introduced: list[SecurityScanFinding] = []
    for key in sorted(set(current_by_key) | set(candidate_by_key)):
        current_rows = current_by_key.get(key, [])
        candidate_rows = candidate_by_key.get(key, [])
        shared_count = min(len(current_rows), len(candidate_rows))
        remaining.extend(candidate_rows[:shared_count])
        fixed.extend(current_rows[shared_count:])
        introduced.extend(candidate_rows[shared_count:])
    status = _comparison_status(fixed, remaining, introduced)
    return SecurityScanComparison(
        status=status,  # type: ignore[arg-type]
        current_subject=current.subject,
        fixed_findings=fixed,
        remaining_findings=remaining,
        introduced_findings=introduced,
        message=_comparison_message(status, fixed, remaining, introduced),
    )


def _unknown_comparison(message: str) -> SecurityScanComparison:
    return SecurityScanComparison(status="unknown", message=message)


def _comparison_mismatch_message(
    current: SecurityScanInfo,
    candidate: SecurityScanInfo,
) -> str:
    if current.scanner != candidate.scanner:
        return "Installed and candidate scans used different scanners."
    if not current.scanner_version or not candidate.scanner_version:
        return "Scanner version provenance is unavailable."
    if current.scanner_version != candidate.scanner_version:
        return "Installed and candidate scans used different scanner versions."
    if not current.scanner_schema or not candidate.scanner_schema:
        return "Scanner schema provenance is unavailable."
    if current.scanner_schema != candidate.scanner_schema:
        return "Installed and candidate scans used different scanner schemas."
    if not _database_identity(current) or not _database_identity(candidate):
        return "Vulnerability database provenance is unavailable."
    if _database_identity(current) != _database_identity(candidate):
        return "Installed and candidate scans used different vulnerability databases."
    if current.subject.platform != candidate.subject.platform:
        return "Installed and candidate scans used different platforms."
    return ""


def _database_identity(info: SecurityScanInfo) -> tuple[str, str] | None:
    if not info.db_revision or not info.db_updated_at:
        return None
    return (info.db_revision, info.db_updated_at)


def _comparison_status(
    fixed: Sequence[SecurityScanFinding],
    remaining: Sequence[SecurityScanFinding],
    introduced: Sequence[SecurityScanFinding],
) -> str:
    if introduced and not fixed:
        return "worse"
    if fixed and (remaining or introduced):
        return "mixed"
    if fixed:
        return "improved"
    return "unchanged"


def _comparison_message(
    status: str,
    fixed: Sequence[SecurityScanFinding],
    remaining: Sequence[SecurityScanFinding],
    introduced: Sequence[SecurityScanFinding],
) -> str:
    if status == "improved":
        return f"Candidate removes {len(fixed)} reported finding(s)."
    if status == "mixed":
        return (
            f"Candidate removes {len(fixed)} finding(s), leaves "
            f"{len(remaining)}, and introduces {len(introduced)}."
        )
    if status == "worse":
        return f"Candidate introduces {len(introduced)} reported finding(s)."
    if remaining:
        return f"Candidate keeps {len(remaining)} reported finding(s)."
    return "No reported findings changed between installed and candidate images."


def _finding_key(finding: SecurityScanFinding) -> tuple[str, str, str, str, str]:
    return (
        finding.target,
        finding.target_class,
        finding.target_type,
        finding.package_name,
        finding.vulnerability_id,
    )


def _findings_by_key(
    findings: Sequence[SecurityScanFinding],
) -> dict[tuple[str, str, str, str, str], list[SecurityScanFinding]]:
    grouped: dict[tuple[str, str, str, str, str], list[SecurityScanFinding]] = {}
    for finding in findings:
        grouped.setdefault(_finding_key(finding), []).append(finding)
    return grouped


def _with_comparison(
    info: SecurityScanInfo,
    comparison: SecurityScanComparison,
) -> SecurityScanInfo:
    return info.model_copy(update={"comparison": comparison})


def _same_subject(
    subject: SecurityScanSubject,
    request: PendingSecurityRequest,
) -> bool:
    if subject.platform != platform_value(request.platform):
        return False
    digest = request.reported_digest
    return bool(digest and digest in {subject.reported_digest, subject.manifest_digest})


def _placeholder_state(request: PendingSecurityRequest) -> str:
    if request.identity_status == "pending":
        return "not_scanned"
    if request.identity_status in {"mismatch", "unsupported"}:
        return "unsupported"
    return "error"


def _state_from_subject(subject: ResolvedImageSubject) -> str:
    if subject.identity_status == "stale":
        return "stale"
    if subject.identity_status == "auth_required":
        return "auth_required"
    if subject.identity_status == "mismatch":
        return "unsupported"
    if subject.identity_status == "unsupported":
        return "unsupported"
    return "error"


def _sanitize_scan_result(
    settings: WebSettings,
    result: SecurityScanResult,
) -> SecurityScanResult:
    return replace(
        result,
        warnings=tuple(_sanitize_text(settings, item) for item in result.warnings),
        error_message=_sanitize_text(settings, result.error_message),
    )


def _sanitize_scan_items(
    settings: WebSettings,
    items: Sequence[SecurityScanInfo],
) -> list[SecurityScanInfo]:
    return [_sanitize_scan_info(settings, item) for item in items]


def _sanitize_scan_info(
    settings: WebSettings,
    info: SecurityScanInfo,
) -> SecurityScanInfo:
    return info.model_copy(
        update={
            "findings": [
                _sanitize_scan_finding(settings, finding)
                for finding in info.findings
            ],
            "comparison": _sanitize_scan_comparison(settings, info.comparison),
            "warnings": [_sanitize_text(settings, item) for item in info.warnings],
            "error_message": _sanitize_text(settings, info.error_message),
        },
    )


def _sanitize_scan_comparison(
    settings: WebSettings,
    comparison: SecurityScanComparison,
) -> SecurityScanComparison:
    return comparison.model_copy(
        update={
            "fixed_findings": [
                _sanitize_scan_finding(settings, finding)
                for finding in comparison.fixed_findings
            ],
            "remaining_findings": [
                _sanitize_scan_finding(settings, finding)
                for finding in comparison.remaining_findings
            ],
            "introduced_findings": [
                _sanitize_scan_finding(settings, finding)
                for finding in comparison.introduced_findings
            ],
            "message": _sanitize_text(settings, comparison.message),
        }
    )


def _sanitize_scan_finding(
    settings: WebSettings,
    finding: SecurityScanFinding,
) -> SecurityScanFinding:
    return finding.model_copy(
        update={
            "target": _sanitize_text(settings, finding.target),
            "target_class": _sanitize_text(settings, finding.target_class),
            "target_type": _sanitize_text(settings, finding.target_type),
            "vulnerability_id": _sanitize_text(settings, finding.vulnerability_id),
            "package_name": _sanitize_text(settings, finding.package_name),
            "installed_version": _sanitize_text(settings, finding.installed_version),
            "fixed_version": _sanitize_text(settings, finding.fixed_version),
            "title": _sanitize_text(settings, finding.title),
            "primary_url": _http_url(_sanitize_text(settings, finding.primary_url)),
        },
    )


def _sanitize_text(settings: WebSettings, value: str) -> str:
    return _redact_unknown_absolute_paths(_redact_sensitive_text(settings, value))


def _counts_model(values: Mapping[str, int]) -> SecurityScanSeverityCounts:
    return SecurityScanSeverityCounts(
        critical=int(values.get("critical", 0)),
        high=int(values.get("high", 0)),
        medium=int(values.get("medium", 0)),
        low=int(values.get("low", 0)),
        unknown=int(values.get("unknown", 0)),
    )


def _job_response(job: WebSecurityScanJob) -> SecurityScanJobResponse:
    return SecurityScanJobResponse(
        job_id=job.id,
        status=job.status,  # type: ignore[arg-type]
        total_count=job.total_count,
        completed_count=job.completed_count,
        result=job.result,
        error=job.error,
    )


def _update_job(state: Any, job_id: str, **changes: object) -> None:
    lock: Lock = state.web_security_scan_lock
    jobs: dict[str, WebSecurityScanJob] = state.web_security_scan_jobs
    with lock:
        job = jobs.get(job_id)
        if job is None:
            return
        for key, value in changes.items():
            setattr(job, key, value)
        _prune_security_scan_jobs_unlocked(jobs)


def _active_security_scan_jobs_unlocked(jobs: Mapping[str, WebSecurityScanJob]) -> int:
    return sum(1 for job in jobs.values() if job.status in {"queued", "running"})


def _prune_security_scan_jobs_unlocked(
    jobs: dict[str, WebSecurityScanJob],
    *,
    keep: int = 20,
) -> None:
    if len(jobs) <= keep:
        return
    terminal_ids = [
        job_id
        for job_id, job in jobs.items()
        if job.status not in {"queued", "running"}
    ]
    for job_id in terminal_ids[: max(0, len(jobs) - keep)]:
        jobs.pop(job_id, None)


def _parse_positive_int(name: str, value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise ConfigError(f"{name} must be 1 or greater")
    return parsed
