from __future__ import annotations

import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest import mock

import pytest
from tests.db_helpers import db_connection
from tests.web_test_helpers import (
    WEB_DB_NAME,
    _client,
    _csrf_headers,
    _install_wud_api,
    _poll_until,
    _wud_api_container,
)

from wudup import web_job_registry, web_security
from wudup.db import init_db, open_db, utc_timestamp
from wudup.digest_verifier import (
    DigestResolveResult,
    DigestVerifier,
    ResolvedImageSubject,
)
from wudup.platforms import ImagePlatform, platform_value
from wudup.security_scanner import SecurityScanFinding, SecurityScanResult
from wudup.security_store import (
    cached_scan_by_request,
    cached_scan_by_request_or_unambiguous_platform,
    row_to_scan_info,
    upsert_scan_result,
)
from wudup.security_subjects import (
    PENDING_SECURITY_CACHE_OPTIONS,
    PENDING_SECURITY_DEFAULT_OPTIONS,
    PENDING_SECURITY_READ_OPTIONS,
    PendingSecurityContext,
    PendingSecurityOptions,
    PendingSecurityRequest,
)
from wudup.web_models import (
    SecurityScanFinding as SecurityScanFindingModel,
)
from wudup.web_models import (
    SecurityScanInfo,
    WebApplyJob,
)
from wudup.web_pending_sources import PendingSourceResult
from wudup.wud_file import parse_wud_text

VALID_DIGEST = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
DEFAULT_SECURITY_PLATFORM = ImagePlatform("linux", "amd64")


def test_security_scan_config_defaults_to_writable_trivy_cache() -> None:
    default_config = web_security.configured_security_scan_config({})
    empty_env_config = web_security.configured_security_scan_config(
        {"WUD_SECURITY_SCAN_CACHE_DIR": ""}
    )

    assert default_config.cache_dir == "/logs/trivy-cache"
    assert empty_env_config.cache_dir == "/logs/trivy-cache"


def test_security_scans_get_is_disabled_and_cache_only_by_default(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})
    wud_file = tmp_path / "state" / "images.todo"
    wud_file.write_text(
        f"repo/app:1.0 platform=linux/amd64 sha256={VALID_DIGEST}\n",
        encoding="utf-8",
    )

    response = client.get("/api/v1/security-scans")

    assert response.status_code == 200
    body = response.json()
    assert body["scanning_enabled"] is False
    assert body["scanner"] == "trivy"
    assert body["scan_mode"] == "registry"
    assert body["count"] == 1
    assert body["items"][0]["state"] == "disabled"
    assert not (tmp_path / "state" / WEB_DB_NAME).exists()


def test_security_scans_get_disabled_uses_cache_only_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[PendingSecurityOptions] = []

    def fake_context(
        _settings,
        *,
        options: PendingSecurityOptions = PENDING_SECURITY_DEFAULT_OPTIONS,
    ) -> PendingSecurityContext:
        calls.append(options)
        return _empty_security_context(tmp_path)

    monkeypatch.setattr("wudup.web_security.pending_security_context", fake_context)
    client = _client(tmp_path, {"WUD_WEB_DEV_NO_AUTH": "true"})

    response = client.get("/api/v1/security-scans")

    assert response.status_code == 200
    assert calls == [PENDING_SECURITY_CACHE_OPTIONS]


def test_security_scans_get_enabled_uses_read_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[PendingSecurityOptions] = []

    def fake_context(
        _settings,
        *,
        options: PendingSecurityOptions = PENDING_SECURITY_DEFAULT_OPTIONS,
    ) -> PendingSecurityContext:
        calls.append(options)
        return _empty_security_context(tmp_path)

    monkeypatch.setattr("wudup.web_security.pending_security_context", fake_context)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    response = client.get("/api/v1/security-scans")

    assert response.status_code == 200
    assert calls == [PENDING_SECURITY_READ_OPTIONS]


def test_security_scans_get_missing_cache_table_uses_placeholders(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "images.todo").write_text(
        f"repo/app:1.0 platform=linux/amd64 sha256={VALID_DIGEST}\n",
        encoding="utf-8",
    )
    with db_connection(state_dir / WEB_DB_NAME) as conn:
        init_db(conn)
        conn.execute("DROP TABLE security_scan_cache")
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    response = client.get("/api/v1/security-scans")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["state"] == "not_scanned"


def test_security_scan_refresh_enforces_csrf_disabled_and_read_only(
    tmp_path: Path,
) -> None:
    disabled = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
        },
    )
    missing_csrf = disabled.post("/api/v1/security-scans/refresh")
    disabled_response = disabled.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(disabled),
    )

    read_only_root = tmp_path / "read-only"
    read_only_root.mkdir()
    read_only = _client(
        read_only_root,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )
    read_only_response = read_only.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(read_only),
    )

    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"] == "origin header is required"
    assert disabled_response.status_code == 403
    assert disabled_response.json()["detail"] == "security scanning is disabled"
    assert read_only_response.status_code == 403
    assert read_only_response.json()["detail"] == "mutations are disabled"


def test_security_scan_refresh_rejects_concurrent_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "wudup.web_security.pending_security_context",
        lambda _settings: _empty_security_context(tmp_path),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )
    executor = QueuedExecutor()
    client.app.state.web_security_scan_executor = executor

    first = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )
    second = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )

    assert first.status_code == 200
    assert (
        web_job_registry._active_mutation_error_in_state(client.app.state)
        == "security scan refresh is already running"
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "security scan refresh is already running"
    assert len(executor.calls) == 1


def test_security_scan_refresh_cleans_up_job_when_executor_submit_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "wudup.web_security.pending_security_context",
        lambda _settings: _empty_security_context(tmp_path),
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    class FailingExecutor:
        def submit(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("queue failed")

        def shutdown(self, **_kwargs: object) -> None:
            pass

    client.app.state.web_security_scan_executor = FailingExecutor()

    with pytest.raises(RuntimeError, match="queue failed"):
        client.post(
            "/api/v1/security-scans/refresh",
            headers=_csrf_headers(client),
        )

    assert client.app.state.web_security_scan_jobs == {}


def test_security_scan_refresh_rejects_active_apply_job(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )
    client.app.state.web_apply_jobs["job-active"] = WebApplyJob(
        id="job-active",
        status="running",
        selected_line_numbers=(1,),
    )

    response = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "an apply job is already running"


def test_security_scan_refresh_rejects_active_self_update(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )
    client.app.state.web_self_update_running = True

    response = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "self-update is already running"


def test_security_scan_refresh_reserves_against_self_update_race(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )
    executor = QueuedExecutor()
    insert_started = Event()
    allow_insert = Event()
    client.app.state.web_security_scan_executor = executor
    client.app.state.web_security_scan_lock = NonBlockingLock()
    client.app.state.web_security_scan_jobs = BlockingSecurityScanJobs(
        insert_started,
        allow_insert,
    )
    request = SimpleNamespace(app=client.app)
    refresh_response: dict[str, object] = {}
    refresh_errors: list[BaseException] = []
    reservation: dict[str, str] = {}
    reservation_done = Event()
    reservation_blocked = False

    def refresh_security_scans() -> None:
        try:
            refresh_response["value"] = web_security.api_refresh_security_scans(request)
        except Exception as exc:  # noqa: BLE001 - asserted after join.  # pragma: no cover
            refresh_errors.append(exc)

    def reserve_self_update() -> None:
        try:
            reservation["error"] = web_job_registry._reserve_self_update(client.app.state)
        finally:
            reservation_done.set()

    refresh_thread = Thread(target=refresh_security_scans)
    reserve_thread: Thread | None = None
    refresh_thread.start()
    try:
        assert insert_started.wait(2.0)
        assert client.app.state.web_apply_lock.locked() is True
        reserve_thread = Thread(target=reserve_self_update)
        reserve_thread.start()
        reservation_blocked = not reservation_done.wait(0.1)
    finally:
        allow_insert.set()
        refresh_thread.join(2.0)
        if reserve_thread is not None:
            reserve_thread.join(2.0)

    assert reservation_blocked is True
    assert refresh_thread.is_alive() is False
    assert reserve_thread is not None
    assert reserve_thread.is_alive() is False
    assert refresh_errors == []
    assert refresh_response["value"].status == "queued"
    assert reservation["error"] == "security scan refresh is already running"
    assert client.app.state.web_self_update_running is False
    assert len(executor.calls) == 1


def test_active_mutation_error_checks_security_jobs_under_scan_lock() -> None:
    security_scan_lock = TrackingLock()
    state = SimpleNamespace(
        web_apply_lock=Lock(),
        web_apply_jobs={},
        web_self_update_running=False,
        web_security_scan_lock=security_scan_lock,
        web_security_scan_jobs=GuardedSecurityScanJobs(
            security_scan_lock,
            {"scan": SimpleNamespace(status="running")},
        ),
    )

    assert (
        web_job_registry._active_mutation_error_in_state(state)
        == "security scan refresh is already running"
    )


def test_active_mutation_error_keeps_general_guards_when_scan_jobs_excluded() -> None:
    state = SimpleNamespace(
        web_apply_lock=Lock(),
        web_apply_jobs={"apply": SimpleNamespace(status="queued")},
        web_self_update_running=False,
        web_security_scan_lock=Lock(),
        web_security_scan_jobs={"scan": SimpleNamespace(status="running")},
    )

    assert (
        web_job_registry._active_mutation_error_in_state(
            state,
            include_security_scan_jobs=False,
        )
        == "an apply job is already running"
    )

    state.web_apply_jobs = {}
    state.web_self_update_running = True
    assert (
        web_job_registry._active_mutation_error_in_state(
            state,
            include_security_scan_jobs=False,
        )
        == "self-update is already running"
    )

    state.web_self_update_running = False
    assert (
        web_job_registry._active_mutation_error_in_state(
            state,
            include_security_scan_jobs=False,
        )
        == ""
    )


def test_security_scan_refresh_queues_server_resolved_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _empty_security_context(tmp_path)
    monkeypatch.setattr(
        "wudup.web_security.pending_security_context",
        lambda _settings: context,
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    queued = client.post(
        "/api/v1/security-scans/refresh",
        json={"image": "ghcr.io/attacker/injected:latest"},
        headers=_csrf_headers(client),
    )

    assert queued.status_code == 200
    job_id = queued.json()["job_id"]
    result = _poll_until(
        lambda: _completed_security_job(client, job_id),
        timeout_message="security scan job did not complete",
    )
    assert result["status"] == "success"
    assert result["total_count"] == 0
    assert result["completed_count"] == 0
    assert result["result"]["count"] == 0
    assert result["result"]["source_file"] == str(tmp_path / "state" / "images.todo")
    assert (tmp_path / "state" / WEB_DB_NAME).exists()


def test_security_scan_refresh_scans_caches_and_reads_back_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _single_security_context(tmp_path)
    monkeypatch.setattr(
        "wudup.web_security.pending_security_context",
        lambda _settings, **_kwargs: context,
    )
    monkeypatch.setattr(
        "wudup.web_security.default_digest_verifier",
        lambda _settings: FakeVerifier(),
    )
    monkeypatch.setattr("wudup.web_security.TrivyScanner", FakeScanner)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
            "WUD_WEB_TOKEN": "supersecret",
        },
    )

    queued = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )

    assert queued.status_code == 200
    job_id = queued.json()["job_id"]
    result = _poll_until(
        lambda: _completed_security_job(client, job_id),
        timeout_message="security scan job did not complete",
    )
    item = result["result"]["items"][0]
    assert item["state"] == "complete"
    assert item["verdict"] == "findings"
    assert item["severity_counts"]["high"] == 1
    assert item["advisory_counts_known"] is True
    assert item["findings"][0]["vulnerability_id"] == "CVE-2026-0001"
    assert item["findings"][0]["package_name"] == "openssl"
    assert item["findings"][0]["primary_url"] == FakeScanner.primary_url
    assert item["warnings"] == ["subject warning"]

    cached = client.get("/api/v1/security-scans")

    assert cached.status_code == 200
    cached_item = cached.json()["items"][0]
    assert cached_item["state"] == "complete"
    assert cached_item["verdict"] == "findings"
    assert cached_item["severity_counts"]["high"] == 1
    assert cached_item["advisory_counts"]["high"] == 1
    assert cached_item["advisory_counts_known"] is True
    assert cached_item["subject"]["index_digest"] == f"sha256:{VALID_DIGEST}"
    assert cached_item["subject"]["immutable_ref"] == (
        "ghcr.io/acme/app@sha256:child"
    )
    assert cached_item["findings"][0]["target"] == "debian:12"
    assert cached_item["findings"][0]["target_class"] == "os-pkgs"
    assert cached_item["findings"][0]["target_type"] == "debian"
    assert cached_item["findings"][0]["vulnerability_id"] == "CVE-2026-0001"
    assert cached_item["findings"][0]["package_name"] == "openssl"
    assert cached_item["findings"][0]["primary_url"] == FakeScanner.primary_url
    assert cached_item["warnings"] == ["subject warning"]


def test_security_scan_refresh_compares_installed_and_candidate_findings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current_digest = f"sha256:{'c' * 64}"
    context = _single_security_context(tmp_path)
    refresh_context = PendingSecurityContext(
        source=context.source,
        requests=(
            _single_security_request(
                current_image="ghcr.io/acme/app:1.0",
                current_digest=current_digest,
            ),
        ),
    )
    read_context = PendingSecurityContext(
        source=context.source,
        requests=(
            _single_security_request(
                platform=None,
                current_image="ghcr.io/acme/app:1.0",
                current_digest=current_digest,
            ),
        ),
    )

    def security_context(
        _settings,
        *,
        options: PendingSecurityOptions = PENDING_SECURITY_DEFAULT_OPTIONS,
    ) -> PendingSecurityContext:
        return refresh_context if options.include_compose else read_context

    monkeypatch.setattr(
        "wudup.web_security.pending_security_context",
        security_context,
    )
    monkeypatch.setattr(
        "wudup.web_security.default_digest_verifier",
        lambda _settings: ComparisonVerifier(current_digest),
    )
    monkeypatch.setattr("wudup.web_security.TrivyScanner", ComparisonScanner)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    queued = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )

    assert queued.status_code == 200
    result = _poll_until(
        lambda: _completed_security_job(client, queued.json()["job_id"]),
        timeout_message="security scan job did not complete",
    )
    item = result["result"]["items"][0]
    comparison = item["comparison"]
    assert comparison["status"] == "mixed"
    assert len(comparison["fixed_findings"]) == 1
    assert len(comparison["remaining_findings"]) == 1
    assert len(comparison["introduced_findings"]) == 1
    assert comparison["current_subject"]["manifest_digest"] == "sha256:old-child"
    assert comparison["fixed_findings"][0]["vulnerability_id"] == "CVE-2026-0001"
    assert comparison["remaining_findings"][0]["vulnerability_id"] == "CVE-2026-0002"
    assert comparison["introduced_findings"][0]["vulnerability_id"] == "CVE-2026-0003"
    assert item["subject"]["manifest_digest"] == "sha256:new-child"

    cached = client.get("/api/v1/security-scans")

    assert cached.status_code == 200
    cached_comparison = cached.json()["items"][0]["comparison"]
    assert cached_comparison["status"] == "mixed"
    assert len(cached_comparison["fixed_findings"]) == 1
    assert len(cached_comparison["remaining_findings"]) == 1
    assert len(cached_comparison["introduced_findings"]) == 1


def test_security_scan_comparison_preserves_duplicate_target_occurrences() -> None:
    installed = _comparison_info(
        [_scan_finding("CVE-2026-0001", "openssl", "1.0", target="debian:12")]
    )
    candidate = _comparison_info(
        [
            _scan_finding("CVE-2026-0001", "openssl", "1.0", target="debian:12"),
            _scan_finding(
                "CVE-2026-0001",
                "openssl",
                "1.0",
                target="usr/local/lib/python3.14/site-packages",
                target_class="lang-pkgs",
                target_type="python-pkg",
            ),
        ]
    )

    comparison = web_security._comparison(installed, candidate)

    assert comparison.status == "worse"
    assert len(comparison.remaining_findings) == 1
    assert len(comparison.introduced_findings) == 1
    assert comparison.introduced_findings[0].target_type == "python-pkg"


def test_security_scan_comparison_requires_scanner_and_database_provenance() -> None:
    finding = _scan_finding("CVE-2026-0001", "openssl", "1.0")

    missing_scanner = web_security._comparison(
        _comparison_info([finding], scanner_version=""),
        _comparison_info([finding]),
    )
    missing_schema = web_security._comparison(
        _comparison_info([finding], scanner_schema=""),
        _comparison_info([finding], scanner_schema=""),
    )
    missing_database = web_security._comparison(
        _comparison_info([finding], db_revision=""),
        _comparison_info([finding]),
    )
    missing_database_timestamp = web_security._comparison(
        _comparison_info([finding], db_updated_at=""),
        _comparison_info([finding]),
    )

    assert missing_scanner.status == "unknown"
    assert missing_scanner.message == "Scanner version provenance is unavailable."
    assert missing_schema.status == "unknown"
    assert missing_schema.message == "Scanner schema provenance is unavailable."
    assert missing_database.status == "unknown"
    assert missing_database.message == "Vulnerability database provenance is unavailable."
    assert missing_database_timestamp.status == "unknown"
    assert (
        missing_database_timestamp.message
        == "Vulnerability database provenance is unavailable."
    )


def test_security_scan_comparison_distinguishes_database_refreshes() -> None:
    finding = _scan_finding("CVE-2026-0001", "openssl", "1.0")

    comparison = web_security._comparison(
        _comparison_info([finding], db_revision="2", db_updated_at="2026-06-26T00:00:00Z"),
        _comparison_info([finding], db_revision="2", db_updated_at="2026-06-27T00:00:00Z"),
    )

    assert comparison.status == "unknown"
    assert comparison.message == (
        "Installed and candidate scans used different vulnerability databases."
    )


def test_security_scan_refresh_reuses_current_scan_within_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current_digest = f"sha256:{'c' * 64}"
    context = _single_security_context(tmp_path)
    first = _single_security_request(
        current_image="ghcr.io/acme/app:1.0",
        current_digest=current_digest,
    )
    second = replace(
        first,
        line_no=2,
        raw=f"{first.raw} duplicate",
    )
    refresh_context = PendingSecurityContext(
        source=context.source,
        requests=(first, second),
    )

    class CountingVerifier(ComparisonVerifier):
        def __init__(self, digest: str) -> None:
            super().__init__(digest)
            self.current_resolves = 0

        def resolve_digest_subject(
            self,
            image: str,
            reported_digest: str,
            platform: ImagePlatform | None,
            *,
            platform_source: str = "",
        ) -> ResolvedImageSubject:
            self.current_resolves += 1
            return super().resolve_digest_subject(
                image,
                reported_digest,
                platform,
                platform_source=platform_source,
            )

    class CountingScanner(ComparisonScanner):
        current_scans = 0

        def scan(self, subject: ResolvedImageSubject) -> SecurityScanResult:
            if subject.manifest_digest == "sha256:old-child":
                type(self).current_scans += 1
            return super().scan(subject)

    verifier = CountingVerifier(current_digest)
    monkeypatch.setattr(
        "wudup.web_security.pending_security_context",
        lambda _settings, **_kwargs: refresh_context,
    )
    monkeypatch.setattr(
        "wudup.web_security.default_digest_verifier",
        lambda _settings: verifier,
    )
    monkeypatch.setattr("wudup.web_security.TrivyScanner", CountingScanner)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    queued = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )

    assert queued.status_code == 200
    result = _poll_until(
        lambda: _completed_security_job(client, queued.json()["job_id"]),
        timeout_message="security scan job did not complete",
    )
    assert result["status"] == "success"
    assert verifier.current_resolves == 1
    assert CountingScanner.current_scans == 1
    assert [item["comparison"]["status"] for item in result["result"]["items"]] == [
        "mixed",
        "mixed",
    ]

    cached = client.get("/api/v1/security-scans")

    assert cached.status_code == 200
    assert [item["comparison"]["status"] for item in cached.json()["items"]] == [
        "mixed",
        "mixed",
    ]


def test_security_scan_refresh_preserves_seeded_demo_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _single_security_context(tmp_path)
    request = context.requests[0]
    demo_result = SecurityScanResult(
        state="complete",
        verdict="findings",
        scanner_version="demo",
        scanner_schema="trivy-json",
        severity_counts={"high": 1},
        findings=(
            SecurityScanFinding(
                vulnerability_id="CVE-2026-0001",
                package_name="demo-package",
                installed_version="1.0.0",
                fixed_version="1.0.1",
                severity="high",
                title="demo vulnerability",
                primary_url="https://avd.aquasec.com/nvd/cve-2026-0001",
            ),
        ),
    )
    with open_db(tmp_path / "state" / WEB_DB_NAME) as conn:
        init_db(conn)
        upsert_scan_result(
            conn,
            request,
            _exact_subject(),
            demo_result,
            timestamp=utc_timestamp(),
        )

    class FailingVerifier:
        def resolve_subject(self, *_args, **_kwargs) -> ResolvedImageSubject:
            raise AssertionError("demo cached scan should not resolve a live digest")

    monkeypatch.setattr(
        "wudup.web_security.pending_security_context",
        lambda _settings, **_kwargs: context,
    )
    monkeypatch.setattr(
        "wudup.web_security.default_digest_verifier",
        lambda _settings: FailingVerifier(),
    )
    monkeypatch.setattr("wudup.web_security.TrivyScanner", FailingScanner)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    queued = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )

    assert queued.status_code == 200
    result = _poll_until(
        lambda: _completed_security_job(client, queued.json()["job_id"]),
        timeout_message="security scan job did not complete",
    )
    item = result["result"]["items"][0]
    assert result["status"] == "success"
    assert item["state"] == "complete"
    assert item["scanner_version"] == "demo"
    assert item["findings"][0]["vulnerability_id"] == "CVE-2026-0001"


def test_security_scan_cache_readback_drops_unsafe_finding_primary_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _single_security_context(tmp_path)
    monkeypatch.setattr(
        "wudup.web_security.pending_security_context",
        lambda _settings, **_kwargs: context,
    )
    monkeypatch.setattr(
        "wudup.web_security.default_digest_verifier",
        lambda _settings: FakeVerifier(),
    )
    monkeypatch.setattr(FakeScanner, "primary_url", "javascript:alert(1)")
    monkeypatch.setattr("wudup.web_security.TrivyScanner", FakeScanner)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    queued = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )

    assert queued.status_code == 200
    job_id = queued.json()["job_id"]
    result = _poll_until(
        lambda: _completed_security_job(client, job_id),
        timeout_message="security scan job did not complete",
    )
    item = result["result"]["items"][0]
    assert item["findings"][0]["primary_url"] == ""

    cached = client.get("/api/v1/security-scans")

    assert cached.status_code == 200
    cached_item = cached.json()["items"][0]
    assert cached_item["findings"][0]["primary_url"] == ""


def test_security_scan_cache_readback_uses_unambiguous_cached_platform(
    tmp_path: Path,
    monkeypatch,
) -> None:
    refresh_context = _single_security_context(
        tmp_path,
        platform=ImagePlatform("linux", "amd64"),
    )
    get_context = _single_security_context(tmp_path, platform=None)

    def fake_context(
        _settings,
        *,
        options: PendingSecurityOptions = PENDING_SECURITY_DEFAULT_OPTIONS,
    ) -> PendingSecurityContext:
        if not options.include_compose:
            return get_context
        return refresh_context

    monkeypatch.setattr("wudup.web_security.pending_security_context", fake_context)
    monkeypatch.setattr(
        "wudup.web_security.default_digest_verifier",
        lambda _settings: FakeVerifier(),
    )
    monkeypatch.setattr("wudup.web_security.TrivyScanner", FakeScanner)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    item = _refresh_and_read_cached_security_scan_item(client)
    assert item["state"] == "complete"
    assert item["verdict"] == "findings"
    assert item["severity_counts"]["high"] == 1
    assert item["findings"][0]["vulnerability_id"] == "CVE-2026-0001"


def test_security_scan_cache_readback_does_not_cross_platform_request_keys(
    tmp_path: Path,
    monkeypatch,
) -> None:
    refresh_context = _single_security_context(
        tmp_path,
        platform=ImagePlatform("linux", "amd64"),
    )
    get_context = _single_security_context(
        tmp_path,
        platform=ImagePlatform("linux", "arm64"),
    )

    def fake_context(
        _settings,
        *,
        options: PendingSecurityOptions = PENDING_SECURITY_DEFAULT_OPTIONS,
    ) -> PendingSecurityContext:
        if not options.include_compose:
            return get_context
        return refresh_context

    monkeypatch.setattr("wudup.web_security.pending_security_context", fake_context)
    monkeypatch.setattr(
        "wudup.web_security.default_digest_verifier",
        lambda _settings: FakeVerifier(),
    )
    monkeypatch.setattr("wudup.web_security.TrivyScanner", FakeScanner)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    item = _refresh_and_read_cached_security_scan_item(client)
    assert item["state"] == "not_scanned"


def test_security_scan_get_resolves_wud_api_tag_digest_for_cache_readback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    digest = f"sha256:{VALID_DIGEST}"
    raw = "ghcr.io/acme/app:1.0 tag=2.0 platform=linux/amd64"

    verifier = mock.create_autospec(DigestVerifier, instance=True, spec_set=True)
    verifier.resolve_tag_digest.return_value = DigestResolveResult(
        ok=True,
        status="resolved",
        reason="tag-digest-resolved",
        digest=digest,
    )
    monkeypatch.setattr(
        "wudup.security_subjects.default_digest_verifier",
        lambda _settings: verifier,
    )
    _install_wud_api(
        monkeypatch,
        containers=[
            _wud_api_container(
                image="acme/app",
                tag="1.0",
                remote_tag="2.0",
                platform="linux/amd64",
                registry_url="https://ghcr.io",
            )
        ],
    )
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_PENDING_SOURCE": "api",
            "WUD_API_BASE_URL": "https://wud.security-scan-api.test:3000",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )
    request = PendingSecurityRequest(
        line_no=1,
        raw=raw,
        image="ghcr.io/acme/app:1.0",
        candidate_image="ghcr.io/acme/app:2.0",
        reported_digest=digest,
        platform=ImagePlatform("linux", "amd64"),
        platform_source="wud",
        identity_status="pending",
    )
    subject = ResolvedImageSubject(
        canonical_registry="ghcr.io",
        canonical_repository="acme/app",
        requested_ref="ghcr.io/acme/app:2.0",
        reported_digest=digest,
        index_digest=digest,
        manifest_digest="sha256:child",
        os="linux",
        architecture="amd64",
        platform_source="wud",
        identity_status="exact",
    )
    with open_db(tmp_path / "state" / WEB_DB_NAME) as conn:
        init_db(conn)
        upsert_scan_result(
            conn,
            request,
            subject,
            SecurityScanResult(state="complete", verdict="none_reported"),
            timestamp=utc_timestamp(),
        )

    response = client.get("/api/v1/security-scans")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["state"] == "complete"
    assert item["verdict"] == "none_reported"
    verifier.resolve_tag_digest.assert_called_once_with("ghcr.io/acme/app:2.0")


def test_security_scan_refresh_persists_non_exact_resolution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _single_security_context(tmp_path)
    monkeypatch.setattr(
        "wudup.web_security.pending_security_context",
        lambda _settings, **_kwargs: context,
    )
    monkeypatch.setattr(
        "wudup.web_security.default_digest_verifier",
        lambda _settings: StaleVerifier(),
    )
    monkeypatch.setattr("wudup.web_security.TrivyScanner", FailingScanner)
    client = _client(
        tmp_path,
        {
            "WUD_WEB_DEV_NO_AUTH": "true",
            "WUD_WEB_MUTATIONS_ENABLED": "true",
            "WUD_SECURITY_SCANNING_ENABLED": "true",
        },
    )

    queued = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )
    assert queued.status_code == 200
    result = _poll_until(
        lambda: _completed_security_job(client, queued.json()["job_id"]),
        timeout_message="security scan job did not complete",
    )
    item = result["result"]["items"][0]
    assert item["state"] == "stale"
    assert item["error_code"] == "stale"

    cached = client.get("/api/v1/security-scans")

    assert cached.status_code == 200
    cached_item = cached.json()["items"][0]
    assert cached_item["state"] == "stale"
    assert cached_item["error_code"] == "stale"
    assert cached_item["error_message"] == "reported digest is not current"


def test_security_scan_cache_corrupt_counts_degrade_to_zero() -> None:
    request = _single_security_request()
    subject = _exact_subject()
    result = SecurityScanResult(
        state="complete",
        verdict="findings",
        severity_counts={"high": 1},
        fixable_counts={"high": 1},
        warnings=("scan warning",),
    )
    with db_connection(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        upsert_scan_result(
            conn,
            request,
            subject,
            result,
            timestamp=utc_timestamp(),
        )
        conn.execute(
            """
            UPDATE security_scan_cache
            SET severity_counts_json = ?,
                fixable_counts_json = ?,
                unfixed_count = ?
            """,
            ('{"high":"not-a-number"}', "[]", "not-a-number"),
        )
        row = conn.execute("SELECT * FROM security_scan_cache").fetchone()

    info = row_to_scan_info(row, request)

    assert info.severity_counts.high == 0
    assert info.fixable_counts.high == 0
    assert info.unfixed_count == 0
    assert info.warnings == ["subject warning", "scan warning"]


def test_security_scan_cache_round_trips_vulnerability_findings() -> None:
    request = _single_security_request()
    subject = _exact_subject()
    result = SecurityScanResult(
        state="complete",
        verdict="findings",
        severity_counts={"critical": 1},
        findings=(
            SecurityScanFinding(
                vulnerability_id="CVE-2026-0001",
                package_name="openssl",
                installed_version="1.0.0",
                fixed_version="1.0.1",
                severity="critical",
                title="demo vulnerability",
                primary_url="https://avd.aquasec.com/nvd/cve-2026-0001",
            ),
        ),
    )
    with db_connection(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        upsert_scan_result(
            conn,
            request,
            subject,
            result,
            timestamp=utc_timestamp(),
        )
        cached = cached_scan_by_request(conn, request)

    assert cached is not None
    assert len(cached.findings) == 1
    assert cached.findings[0].vulnerability_id == "CVE-2026-0001"
    assert cached.findings[0].package_name == "openssl"
    assert cached.findings[0].installed_version == "1.0.0"
    assert cached.findings[0].fixed_version == "1.0.1"
    assert cached.findings[0].severity == "critical"
    assert cached.findings[0].title == "demo vulnerability"
    assert cached.findings[0].primary_url == "https://avd.aquasec.com/nvd/cve-2026-0001"
    assert cached.advisory_counts_known is True


def test_security_scan_cache_uses_maximum_advisory_severity() -> None:
    request = _single_security_request()
    result = SecurityScanResult(
        state="complete",
        verdict="findings",
        severity_counts={"critical": 1, "low": 1},
        findings=(
            SecurityScanFinding(vulnerability_id="CVE-2026-0001", severity="low"),
            SecurityScanFinding(
                vulnerability_id="CVE-2026-0001", severity="critical"
            ),
        ),
    )
    with db_connection(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        upsert_scan_result(
            conn, request, _exact_subject(), result, timestamp=utc_timestamp()
        )
        cached = cached_scan_by_request(conn, request)

    assert cached is not None
    assert cached.advisory_counts.low == 0
    assert cached.advisory_counts.critical == 1


def test_security_scan_cache_marks_summary_only_advisory_counts_unknown() -> None:
    request = _single_security_request()
    result = SecurityScanResult(
        state="complete",
        verdict="findings",
        severity_counts={"high": 2},
    )
    with db_connection(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        upsert_scan_result(
            conn, request, _exact_subject(), result, timestamp=utc_timestamp()
        )
        cached = cached_scan_by_request(conn, request)

    assert cached is not None
    assert cached.advisory_counts.high == 0
    assert cached.advisory_counts_known is False


def test_security_scan_cache_identity_includes_database_updated_at() -> None:
    request = _single_security_request()
    subject = _exact_subject()
    with db_connection(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        for updated_at in ("2026-06-26T00:00:00Z", "2026-06-27T00:00:00Z"):
            upsert_scan_result(
                conn,
                request,
                subject,
                SecurityScanResult(
                    state="complete",
                    scanner_version="0.71.2",
                    scanner_schema="2",
                    db_revision="2",
                    db_updated_at=updated_at,
                ),
                timestamp="2026-06-27T01:00:00Z",
            )
        rows = conn.execute("SELECT cache_key FROM security_scan_cache").fetchall()

    assert len(rows) == 2


def test_security_scan_cache_platform_fallback_rejects_ambiguous_platforms() -> None:
    result = SecurityScanResult(state="complete", verdict="clean")
    amd64_request = _single_security_request(platform=ImagePlatform("linux", "amd64"))
    arm64_request = _single_security_request(platform=ImagePlatform("linux", "arm64"))
    fallback_request = _single_security_request(platform=None)
    arm64_subject = ResolvedImageSubject(
        canonical_registry="ghcr.io",
        canonical_repository="acme/app",
        requested_ref="ghcr.io/acme/app:1.0",
        reported_digest=f"sha256:{VALID_DIGEST}",
        index_digest=f"sha256:{VALID_DIGEST}",
        manifest_digest="sha256:arm-child",
        os="linux",
        architecture="arm64",
        platform_source="wud",
        identity_status="exact",
    )

    with db_connection(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        upsert_scan_result(
            conn,
            amd64_request,
            _exact_subject(),
            result,
            timestamp="2026-06-26T00:00:00+00:00",
        )
        upsert_scan_result(
            conn,
            arm64_request,
            arm64_subject,
            result,
            timestamp="2026-06-26T00:00:01+00:00",
        )

        cached = cached_scan_by_request_or_unambiguous_platform(
            conn,
            fallback_request,
        )

    assert cached is None


def test_security_scan_cache_uses_newest_same_second_row_and_prunes() -> None:
    request = _single_security_request()
    subject = _exact_subject()
    timestamp = "2026-06-26T00:00:00+00:00"
    with db_connection(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
        for index in range(7):
            upsert_scan_result(
                conn,
                request,
                subject,
                SecurityScanResult(
                    state="complete",
                    verdict="findings" if index == 6 else "unknown",
                    scanner_schema=str(index),
                    db_revision=str(index),
                    severity_counts={"high": index},
                ),
                timestamp=timestamp,
            )
        cached = cached_scan_by_request(conn, request)
        rows = conn.execute(
            """
            SELECT rowid
            FROM security_scan_cache
            WHERE request_key = ?
            """,
            (request.request_key,),
        ).fetchall()

    assert cached is not None
    assert cached.db_revision == "6"
    assert cached.severity_counts.high == 6
    assert len(rows) == 5


def _completed_security_job(client, job_id: str) -> dict[str, object] | None:
    response = client.get(f"/api/v1/security-scans/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    return body if body["status"] in {"success", "failure"} else None


def _refresh_and_read_cached_security_scan_item(client) -> dict[str, object]:
    queued = client.post(
        "/api/v1/security-scans/refresh",
        headers=_csrf_headers(client),
    )
    assert queued.status_code == 200
    result = _poll_until(
        lambda: _completed_security_job(client, queued.json()["job_id"]),
        timeout_message="security scan job did not complete",
    )
    assert result["status"] == "success"

    cached = client.get("/api/v1/security-scans")

    assert cached.status_code == 200
    return cached.json()["items"][0]


def _empty_security_context(tmp_path: Path) -> PendingSecurityContext:
    source_file = tmp_path / "state" / "images.todo"
    return PendingSecurityContext(
        source=PendingSourceResult(
            configured="file",
            active="file",
            label="Pending file",
            source_file=str(source_file),
            exists=True,
            parsed=parse_wud_text(""),
            text="",
            source_hash="empty",
        ),
        requests=(),
    )


def _single_security_context(
    tmp_path: Path,
    *,
    raw: str | None = None,
    platform: ImagePlatform | None = DEFAULT_SECURITY_PLATFORM,
) -> PendingSecurityContext:
    source_file = tmp_path / "state" / "images.todo"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    line = raw if raw is not None else _single_security_raw(platform)
    text = f"{line}\n"
    source_file.write_text(text, encoding="utf-8")
    return PendingSecurityContext(
        source=PendingSourceResult(
            configured="file",
            active="file",
            label="Pending file",
            source_file=str(source_file),
            exists=True,
            parsed=parse_wud_text(text),
            text=text,
            source_hash="single",
        ),
        requests=(_single_security_request(raw=line, platform=platform),),
    )


def _single_security_request(
    *,
    raw: str | None = None,
    platform: ImagePlatform | None = DEFAULT_SECURITY_PLATFORM,
    current_image: str = "",
    current_digest: str = "",
) -> PendingSecurityRequest:
    line = raw if raw is not None else _single_security_raw(platform)
    identity_status = "pending" if platform is not None else "unsupported"
    error = "" if platform is not None else "platform is required"
    return PendingSecurityRequest(
        line_no=1,
        raw=line,
        image="ghcr.io/acme/app:1.0",
        candidate_image="ghcr.io/acme/app:1.0",
        reported_digest=f"sha256:{VALID_DIGEST}",
        platform=platform,
        platform_source="wud" if platform is not None else "",
        current_image=current_image,
        current_digest=current_digest,
        current_digest_source="wud" if current_digest else "",
        identity_status=identity_status,
        error=error,
    )


def _single_security_raw(platform: ImagePlatform | None) -> str:
    platform_text = platform_value(platform)
    platform_suffix = f" platform={platform_text}" if platform_text else ""
    return f"ghcr.io/acme/app:1.0{platform_suffix} sha256={VALID_DIGEST}"


def _exact_subject() -> ResolvedImageSubject:
    return ResolvedImageSubject(
        canonical_registry="ghcr.io",
        canonical_repository="acme/app",
        requested_ref="ghcr.io/acme/app:1.0",
        reported_digest=f"sha256:{VALID_DIGEST}",
        index_digest=f"sha256:{VALID_DIGEST}",
        manifest_digest="sha256:child",
        os="linux",
        architecture="amd64",
        platform_source="wud",
        identity_status="exact",
        warnings=("subject warning",),
    )


class FakeVerifier:
    def resolve_subject(
        self,
        image: str,
        reported_digest: str,
        platform: ImagePlatform | None,
        *,
        platform_source: str = "",
    ) -> ResolvedImageSubject:
        assert image == "ghcr.io/acme/app:1.0"
        assert reported_digest == f"sha256:{VALID_DIGEST}"
        assert platform == ImagePlatform("linux", "amd64")
        return _exact_subject()


class StaleVerifier:
    def resolve_subject(
        self,
        _image: str,
        _reported_digest: str,
        platform: ImagePlatform | None,
        *,
        platform_source: str = "",
    ) -> ResolvedImageSubject:
        assert platform == ImagePlatform("linux", "amd64")
        return ResolvedImageSubject(
            canonical_registry="ghcr.io",
            canonical_repository="acme/app",
            requested_ref="ghcr.io/acme/app:1.0",
            reported_digest=f"sha256:{VALID_DIGEST}",
            index_digest="sha256:current",
            os="linux",
            architecture="amd64",
            platform_source=platform_source,
            identity_status="stale",
            error="reported digest is not current",
        )


class FakeScanner:
    primary_url = "https://avd.aquasec.com/nvd/cve-2026-0001"

    def __init__(self, *_args, **_kwargs) -> None:
        pass  # Test double accepts the production scanner constructor signature.

    def scan(self, subject: ResolvedImageSubject) -> SecurityScanResult:
        assert subject.identity_status == "exact"
        return SecurityScanResult(
            state="complete",
            verdict="findings",
            scanner_version="fake-trivy",
            scanner_schema="2",
            db_revision="2",
            db_updated_at="2026-06-26T00:00:00Z",
            severity_counts={"high": 1},
            advisory_counts={"high": 1},
            fixable_counts={"high": 1},
            findings=(
                SecurityScanFinding(
                    target="debian:12",
                    target_class="os-pkgs",
                    target_type="debian",
                    vulnerability_id="CVE-2026-0001",
                    package_name="openssl",
                    installed_version="1.0.0",
                    fixed_version="1.0.1",
                    severity="high",
                    title="demo vulnerability",
                    primary_url=self.primary_url,
                ),
            ),
        )


class ComparisonVerifier:
    def __init__(self, current_digest: str) -> None:
        self.current_digest = current_digest

    def resolve_subject(
        self,
        image: str,
        reported_digest: str,
        platform: ImagePlatform | None,
        *,
        platform_source: str = "",
    ) -> ResolvedImageSubject:
        assert image == "ghcr.io/acme/app:1.0"
        assert reported_digest == f"sha256:{VALID_DIGEST}"
        assert platform == ImagePlatform("linux", "amd64")
        return ResolvedImageSubject(
            canonical_registry="ghcr.io",
            canonical_repository="acme/app",
            requested_ref="ghcr.io/acme/app:1.0",
            reported_digest=f"sha256:{VALID_DIGEST}",
            index_digest=f"sha256:{VALID_DIGEST}",
            manifest_digest="sha256:new-child",
            os="linux",
            architecture="amd64",
            platform_source=platform_source,
            identity_status="exact",
        )

    def resolve_digest_subject(
        self,
        image: str,
        reported_digest: str,
        platform: ImagePlatform | None,
        *,
        platform_source: str = "",
    ) -> ResolvedImageSubject:
        assert image == "ghcr.io/acme/app:1.0"
        assert reported_digest == self.current_digest
        assert platform == ImagePlatform("linux", "amd64")
        return ResolvedImageSubject(
            canonical_registry="ghcr.io",
            canonical_repository="acme/app",
            requested_ref="ghcr.io/acme/app:1.0",
            reported_digest=self.current_digest,
            index_digest=self.current_digest,
            manifest_digest="sha256:old-child",
            os="linux",
            architecture="amd64",
            platform_source=platform_source,
            identity_status="exact",
        )


class ComparisonScanner:
    def __init__(self, *_args, **_kwargs) -> None:
        # Matches TrivyScanner construction; the fake scanner has no setup.
        pass

    def scan(self, subject: ResolvedImageSubject) -> SecurityScanResult:
        findings = (
            _scan_finding("CVE-2026-0001", "openssl", "1.0.0"),
            _scan_finding("CVE-2026-0002", "zlib", "1.0.0"),
        )
        if subject.manifest_digest == "sha256:new-child":
            findings = (
                _scan_finding("CVE-2026-0002", "zlib", "2.0.0"),
                _scan_finding("CVE-2026-0003", "curl", "1.0.0"),
            )
        return SecurityScanResult(
            state="complete",
            verdict="findings",
            scanner_version="fake-trivy",
            scanner_schema="2",
            db_revision="2",
            db_updated_at="2026-06-26T00:00:00Z",
            severity_counts={"high": len(findings)},
            advisory_counts={"high": len(findings)},
            fixable_counts={"high": len(findings)},
            findings=findings,
        )


def _scan_finding(
    vulnerability_id: str,
    package_name: str,
    installed_version: str,
    *,
    target: str = "debian:12",
    target_class: str = "os-pkgs",
    target_type: str = "debian",
) -> SecurityScanFinding:
    return SecurityScanFinding(
        target=target,
        target_class=target_class,
        target_type=target_type,
        vulnerability_id=vulnerability_id,
        package_name=package_name,
        installed_version=installed_version,
        fixed_version="9.9.9",
        severity="high",
        title=f"{package_name} vulnerability",
    )


def _comparison_info(
    findings: list[SecurityScanFinding],
    *,
    scanner_version: str = "fake-trivy",
    scanner_schema: str = "2",
    db_revision: str = "test-db",
    db_updated_at: str = "2026-06-26T00:00:00Z",
) -> SecurityScanInfo:
    return SecurityScanInfo(
        line_no=1,
        state="complete",
        verdict="findings",
        scanner="trivy",
        scanner_version=scanner_version,
        scanner_schema=scanner_schema,
        db_revision=db_revision,
        db_updated_at=db_updated_at,
        findings=[SecurityScanFindingModel(**asdict(finding)) for finding in findings],
    )


class FailingScanner:
    def __init__(self, *_args, **_kwargs) -> None:
        pass  # Test double accepts the production scanner constructor signature.

    def scan(self, _subject: ResolvedImageSubject) -> SecurityScanResult:
        raise AssertionError("non-exact subject should not be scanned")


class QueuedExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def submit(self, *args) -> None:
        self.calls.append(args)

    def shutdown(self, **_kwargs) -> None:
        pass  # Queued test executor has no background workers to stop.


class NonBlockingLock:
    def __enter__(self) -> NonBlockingLock:
        return self

    def __exit__(self, *_args: object) -> None:
        # The test double intentionally leaves the context without blocking.
        pass


class BlockingSecurityScanJobs(dict[str, object]):
    def __init__(self, insert_started: Event, allow_insert: Event) -> None:
        super().__init__()
        self._insert_started = insert_started
        self._allow_insert = allow_insert

    def __setitem__(self, key: str, value: object) -> None:
        self._insert_started.set()
        assert self._allow_insert.wait(2.0), "timed out waiting to insert scan job"
        super().__setitem__(key, value)


class TrackingLock:
    def __init__(self) -> None:
        self.entered = False

    def __enter__(self) -> TrackingLock:
        self.entered = True
        return self

    def __exit__(self, *_args: object) -> None:
        self.entered = False


class GuardedSecurityScanJobs(dict[str, object]):
    def __init__(self, lock: TrackingLock, *args: object) -> None:
        super().__init__(*args)
        self._lock = lock

    def values(self):  # type: ignore[no-untyped-def]
        assert self._lock.entered, "security scan jobs read without lock"
        return super().values()
