"""Resolve pending update candidates into security scan subjects."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, replace
from threading import Lock
from typing import TYPE_CHECKING, cast

from . import web_pending_sources
from .command import CommandRunner
from .compose import ComposeCli, ComposeDiscoveryError, ServiceImage
from .digest_verifier import DigestResolveResult, DigestVerifier, ResolvedImageSubject
from .docker_cli import DockerCli
from .images import image_with_tag, normalize_digest
from .plan_matching import _match_targets
from .platforms import ImagePlatform, platform_value
from .web_pending_sources import PendingSourceResult
from .wud_file import WudTarget

if TYPE_CHECKING:
    from .web_models import WebSettings


@dataclass(frozen=True)
class PendingSecurityRequest:
    line_no: int
    raw: str
    image: str
    candidate_image: str
    reported_digest: str
    platform: ImagePlatform | None
    platform_source: str
    current_image: str = ""
    current_digest: str = ""
    current_digest_source: str = ""
    missing_reported_digest_resolvable: bool = False
    identity_status: str = "pending"
    warnings: tuple[str, ...] = ()
    error: str = ""

    @property
    def request_key(self) -> str:
        return _hash_key(
            "security-request",
            str(self.line_no),
            self.raw,
            self.candidate_image,
            self.reported_digest,
            platform_value(self.platform),
        )


@dataclass(frozen=True)
class PendingSecurityContext:
    source: PendingSourceResult
    requests: tuple[PendingSecurityRequest, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingSecurityOptions:
    include_compose: bool = True
    include_wud_metadata: bool = True
    resolve_missing_digests: bool = True


PENDING_SECURITY_DEFAULT_OPTIONS = PendingSecurityOptions()
PENDING_SECURITY_READ_OPTIONS = PendingSecurityOptions(include_compose=False)
PENDING_SECURITY_CACHE_OPTIONS = PendingSecurityOptions(
    include_compose=False,
    include_wud_metadata=False,
    resolve_missing_digests=False,
)
_MISSING_DIGEST_FAILURE_CACHE_TTL_SECONDS = 30.0
_missing_digest_failure_cache: dict[str, tuple[float, DigestResolveResult]] = {}
_missing_digest_failure_cache_lock = Lock()


def pending_security_context(
    settings: WebSettings,
    *,
    options: PendingSecurityOptions = PENDING_SECURITY_DEFAULT_OPTIONS,
) -> PendingSecurityContext:
    source = web_pending_sources.resolve_pending_source(
        settings,
        include_wud_metadata=options.include_wud_metadata,
    )
    platform_by_line: dict[int, ImagePlatform] = {}
    platform_conflicts: set[int] = set()
    warnings: tuple[str, ...] = ()
    if options.include_compose:
        platform_by_line, platform_conflicts, warnings = _compose_platforms_by_line(
            settings,
            source,
        )
    requests = tuple(
        _request_for_target(
            target,
            compose_platform=platform_by_line.get(target.line_no),
            compose_platform_conflict=target.line_no in platform_conflicts,
            wud_platform=_wud_platform(source, target),
            current_image=_current_image(source, target),
            current_digest=_current_digest(source, target),
        )
        for target in source.parsed.targets
    )
    if options.resolve_missing_digests:
        requests = _resolve_missing_reported_digests(settings, requests)
    return PendingSecurityContext(source=source, requests=requests, warnings=warnings)


def resolve_security_subject(
    request: PendingSecurityRequest,
    verifier: DigestVerifier,
) -> ResolvedImageSubject:
    if request.identity_status != "pending":
        return _unresolved_subject(request)
    subject = verifier.resolve_subject(
        request.candidate_image,
        request.reported_digest,
        request.platform,
        platform_source=request.platform_source,
    )
    return cast(
        ResolvedImageSubject,
        replace(
            subject,
            warnings=(*request.warnings, *subject.warnings),
        ),
    )


def current_security_request(
    request: PendingSecurityRequest,
) -> PendingSecurityRequest | None:
    if not request.current_digest:
        return None
    identity_status = "pending" if request.platform is not None else "unsupported"
    error = "" if request.platform is not None else "platform is required"
    return cast(
        PendingSecurityRequest,
        replace(
            request,
            raw=f"{request.raw} current_sha256={request.current_digest}",
            candidate_image=request.current_image or request.image,
            reported_digest=request.current_digest,
            current_image="",
            current_digest="",
            current_digest_source="",
            missing_reported_digest_resolvable=False,
            identity_status=identity_status,
            error=error,
        ),
    )


def resolve_current_security_subject(
    request: PendingSecurityRequest,
    verifier: DigestVerifier,
) -> ResolvedImageSubject:
    current = current_security_request(request)
    if current is None:
        return ResolvedImageSubject(
            requested_ref=request.current_image or request.image,
            platform_source=request.platform_source,
            identity_status="unsupported",
            error="installed digest is unavailable",
        )
    subject = verifier.resolve_digest_subject(
        current.candidate_image,
        current.reported_digest,
        current.platform,
        platform_source=current.platform_source,
    )
    return cast(
        ResolvedImageSubject,
        replace(
            subject,
            warnings=(*request.warnings, *subject.warnings),
        ),
    )


def subject_id(subject: ResolvedImageSubject) -> str:
    if subject.identity_status != "exact":
        return ""
    return _hash_key(
        "security-subject",
        subject.canonical_registry,
        subject.canonical_repository,
        subject.manifest_digest,
        subject.platform,
    )


def default_digest_verifier(settings: WebSettings) -> DigestVerifier:
    runner = CommandRunner(env=settings.command_env)
    return DigestVerifier(DockerCli(runner=runner))


def _request_for_target(
    target: WudTarget,
    *,
    compose_platform: ImagePlatform | None,
    compose_platform_conflict: bool = False,
    wud_platform: ImagePlatform | None,
    current_image: str = "",
    current_digest: str = "",
) -> PendingSecurityRequest:
    candidate_image = (
        image_with_tag(target.first, target.desired_tag)
        if target.desired_tag
        else target.first
    )
    platform = compose_platform or wud_platform
    platform_source = "compose" if compose_platform is not None else "wud"
    if compose_platform is None and wud_platform is None:
        platform_source = ""
    identity_status = "pending"
    error = ""
    if compose_platform_conflict:
        identity_status = "mismatch"
        error = "Multiple Compose platforms matched WUD line"
    elif compose_platform is not None and wud_platform is not None:
        if compose_platform != wud_platform:
            identity_status = "mismatch"
            error = "Compose platform conflicts with WUD platform"
    missing_reported_digest_resolvable = False
    if not target.digest and identity_status == "pending":
        identity_status = "unsupported"
        error = "reported digest is required"
        missing_reported_digest_resolvable = platform is not None
    if platform is None and identity_status == "pending":
        identity_status = "unsupported"
        error = "platform is required"
    return PendingSecurityRequest(
        line_no=target.line_no,
        raw=target.raw,
        image=target.first,
        candidate_image=candidate_image,
        reported_digest=target.digest,
        platform=platform,
        platform_source=platform_source,
        current_image=current_image,
        current_digest=normalize_digest(current_digest),
        current_digest_source="wud" if current_digest else "",
        missing_reported_digest_resolvable=missing_reported_digest_resolvable,
        identity_status=identity_status,
        error=error,
    )


def _resolve_missing_reported_digests(
    settings: WebSettings,
    requests: tuple[PendingSecurityRequest, ...],
) -> tuple[PendingSecurityRequest, ...]:
    if not settings.security_scan.enabled:
        return requests
    resolver: DigestVerifier | None = None
    resolved_by_image: dict[str, DigestResolveResult] = {}
    updated: list[PendingSecurityRequest] = []
    now = time.monotonic()
    for request in requests:
        if not request.missing_reported_digest_resolvable:
            updated.append(request)
            continue
        result = resolved_by_image.get(request.candidate_image)
        if result is None:
            result, resolver = _resolve_missing_reported_digest(
                settings,
                request.candidate_image,
                now,
                resolver,
            )
            resolved_by_image[request.candidate_image] = result
        if result.ok and result.digest:
            updated.append(
                replace(
                    request,
                    reported_digest=result.digest,
                    missing_reported_digest_resolvable=False,
                    identity_status="pending",
                    error="",
                )
            )
            continue
        updated.append(
            replace(
                request,
                warnings=(
                    *request.warnings,
                    _reported_digest_lookup_warning(request, result),
                ),
            )
        )
    return tuple(updated)


def _resolve_missing_reported_digest(
    settings: WebSettings,
    image: str,
    now: float,
    resolver: DigestVerifier | None,
) -> tuple[DigestResolveResult, DigestVerifier | None]:
    result = _cached_missing_digest_failure(image, now)
    if result is not None:
        return result, resolver
    if resolver is None:
        resolver = default_digest_verifier(settings)
    result = resolver.resolve_tag_digest(image)
    _remember_missing_digest_result(image, result, now)
    return result, resolver


def _cached_missing_digest_failure(
    image: str,
    now: float,
) -> DigestResolveResult | None:
    with _missing_digest_failure_cache_lock:
        cached = _missing_digest_failure_cache.get(image)
        if cached is None:
            return None
        checked_at, result = cached
        if now - checked_at < _MISSING_DIGEST_FAILURE_CACHE_TTL_SECONDS:
            return result
        _missing_digest_failure_cache.pop(image, None)
    return None


def _remember_missing_digest_result(
    image: str,
    result: DigestResolveResult,
    now: float,
) -> None:
    with _missing_digest_failure_cache_lock:
        expired = [
            cached_image
            for cached_image, (
                checked_at,
                _result,
            ) in _missing_digest_failure_cache.items()
            if now - checked_at >= _MISSING_DIGEST_FAILURE_CACHE_TTL_SECONDS
        ]
        for cached_image in expired:
            _missing_digest_failure_cache.pop(cached_image, None)
        if result.ok and result.digest:
            _missing_digest_failure_cache.pop(image, None)
        else:
            _missing_digest_failure_cache[image] = (now, result)


def _reported_digest_lookup_warning(
    request: PendingSecurityRequest,
    result: DigestResolveResult,
) -> str:
    detail = result.error or result.reason or result.status
    suffix = f": {detail}" if detail else ""
    return f"Could not resolve reported digest for {request.candidate_image}{suffix}"


def _wud_platform(
    source: PendingSourceResult,
    target: WudTarget,
) -> ImagePlatform | None:
    if target.platform is not None:
        return target.platform
    metadata = (source.metadata_by_line or {}).get(target.line_no)
    if metadata is not None:
        return metadata.platform
    return None


def _current_image(source: PendingSourceResult, target: WudTarget) -> str:
    metadata = (source.metadata_by_line or {}).get(target.line_no)
    if metadata is not None and metadata.image:
        return metadata.image
    return target.first


def _current_digest(source: PendingSourceResult, target: WudTarget) -> str:
    metadata = (source.metadata_by_line or {}).get(target.line_no)
    if metadata is not None:
        return metadata.local_digest
    return ""


def _compose_platforms_by_line(
    settings: WebSettings,
    source: PendingSourceResult,
) -> tuple[dict[int, ImagePlatform], set[int], tuple[str, ...]]:
    runner = CommandRunner(env=settings.command_env)
    compose = ComposeCli(runner=runner)
    docker = DockerCli(runner=runner)
    try:
        stacks = compose.discover_stacks(
            settings.config.docker_base,
            project_base=settings.host_docker_base,
            ignore_paths=settings.config.compose_ignore_paths,
        )
    except ComposeDiscoveryError as exc:
        return {}, set(), (str(exc),)
    matches, _skipped = _match_targets(
        source.parsed,
        stacks,
        docker,
        allow_tag_updates=True,
        allow_digest_pin_rematch=settings.config.digest_pin_updates,
    )
    platforms: dict[int, ImagePlatform] = {}
    conflicts: set[int] = set()
    warnings: list[str] = []
    for match in matches:
        line_no = match.target.line_no
        if line_no in conflicts:
            continue
        platform = _platform_for_match(
            match.stack.service_images,
            match.service,
            match.compose_image,
        )
        if platform is None:
            continue
        existing = platforms.get(line_no)
        if existing is not None and existing != platform:
            platforms.pop(line_no, None)
            conflicts.add(line_no)
            warnings.append(
                f"Multiple Compose platforms matched WUD line {line_no}"
            )
            continue
        platforms[line_no] = platform
    return platforms, conflicts, tuple(warnings)


def _platform_for_match(
    service_images: tuple[ServiceImage, ...],
    service: str,
    image: str,
) -> ImagePlatform | None:
    for item in service_images:
        if service and item.service != service:
            continue
        if item.image == image:
            return item.platform
    return None


def _unresolved_subject(request: PendingSecurityRequest) -> ResolvedImageSubject:
    platform = request.platform
    return ResolvedImageSubject(
        requested_ref=request.candidate_image,
        reported_digest=request.reported_digest,
        os=platform.os if platform is not None else "",
        architecture=platform.architecture if platform is not None else "",
        variant=platform.variant if platform is not None else "",
        platform_source=request.platform_source,
        identity_status=request.identity_status,
        warnings=request.warnings,
        error=request.error,
    )


def _hash_key(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
