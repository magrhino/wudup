"""Digest verification helpers for WUD-reported image updates."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .command import CommandError
from .docker_cli import DockerCli
from .images import normalize_digest, strip_digest
from .platforms import ImagePlatform, platform_from_parts, platform_value

GHCR_REGISTRY = "ghcr.io"
DOCKER_HUB_REGISTRIES = frozenset(
    ("docker.io", "index.docker.io", "registry-1.docker.io")
)
DOCKER_HUB_HTTP_REGISTRY = "registry-1.docker.io"
DEFAULT_REGISTRY_TIMEOUT = 5.0
_SHA256_PREFIX = "sha256:"
_STATUS_FAILED = "failed"
_STATUS_VERIFIED = "verified"
_STATUS_UNTRUSTED = "untrusted"
_REASON_PLATFORM_MISMATCH = "platform-mismatch"
_REASON_MANIFEST_CONFIG_MISSING = "manifest-config-missing"
IdentityStatus = Literal[
    "exact",
    "mismatch",
    "stale",
    "unsupported",
    "auth_required",
    "error",
]
_DOCKER_MANIFEST_LIST_MEDIA_TYPE = (
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        _DOCKER_MANIFEST_LIST_MEDIA_TYPE,
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
INDEX_MEDIA_TYPES = frozenset(
    (
        "application/vnd.oci.image.index.v1+json",
        _DOCKER_MANIFEST_LIST_MEDIA_TYPE,
    )
)
IMAGE_MANIFEST_MEDIA_TYPES = frozenset(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
VERBOSE_MANIFEST_LIST_MEDIA_TYPE = _DOCKER_MANIFEST_LIST_MEDIA_TYPE


class ManifestLookupError(RuntimeError):
    """Raised when a registry manifest cannot be resolved or parsed."""


class ManifestIntegrityError(RuntimeError):
    """Raised when manifest bytes do not match their claimed identity."""


class ManifestResolver(Protocol):
    def fetch(self, image: RegistryImageRef, reference: str) -> ManifestDocument:
        """Return a manifest or index document for ``image`` and ``reference``."""


@dataclass(frozen=True)
class RegistryImageRef:
    registry: str
    http_registry: str
    repo: str
    tag: str

    def is_ghcr(self) -> bool:
        return self.registry == GHCR_REGISTRY

    def manifest_ref(self, reference: str) -> str:
        if reference.startswith(_SHA256_PREFIX):
            return f"{self.registry}/{self.repo}@{reference}"
        return f"{self.registry}/{self.repo}:{reference}"


@dataclass(frozen=True)
class ManifestDocument:
    source: str
    digest: str
    media_type: str
    payload: Mapping[str, Any]
    raw_body: bytes = b""

    def is_index(self) -> bool:
        return self.media_type in INDEX_MEDIA_TYPES or isinstance(
            self.payload.get("manifests"), list
        )

    def is_image_manifest(self) -> bool:
        return self.media_type in IMAGE_MANIFEST_MEDIA_TYPES or isinstance(
            self.payload.get("config"), Mapping
        )

    def child_digest(self, digest: str) -> str:
        for manifest in self._manifest_items():
            if manifest.get("digest") == digest:
                return digest
        return ""

    def platform_child_digest(self, digest: str) -> str:
        for manifest in self._manifest_items():
            if manifest.get("digest") == digest and _real_platform(
                manifest.get("platform")
            ):
                return digest
        return ""

    def child_platform(self, digest: str) -> ImagePlatform | None:
        for manifest in self._manifest_items():
            if manifest.get("digest") != digest:
                continue
            return _platform_from_descriptor(manifest.get("platform"))
        return None

    def matching_platform_child_digest(self, platform: ImagePlatform) -> str:
        matches: list[str] = []
        for manifest in self._manifest_items():
            digest = manifest.get("digest")
            if not isinstance(digest, str) or not digest:
                continue
            child_platform = _platform_from_descriptor(manifest.get("platform"))
            if child_platform is not None and _platform_matches(
                child_platform,
                platform,
            ):
                matches.append(digest)
        return matches[0] if len(matches) == 1 else ""

    def child_digests(self) -> tuple[str, ...]:
        return tuple(
            digest
            for manifest in self._manifest_items()
            if isinstance((digest := manifest.get("digest")), str) and digest
        )

    def config_digest(self) -> str:
        config = self.payload.get("config")
        if not isinstance(config, Mapping):
            return ""
        digest = config.get("digest")
        return digest if isinstance(digest, str) else ""

    def _manifest_items(self) -> tuple[Mapping[str, Any], ...]:
        manifests = self.payload.get("manifests")
        if not isinstance(manifests, list):
            return ()
        return tuple(item for item in manifests if isinstance(item, Mapping))


@dataclass(frozen=True)
class DigestCheckResult:
    ok: bool
    status: str
    reason: str
    seen_repo_digests: tuple[str, ...] = ()
    tag_digest: str = ""
    matched_child_digest: str = ""
    expected_config_digest: str = ""
    local_image_id: str = ""
    source: str = ""
    error: str = ""


@dataclass(frozen=True)
class DigestResolveResult:
    ok: bool
    status: str
    reason: str
    digest: str = ""
    source: str = ""
    error: str = ""


@dataclass(frozen=True)
class ResolvedImageSubject:
    canonical_registry: str = ""
    canonical_repository: str = ""
    requested_ref: str = ""
    reported_digest: str = ""
    index_digest: str = ""
    manifest_digest: str = ""
    os: str = ""
    architecture: str = ""
    variant: str = ""
    platform_source: str = ""
    identity_status: IdentityStatus = "unsupported"
    warnings: tuple[str, ...] = ()
    source: str = ""
    error: str = ""

    @property
    def platform(self) -> str:
        if not self.os or not self.architecture:
            return ""
        return platform_value(
            ImagePlatform(
                os=self.os,
                architecture=self.architecture,
                variant=self.variant,
            )
        )

    @property
    def immutable_ref(self) -> str:
        if not self.canonical_registry or not self.canonical_repository:
            return ""
        if not self.manifest_digest:
            return ""
        return (
            f"{self.canonical_registry}/{self.canonical_repository}"
            f"@{self.manifest_digest}"
        )


class RegistryHttpManifestResolver:
    """Resolve public registry manifests through the distribution HTTP API."""

    def __init__(self, *, timeout: float = DEFAULT_REGISTRY_TIMEOUT) -> None:
        self.timeout = timeout
        self._tokens: dict[str, str] = {}

    def fetch(self, image: RegistryImageRef, reference: str) -> ManifestDocument:
        url = f"https://{image.http_registry}/v2/{image.repo}/manifests/{reference}"
        headers, payload, body = self._request_json(url, reference=reference)
        return ManifestDocument(
            source=f"registry-http:{image.http_registry}",
            digest=reference if ":" in reference else _manifest_digest(body),
            media_type=_content_type(_header_value(headers, "Content-Type")),
            payload=payload,
            raw_body=body,
        )

    def _request_json(
        self,
        url: str,
        *,
        accept: str = MANIFEST_ACCEPT,
        token: str = "",
        reference: str = "",
    ) -> tuple[Mapping[str, str], Mapping[str, Any], bytes]:
        request = urllib.request.Request(url)
        request.add_header("Accept", accept)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                headers = {key: value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or token:
                raise ManifestLookupError(
                    f"registry request failed for {url}: {exc}"
                ) from exc
            challenge = exc.headers.get("WWW-Authenticate", "")
            return self._request_json(
                url,
                accept=accept,
                token=self._token(challenge),
                reference=reference,
            )
        except OSError as exc:
            raise ManifestLookupError(f"registry request failed for {url}: {exc}") from exc
        if reference:
            # Authenticate the original bytes before parsing; JSON reserialization
            # changes the content-addressed identity, and invalid JSON may be tampered.
            advertised = _header_value(headers, "Docker-Content-Digest")
            requested = reference if ":" in reference else ""
            for digest in (requested, advertised):
                if digest:
                    _verify_manifest_digest(body, digest)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestLookupError(
                f"registry response was not valid JSON for {url}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ManifestLookupError(
                f"registry response was not a JSON object for {url}"
            )
        if reference and isinstance(payload.get("manifests"), list):
            for child in payload["manifests"]:
                if not isinstance(child, Mapping) or not _valid_manifest_digest(child.get("digest")):
                    raise ManifestIntegrityError(
                        "Registry manifest integrity check failed: an image index "
                        "contains an invalid child digest. Check the registry before retrying."
                    )
        return headers, payload, body

    def _token(self, challenge: str) -> str:
        cached = self._tokens.get(challenge)
        if cached:
            return cached
        scheme, _sep, rest = challenge.partition(" ")
        if scheme.lower() != "bearer" or not rest:
            raise ManifestLookupError(
                f"unsupported registry auth challenge: {challenge}"
            )
        values = urllib.request.parse_keqv_list(urllib.request.parse_http_list(rest))
        realm = values.get("realm")
        if not realm:
            raise ManifestLookupError(
                f"registry auth challenge did not include a realm: {challenge}"
            )
        query = {
            key: value
            for key in ("service", "scope")
            if isinstance((value := values.get(key)), str) and value
        }
        separator = "&" if urllib.parse.urlparse(realm).query else "?"
        url = realm + (separator + urllib.parse.urlencode(query) if query else "")
        _headers, payload, _body = self._request_json(url, accept="application/json")
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise ManifestLookupError(
                f"registry token response for {url} did not include a token"
            )
        self._tokens[challenge] = token
        return token


class DockerManifestResolver:
    """Resolve manifests through ``docker manifest inspect``."""

    def __init__(self, docker: DockerCli, *, verbose: bool = False) -> None:
        self.docker = docker
        self.verbose = verbose

    def fetch(self, image: RegistryImageRef, reference: str) -> ManifestDocument:
        manifest_ref = image.manifest_ref(reference)
        try:
            result = (
                self.docker.manifest_inspect_verbose(manifest_ref)
                if self.verbose
                else self.docker.manifest_inspect(manifest_ref)
            )
        except CommandError as exc:
            raise ManifestLookupError(
                f"docker manifest inspect failed for {manifest_ref}"
            ) from exc
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ManifestLookupError(
                f"docker manifest inspect returned invalid JSON for {manifest_ref}"
            ) from exc
        if self.verbose:
            payload = _normalize_verbose_manifest_payload(payload, manifest_ref)
        elif not isinstance(payload, Mapping):
            raise ManifestLookupError(
                f"docker manifest inspect returned non-object JSON for {manifest_ref}"
            )
        return ManifestDocument(
            source="docker-manifest-verbose" if self.verbose else "docker-manifest",
            digest=_payload_digest(payload) if self.verbose else "",
            media_type=str(payload.get("mediaType") or ""),
            payload=payload,
        )


class DigestVerifier:
    """Verify that a pulled local image satisfies a WUD-reported digest."""

    def __init__(
        self,
        docker: DockerCli,
        *,
        primary_resolver: ManifestResolver | None = None,
        fallback_resolver: ManifestResolver | None = None,
    ) -> None:
        self.docker = docker
        self.primary_resolver = primary_resolver or RegistryHttpManifestResolver()
        self.fallback_resolver = fallback_resolver or DockerManifestResolver(docker)

    def verify(self, image: str, expected: str) -> DigestCheckResult:
        try:
            return self._verify(image, expected)
        except ManifestIntegrityError as exc:
            return DigestCheckResult(
                ok=False, status=_STATUS_FAILED,
                reason="manifest-integrity-mismatch", error=str(exc),
            )

    def _verify(self, image: str, expected: str) -> DigestCheckResult:
        repo_digests = tuple(self.docker.image_repo_digests(image))
        local_image_id = self.docker.image_id(image)
        if any(digest.rsplit("@", 1)[-1] == expected for digest in repo_digests):
            return DigestCheckResult(
                ok=True,
                status=_STATUS_VERIFIED,
                reason="repo-digest-match",
                seen_repo_digests=repo_digests,
                local_image_id=local_image_id,
            )

        registry_image = parse_registry_image(image)
        if registry_image is None:
            return DigestCheckResult(
                ok=False,
                status=_STATUS_UNTRUSTED,
                reason="repo-digest-mismatch",
                seen_repo_digests=repo_digests,
                local_image_id=local_image_id,
            )
        if not local_image_id:
            return DigestCheckResult(
                ok=False,
                status=_failure_status(registry_image),
                reason="missing-local-image-id",
                seen_repo_digests=repo_digests,
            )

        try:
            tag_document = self._fetch(registry_image, registry_image.tag)
        except ManifestLookupError as exc:
            return DigestCheckResult(
                ok=False,
                status=_failure_status(registry_image),
                reason=_manifest_unavailable_reason(registry_image),
                seen_repo_digests=repo_digests,
                local_image_id=local_image_id,
                error=str(exc),
            )

        if tag_document.digest == expected:
            return self._verify_tag_document(
                registry_image,
                tag_document,
                expected,
                repo_digests,
                local_image_id,
            )

        if tag_document.is_index() and tag_document.child_digest(expected):
            return self._verify_expected_manifest(
                registry_image,
                expected,
                tag_document,
                repo_digests,
                local_image_id,
            )

        if not tag_document.digest:
            expected_document = self._try_fetch(registry_image, expected)
            if expected_document is not None and _same_manifest(
                tag_document.payload,
                expected_document.payload,
            ):
                return self._verify_tag_document(
                    registry_image,
                    tag_document,
                    expected,
                    repo_digests,
                    local_image_id,
                )

        return DigestCheckResult(
            ok=False,
            status=_STATUS_FAILED,
            reason="stale-digest",
            seen_repo_digests=repo_digests,
            tag_digest=tag_document.digest,
            local_image_id=local_image_id,
            source=tag_document.source,
        )

    def resolve_tag_digest(self, image: str) -> DigestResolveResult:
        try:
            return self._resolve_tag_digest(image)
        except ManifestIntegrityError as exc:
            return DigestResolveResult(
                ok=False, status=_STATUS_FAILED,
                reason="manifest-integrity-mismatch", error=str(exc),
            )

    def _resolve_tag_digest(self, image: str) -> DigestResolveResult:
        registry_image = parse_registry_image(image)
        if registry_image is None:
            return DigestResolveResult(
                ok=False,
                status=_STATUS_UNTRUSTED,
                reason="unsupported-image-reference",
            )
        try:
            tag_document = self._fetch(registry_image, registry_image.tag)
        except ManifestLookupError as exc:
            return DigestResolveResult(
                ok=False,
                status=_failure_status(registry_image),
                reason=_manifest_unavailable_reason(registry_image),
                error=str(exc),
            )
        digest = tag_document.digest or _payload_digest(tag_document.payload)
        if not digest and tag_document.is_index():
            digest = self._resolve_index_digest(registry_image)
        if not digest:
            return DigestResolveResult(
                ok=False,
                status=_failure_status(registry_image),
                reason="manifest-digest-missing",
                source=tag_document.source,
            )
        return DigestResolveResult(
            ok=True,
            status="resolved",
            reason="tag-digest-resolved",
            digest=digest,
            source=tag_document.source,
        )

    def verify_tag_digest(self, image: str, expected: str) -> DigestResolveResult:
        try:
            return self._verify_tag_digest(image, expected)
        except ManifestIntegrityError as exc:
            return DigestResolveResult(
                ok=False, status=_STATUS_FAILED,
                reason="manifest-integrity-mismatch", error=str(exc),
            )

    def _verify_tag_digest(self, image: str, expected: str) -> DigestResolveResult:
        expected = normalize_digest(expected)
        if not expected:
            return DigestResolveResult(
                ok=False,
                status=_STATUS_FAILED,
                reason="expected-digest-missing",
            )
        registry_image = parse_registry_image(image)
        if registry_image is None:
            return DigestResolveResult(
                ok=False,
                status=_STATUS_UNTRUSTED,
                reason="unsupported-image-reference",
            )
        try:
            tag_document = self._fetch(registry_image, registry_image.tag)
        except ManifestLookupError as exc:
            return DigestResolveResult(
                ok=False,
                status=_failure_status(registry_image),
                reason=_manifest_unavailable_reason(registry_image),
                error=str(exc),
            )

        tag_digest = normalize_digest(
            tag_document.digest or _payload_digest(tag_document.payload)
        )
        if not tag_digest and tag_document.is_index():
            tag_digest = self._resolve_index_digest(registry_image)
        if tag_digest == expected:
            return DigestResolveResult(
                ok=True,
                status=_STATUS_VERIFIED,
                reason="tag-digest-current",
                digest=expected,
                source=tag_document.source,
            )
        if tag_document.is_index() and tag_document.platform_child_digest(expected):
            return DigestResolveResult(
                ok=True,
                status=_STATUS_VERIFIED,
                reason="tag-child-digest-current",
                digest=expected,
                source=tag_document.source,
            )
        return DigestResolveResult(
            ok=False,
            status=_STATUS_FAILED,
            reason="stale-digest",
            digest=tag_digest,
            source=tag_document.source,
        )

    def resolve_subject(
        self,
        image: str,
        reported_digest: str,
        platform: ImagePlatform | None,
        *,
        platform_source: str = "",
    ) -> ResolvedImageSubject:
        lookup = self._fetch_subject_document(
            image,
            reported_digest,
            platform,
            platform_source=platform_source,
            use_reported_digest=False,
        )
        if isinstance(lookup, ResolvedImageSubject):
            return lookup
        registry_image, reported_digest, tag_document = lookup
        assert platform is not None

        tag_digest = _tag_document_digest(tag_document)
        if tag_document.is_index():
            return self._resolve_index_subject(
                registry_image,
                tag_document,
                reported_digest,
                platform,
                platform_source,
                tag_digest,
            )
        if tag_document.is_image_manifest():
            return _resolve_image_manifest_subject(
                registry_image,
                tag_document,
                reported_digest,
                platform,
                platform_source,
                tag_digest,
            )
        return _resolved_subject(
            registry_image,
            reported_digest,
            platform,
            platform_source,
            identity_status="unsupported",
            source=tag_document.source,
            error="manifest document was not an image manifest or index",
        )

    def resolve_digest_subject(
        self,
        image: str,
        reported_digest: str,
        platform: ImagePlatform | None,
        *,
        platform_source: str = "",
    ) -> ResolvedImageSubject:
        lookup = self._fetch_subject_document(
            image,
            reported_digest,
            platform,
            platform_source=platform_source,
            use_reported_digest=True,
        )
        if isinstance(lookup, ResolvedImageSubject):
            return lookup
        registry_image, reported_digest, digest_document = lookup
        assert platform is not None

        document_digest = _tag_document_digest(digest_document) or reported_digest
        if digest_document.is_index():
            return _resolve_reported_index_subject(
                registry_image,
                digest_document,
                reported_digest,
                platform,
                platform_source,
                document_digest,
            )
        if digest_document.is_image_manifest():
            return _resolved_subject(
                registry_image,
                reported_digest,
                platform,
                platform_source,
                identity_status="exact",
                manifest_digest=reported_digest,
                source=digest_document.source,
                warnings=("single-manifest platform was supplied by metadata",),
            )
        return _resolved_subject(
            registry_image,
            reported_digest,
            platform,
            platform_source,
            identity_status="unsupported",
            source=digest_document.source,
            error="manifest document was not an image manifest or index",
        )

    def _fetch_subject_document(
        self,
        image: str,
        reported_digest: str,
        platform: ImagePlatform | None,
        *,
        platform_source: str,
        use_reported_digest: bool,
    ) -> tuple[RegistryImageRef, str, ManifestDocument] | ResolvedImageSubject:
        reported_digest = normalize_digest(reported_digest)
        registry_image = parse_registry_image(image)
        if registry_image is None:
            return ResolvedImageSubject(
                requested_ref=image,
                reported_digest=reported_digest,
                platform_source=platform_source,
                identity_status="unsupported",
                error="unsupported image reference",
            )
        if not reported_digest:
            return _resolved_subject(
                registry_image,
                reported_digest,
                None,
                platform_source,
                identity_status="unsupported",
                error="reported digest is required",
            )
        if platform is None:
            return _resolved_subject(
                registry_image,
                reported_digest,
                platform,
                platform_source,
                identity_status="unsupported",
                error="platform is required",
            )

        try:
            document = self._fetch(
                registry_image,
                reported_digest if use_reported_digest else registry_image.tag,
            )
        except (ManifestLookupError, ManifestIntegrityError) as exc:
            return _resolved_subject(
                registry_image,
                reported_digest,
                platform,
                platform_source,
                identity_status=(
                    "mismatch" if isinstance(exc, ManifestIntegrityError)
                    else _subject_error_status(str(exc))
                ),
                error=str(exc),
            )
        return registry_image, reported_digest, document

    def _resolve_index_subject(
        self,
        registry_image: RegistryImageRef,
        tag_document: ManifestDocument,
        reported_digest: str,
        platform: ImagePlatform,
        platform_source: str,
        tag_digest: str,
    ) -> ResolvedImageSubject:
        if not tag_digest:
            try:
                tag_digest = self._resolve_index_digest(registry_image)
            except ManifestIntegrityError as exc:
                return _resolved_subject(
                    registry_image, reported_digest, platform, platform_source,
                    identity_status="mismatch", error=str(exc),
                )
        if tag_digest and reported_digest == tag_digest:
            return _resolve_reported_index_subject(
                registry_image,
                tag_document,
                reported_digest,
                platform,
                platform_source,
                tag_digest,
            )
        child_platform = tag_document.child_platform(reported_digest)
        if child_platform is not None:
            return _resolve_reported_child_subject(
                registry_image,
                tag_document,
                reported_digest,
                platform,
                platform_source,
                tag_digest,
                child_platform,
            )
        if not tag_digest:
            return _resolved_subject(
                registry_image,
                reported_digest,
                platform,
                platform_source,
                identity_status="error",
                source=tag_document.source,
                error="current index digest could not be resolved",
            )
        return _resolved_subject(
            registry_image,
            reported_digest,
            platform,
            platform_source,
            identity_status="stale",
            index_digest=tag_digest,
            source=tag_document.source,
            error="reported digest is not in the current index",
        )

    def _resolve_index_digest(
        self,
        image: RegistryImageRef,
    ) -> str:
        resolver = (
            self.primary_resolver
            if isinstance(self.primary_resolver, RegistryHttpManifestResolver)
            else RegistryHttpManifestResolver()
        )
        try:
            document = resolver.fetch(image, image.tag)
        except ManifestLookupError:
            return ""
        if not document.is_index():
            return ""
        return document.digest or _payload_digest(document.payload)

    def _verify_expected_manifest(
        self,
        image: RegistryImageRef,
        expected: str,
        tag_document: ManifestDocument,
        repo_digests: tuple[str, ...],
        local_image_id: str,
    ) -> DigestCheckResult:
        try:
            expected_document = self._fetch(image, expected)
        except ManifestLookupError as exc:
            return DigestCheckResult(
                ok=False,
                status=_failure_status(image),
                reason=_manifest_unavailable_reason(image),
                seen_repo_digests=repo_digests,
                tag_digest=tag_document.digest,
                matched_child_digest=expected,
                local_image_id=local_image_id,
                source=tag_document.source,
                error=str(exc),
            )
        expected_config = expected_document.config_digest()
        if expected_config and expected_config == local_image_id:
            return DigestCheckResult(
                ok=True,
                status=_STATUS_VERIFIED,
                reason=_reason(image, "platform-manifest-match"),
                seen_repo_digests=repo_digests,
                tag_digest=tag_document.digest,
                matched_child_digest=expected,
                expected_config_digest=expected_config,
                local_image_id=local_image_id,
                source=expected_document.source,
            )
        return DigestCheckResult(
            ok=False,
            status=_STATUS_FAILED,
            reason=_config_mismatch_reason(expected_config),
            seen_repo_digests=repo_digests,
            tag_digest=tag_document.digest,
            matched_child_digest=expected,
            expected_config_digest=expected_config,
            local_image_id=local_image_id,
            source=expected_document.source,
        )

    def _verify_tag_document(
        self,
        image: RegistryImageRef,
        tag_document: ManifestDocument,
        expected: str,
        repo_digests: tuple[str, ...],
        local_image_id: str,
    ) -> DigestCheckResult:
        if tag_document.is_image_manifest():
            return self._verify_image_manifest_document(
                image,
                tag_document,
                expected,
                repo_digests,
                local_image_id,
            )
        return self._verify_index_manifest_document(
            image,
            tag_document,
            expected,
            repo_digests,
            local_image_id,
        )

    def _verify_image_manifest_document(
        self,
        image: RegistryImageRef,
        tag_document: ManifestDocument,
        expected: str,
        repo_digests: tuple[str, ...],
        local_image_id: str,
    ) -> DigestCheckResult:
        config_digest = tag_document.config_digest()
        if config_digest and config_digest == local_image_id:
            return DigestCheckResult(
                ok=True,
                status=_STATUS_VERIFIED,
                reason=_reason(image, "tag-manifest-match"),
                seen_repo_digests=repo_digests,
                tag_digest=tag_document.digest or expected,
                expected_config_digest=config_digest,
                local_image_id=local_image_id,
                source=tag_document.source,
            )
        return DigestCheckResult(
            ok=False,
            status=_STATUS_FAILED,
            reason=_config_mismatch_reason(config_digest),
            seen_repo_digests=repo_digests,
            tag_digest=tag_document.digest or expected,
            expected_config_digest=config_digest,
            local_image_id=local_image_id,
            source=tag_document.source,
        )

    def _verify_index_manifest_document(
        self,
        image: RegistryImageRef,
        tag_document: ManifestDocument,
        expected: str,
        repo_digests: tuple[str, ...],
        local_image_id: str,
    ) -> DigestCheckResult:
        for child_digest in tag_document.child_digests():
            child = self._try_fetch(image, child_digest)
            if child is None:
                continue
            config_digest = child.config_digest()
            if config_digest and config_digest == local_image_id:
                return DigestCheckResult(
                    ok=True,
                    status=_STATUS_VERIFIED,
                    reason=_reason(image, "index-manifest-match"),
                    seen_repo_digests=repo_digests,
                    tag_digest=tag_document.digest or expected,
                    matched_child_digest=child_digest,
                    expected_config_digest=config_digest,
                    local_image_id=local_image_id,
                    source=child.source,
                )
        return DigestCheckResult(
            ok=False,
            status=_STATUS_FAILED,
            reason=_REASON_PLATFORM_MISMATCH,
            seen_repo_digests=repo_digests,
            tag_digest=tag_document.digest or expected,
            local_image_id=local_image_id,
            source=tag_document.source,
        )

    def _fetch(self, image: RegistryImageRef, reference: str) -> ManifestDocument:
        primary_error: ManifestLookupError | None = None
        try:
            return self.primary_resolver.fetch(image, reference)
        except ManifestLookupError as exc:
            primary_error = exc
        try:
            return self.fallback_resolver.fetch(image, reference)
        except ManifestLookupError as exc:
            raise ManifestLookupError(f"{primary_error}; fallback failed: {exc}") from exc

    def _try_fetch(
        self,
        image: RegistryImageRef,
        reference: str,
    ) -> ManifestDocument | None:
        try:
            return self._fetch(image, reference)
        except ManifestLookupError:
            return None


def _manifest_digest(body: bytes, algorithm: str = "sha256") -> str:
    return f"{algorithm}:{hashlib.new(algorithm, body).hexdigest()}"


def _valid_manifest_digest(digest: object) -> bool:
    return isinstance(digest, str) and re.fullmatch(
        r"sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128}", digest,
    ) is not None


def _verify_manifest_digest(body: bytes, digest: str) -> None:
    algorithm, _separator, _encoded = digest.partition(":")
    if not _valid_manifest_digest(digest) or _manifest_digest(body, algorithm) != digest:
        raise ManifestIntegrityError(
            "Registry manifest integrity check failed: the response does not match "
            "its requested or advertised digest. No update can be verified from "
            "this response; check the registry before retrying."
        )


def _tag_document_digest(tag_document: ManifestDocument) -> str:
    return normalize_digest(tag_document.digest or _payload_digest(tag_document.payload))


def _resolve_reported_index_subject(
    registry_image: RegistryImageRef,
    tag_document: ManifestDocument,
    reported_digest: str,
    platform: ImagePlatform,
    platform_source: str,
    tag_digest: str,
) -> ResolvedImageSubject:
    child_digest = tag_document.matching_platform_child_digest(platform)
    if not child_digest:
        return _resolved_subject(
            registry_image,
            reported_digest,
            platform,
            platform_source,
            identity_status="mismatch",
            index_digest=tag_digest,
            source=tag_document.source,
            error="reported index digest has no matching platform child",
        )
    return _resolved_subject(
        registry_image,
        reported_digest,
        platform,
        platform_source,
        identity_status="exact",
        index_digest=tag_digest,
        manifest_digest=child_digest,
        source=tag_document.source,
    )


def _resolve_reported_child_subject(
    registry_image: RegistryImageRef,
    tag_document: ManifestDocument,
    reported_digest: str,
    platform: ImagePlatform,
    platform_source: str,
    tag_digest: str,
    child_platform: ImagePlatform,
) -> ResolvedImageSubject:
    if not _platform_matches(child_platform, platform):
        return _resolved_subject(
            registry_image,
            reported_digest,
            platform,
            platform_source,
            identity_status="mismatch",
            index_digest=tag_digest,
            source=tag_document.source,
            error="reported child digest platform does not match",
        )
    return _resolved_subject(
        registry_image,
        reported_digest,
        platform,
        platform_source,
        identity_status="exact",
        index_digest=tag_digest,
        manifest_digest=reported_digest,
        source=tag_document.source,
    )


def _resolve_image_manifest_subject(
    registry_image: RegistryImageRef,
    tag_document: ManifestDocument,
    reported_digest: str,
    platform: ImagePlatform,
    platform_source: str,
    tag_digest: str,
) -> ResolvedImageSubject:
    if reported_digest != tag_digest:
        return _resolved_subject(
            registry_image,
            reported_digest,
            platform,
            platform_source,
            identity_status="stale",
            source=tag_document.source,
            error="reported digest does not match the current manifest",
        )
    return _resolved_subject(
        registry_image,
        reported_digest,
        platform,
        platform_source,
        identity_status="exact",
        manifest_digest=reported_digest,
        source=tag_document.source,
        warnings=("single-manifest platform was supplied by metadata",),
    )


def _resolved_subject(
    registry_image: RegistryImageRef,
    reported_digest: str,
    platform: ImagePlatform | None,
    platform_source: str,
    *,
    identity_status: IdentityStatus,
    index_digest: str = "",
    manifest_digest: str = "",
    source: str = "",
    warnings: tuple[str, ...] = (),
    error: str = "",
) -> ResolvedImageSubject:
    return ResolvedImageSubject(
        canonical_registry=registry_image.registry,
        canonical_repository=registry_image.repo,
        requested_ref=registry_image.manifest_ref(registry_image.tag),
        reported_digest=reported_digest,
        index_digest=index_digest,
        manifest_digest=manifest_digest,
        os=platform.os if platform is not None else "",
        architecture=platform.architecture if platform is not None else "",
        variant=platform.variant if platform is not None else "",
        platform_source=platform_source,
        identity_status=identity_status,
        warnings=warnings,
        source=source,
        error=error,
    )


def parse_registry_image(image: str) -> RegistryImageRef | None:
    image = strip_digest(image).strip()
    if not image:
        return None
    first, sep, remainder = image.partition("/")
    if sep and _is_registry_prefix(first):
        registry = first.lower()
        repo_tag = remainder
    else:
        registry = "docker.io"
        repo_tag = image
        if "/" not in repo_tag:
            repo_tag = f"library/{repo_tag}"
    if not repo_tag:
        return None
    repo_path, tag_sep, tag = repo_tag.rpartition(":")
    if tag_sep == "" or "/" in tag or not repo_path or not tag:
        return None
    return RegistryImageRef(
        registry=registry,
        http_registry=(
            DOCKER_HUB_HTTP_REGISTRY
            if registry in DOCKER_HUB_REGISTRIES
            else registry
        ),
        repo=repo_path,
        tag=tag,
    )


def parse_ghcr_image(image: str) -> RegistryImageRef | None:
    parsed = parse_registry_image(image)
    if parsed is None or not parsed.is_ghcr():
        return None
    return parsed


def _failure_status(image: RegistryImageRef) -> str:
    return _STATUS_FAILED if image.is_ghcr() else _STATUS_UNTRUSTED


def _manifest_unavailable_reason(image: RegistryImageRef) -> str:
    return "manifest-unavailable" if image.is_ghcr() else "manifest-unavailable-untrusted"


def _reason(image: RegistryImageRef, suffix: str) -> str:
    prefix = "ghcr" if image.is_ghcr() else "registry"
    return f"{prefix}-{suffix}"


def _is_registry_prefix(value: str) -> bool:
    return "." in value or ":" in value or value == "localhost"


def _real_platform(value: Any) -> bool:
    return _platform_from_descriptor(value) is not None


def _platform_from_descriptor(value: Any) -> ImagePlatform | None:
    if not isinstance(value, Mapping):
        return None
    os_value = value.get("os")
    architecture = value.get("architecture")
    variant = value.get("variant")
    if not isinstance(os_value, str) or not isinstance(architecture, str):
        return None
    return platform_from_parts(
        os_value,
        architecture,
        variant if isinstance(variant, str) else "",
    )


def _platform_matches(found: ImagePlatform, wanted: ImagePlatform) -> bool:
    if found.os != wanted.os or found.architecture != wanted.architecture:
        return False
    return found.variant == wanted.variant


def _subject_error_status(error: str) -> IdentityStatus:
    lowered = error.lower()
    if "401" in lowered or "403" in lowered or "auth" in lowered:
        return "auth_required"
    return "error"


def _content_type(value: str) -> str:
    return value.split(";", 1)[0].strip()


def _header_value(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def _same_manifest(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
        right,
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload_digest(payload: Mapping[str, Any]) -> str:
    digest = payload.get("digest")
    if isinstance(digest, str) and digest.startswith(_SHA256_PREFIX):
        return digest
    descriptor = payload.get("Descriptor")
    if isinstance(descriptor, Mapping):
        digest = descriptor.get("digest")
        if isinstance(digest, str) and digest.startswith(_SHA256_PREFIX):
            return digest
    return ""


def _config_mismatch_reason(config_digest: str) -> str:
    return (
        _REASON_PLATFORM_MISMATCH
        if config_digest
        else _REASON_MANIFEST_CONFIG_MISSING
    )


def _normalize_verbose_manifest_payload(
    payload: Any,
    manifest_ref: str,
) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if not isinstance(payload, list):
        raise ManifestLookupError(
            f"docker manifest inspect --verbose returned unsupported JSON for {manifest_ref}"
        )

    manifests: list[Mapping[str, Any]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ManifestLookupError(
                f"docker manifest inspect --verbose returned unsupported JSON for {manifest_ref}"
            )
        descriptor = item.get("Descriptor")
        if not isinstance(descriptor, Mapping):
            raise ManifestLookupError(
                f"docker manifest inspect --verbose omitted descriptors for {manifest_ref}"
            )
        digest = descriptor.get("digest")
        if not isinstance(digest, str) or not digest:
            raise ManifestLookupError(
                f"docker manifest inspect --verbose omitted descriptor digests for {manifest_ref}"
            )
        manifests.append({**descriptor})

    if not manifests:
        raise ManifestLookupError(
            f"docker manifest inspect --verbose returned an empty manifest list for {manifest_ref}"
        )
    return {
        "schemaVersion": 2,
        "mediaType": VERBOSE_MANIFEST_LIST_MEDIA_TYPE,
        "manifests": manifests,
    }
