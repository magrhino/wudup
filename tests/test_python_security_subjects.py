from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from wudup import security_subjects as security_subjects_module
from wudup.digest_verifier import DigestResolveResult
from wudup.platforms import ImagePlatform
from wudup.security_subjects import (
    PendingSecurityOptions,
    PendingSecurityRequest,
    _request_for_target,
    _resolve_missing_reported_digests,
    current_security_request,
    pending_security_context,
)
from wudup.web_pending_sources import PendingSourceResult
from wudup.wud_file import parse_wud_text


class SecuritySubjectTests(unittest.TestCase):
    def setUp(self) -> None:
        security_subjects_module._missing_digest_failure_cache.clear()

    def test_request_prefers_compose_platform_and_reports_conflict(self) -> None:
        target = parse_wud_text(
            "repo/app:1.0 tag=2.0 platform=linux/amd64 "
            "sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        ).targets[0]

        request = _request_for_target(
            target,
            compose_platform=ImagePlatform("linux", "arm64"),
            wud_platform=target.platform,
        )

        self.assertEqual(request.identity_status, "mismatch")
        self.assertEqual(request.error, "Compose platform conflicts with WUD platform")
        self.assertEqual(request.candidate_image, "repo/app:2.0")
        self.assertEqual(request.platform, ImagePlatform("linux", "arm64"))
        self.assertEqual(request.platform_source, "compose")

    def test_request_rejects_ambiguous_compose_platforms(self) -> None:
        target = parse_wud_text(
            "repo/app:1.0 platform=linux/amd64 "
            "sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        ).targets[0]

        request = _request_for_target(
            target,
            compose_platform=None,
            compose_platform_conflict=True,
            wud_platform=target.platform,
        )

        self.assertEqual(request.identity_status, "mismatch")
        self.assertEqual(request.error, "Multiple Compose platforms matched WUD line")
        self.assertEqual(request.platform, ImagePlatform("linux", "amd64"))
        self.assertEqual(request.platform_source, "wud")

    def test_missing_digest_keeps_platform_mismatch_unresolvable(self) -> None:
        target = parse_wud_text("repo/app:1.0 platform=linux/amd64\n").targets[0]

        requests = (
            _request_for_target(
                target,
                compose_platform=ImagePlatform("linux", "arm64"),
                wud_platform=target.platform,
            ),
            _request_for_target(
                target,
                compose_platform=None,
                compose_platform_conflict=True,
                wud_platform=target.platform,
            ),
        )

        with mock.patch(
            "wudup.security_subjects.default_digest_verifier",
        ) as verifier_factory:
            resolved = _resolve_missing_reported_digests(_settings(), requests)

        verifier_factory.assert_not_called()
        self.assertEqual(
            [request.identity_status for request in resolved],
            ["mismatch", "mismatch"],
        )
        self.assertEqual(
            [request.error for request in resolved],
            [
                "Compose platform conflicts with WUD platform",
                "Multiple Compose platforms matched WUD line",
            ],
        )
        self.assertFalse(
            any(request.missing_reported_digest_resolvable for request in resolved)
        )

    def test_request_requires_reported_digest_and_platform(self) -> None:
        no_digest = _request_for_target(
            parse_wud_text("repo/app:1.0 platform=linux/amd64\n").targets[0],
            compose_platform=None,
            wud_platform=ImagePlatform("linux", "amd64"),
        )
        no_platform = _request_for_target(
            parse_wud_text(
                "repo/app:1.0 "
                "sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            ).targets[0],
            compose_platform=None,
            wud_platform=None,
        )

        self.assertEqual(no_digest.identity_status, "unsupported")
        self.assertEqual(no_digest.error, "reported digest is required")
        self.assertTrue(no_digest.missing_reported_digest_resolvable)
        self.assertEqual(no_platform.identity_status, "unsupported")
        self.assertEqual(no_platform.error, "platform is required")
        self.assertFalse(no_platform.missing_reported_digest_resolvable)

    def test_request_key_includes_platform(self) -> None:
        request = PendingSecurityRequest(
            line_no=1,
            raw=(
                "repo/app:1.0 tag=2.0 "
                "sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
            image="repo/app:1.0",
            candidate_image="repo/app:2.0",
            reported_digest=(
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
            platform=ImagePlatform("linux", "amd64"),
            platform_source="wud",
        )

        self.assertNotEqual(
            request.request_key,
            replace(request, platform=ImagePlatform("linux", "arm64")).request_key,
        )
        self.assertNotEqual(
            request.request_key,
            replace(request, platform=None, platform_source="").request_key,
        )

    def test_request_records_wud_current_digest_for_comparison(self) -> None:
        current_digest = f"sha256:{'c' * 64}"
        target = parse_wud_text(
            "repo/app:1.0 tag=2.0 platform=linux/amd64 "
            "sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        ).targets[0]

        request = _request_for_target(
            target,
            compose_platform=None,
            wud_platform=target.platform,
            current_image="repo/app:1.0",
            current_digest=current_digest,
        )
        current = current_security_request(request)

        self.assertEqual(request.current_image, "repo/app:1.0")
        self.assertEqual(request.current_digest, current_digest)
        self.assertEqual(request.current_digest_source, "wud")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.candidate_image, "repo/app:1.0")
        self.assertEqual(current.reported_digest, current_digest)
        self.assertIn("current_sha256=", current.raw)
        self.assertNotEqual(current.request_key, request.request_key)

    def test_context_resolves_missing_digest_once_per_candidate(self) -> None:
        digest = f"sha256:{'b' * 64}"
        verifier = mock.Mock()
        verifier.resolve_tag_digest.return_value = (
            DigestResolveResult(
                ok=True,
                status="resolved",
                reason="tag-digest-resolved",
                digest=digest,
            )
        )
        source = _pending_source(
            "repo/app:1.0 tag=2.0 platform=linux/amd64\n"
            "repo/app:1.0 tag=2.0 platform=linux/amd64\n"
        )

        with mock.patch(
            "wudup.security_subjects.web_pending_sources.resolve_pending_source",
            return_value=source,
        ), mock.patch(
            "wudup.security_subjects.default_digest_verifier",
            return_value=verifier,
        ):
            context = pending_security_context(
                _settings(),
                options=PendingSecurityOptions(include_compose=False),
            )

        verifier.resolve_tag_digest.assert_called_once_with("repo/app:2.0")
        self.assertEqual(
            [request.reported_digest for request in context.requests],
            [digest, digest],
        )
        self.assertEqual(
            [request.identity_status for request in context.requests],
            ["pending", "pending"],
        )
        self.assertEqual([request.error for request in context.requests], ["", ""])

    def test_context_keeps_missing_digest_unsupported_when_lookup_fails(self) -> None:
        verifier = mock.Mock()
        verifier.resolve_tag_digest.return_value = (
            DigestResolveResult(
                ok=False,
                status="failed",
                reason="manifest-unavailable",
                error="registry auth failed",
            )
        )
        source = _pending_source("repo/app:1.0 tag=2.0 platform=linux/amd64\n")

        with mock.patch(
            "wudup.security_subjects.web_pending_sources.resolve_pending_source",
            return_value=source,
        ), mock.patch(
            "wudup.security_subjects.default_digest_verifier",
            return_value=verifier,
        ):
            context = pending_security_context(
                _settings(),
                options=PendingSecurityOptions(include_compose=False),
            )

        request = context.requests[0]
        verifier.resolve_tag_digest.assert_called_once_with("repo/app:2.0")
        self.assertEqual(request.identity_status, "unsupported")
        self.assertEqual(request.error, "reported digest is required")
        self.assertEqual(request.reported_digest, "")
        self.assertEqual(
            request.warnings,
            (
                (
                    "Could not resolve reported digest for repo/app:2.0: "
                    "registry auth failed"
                ),
            ),
        )

    def test_context_backs_off_failed_missing_digest_resolution(self) -> None:
        verifier = mock.Mock()
        verifier.resolve_tag_digest.return_value = DigestResolveResult(
            ok=False,
            status="failed",
            reason="manifest-unavailable",
            error="registry auth failed",
        )
        source = _pending_source("repo/app:1.0 tag=2.0 platform=linux/amd64\n")

        with mock.patch(
            "wudup.security_subjects.web_pending_sources.resolve_pending_source",
            return_value=source,
        ), mock.patch(
            "wudup.security_subjects.default_digest_verifier",
            return_value=verifier,
        ) as verifier_factory:
            with mock.patch("wudup.security_subjects.time.monotonic", return_value=100.0):
                first = pending_security_context(
                    _settings(),
                    options=PendingSecurityOptions(include_compose=False),
                )
                second = pending_security_context(
                    _settings(),
                    options=PendingSecurityOptions(include_compose=False),
                )
            with mock.patch(
                "wudup.security_subjects.time.monotonic",
                return_value=(
                    100.0
                    + security_subjects_module._MISSING_DIGEST_FAILURE_CACHE_TTL_SECONDS
                    + 0.1
                ),
            ):
                pending_security_context(
                    _settings(),
                    options=PendingSecurityOptions(include_compose=False),
                )

        self.assertEqual(verifier.resolve_tag_digest.call_count, 2)
        self.assertEqual(verifier_factory.call_count, 2)
        self.assertEqual(first.requests[0].warnings, second.requests[0].warnings)

    def test_missing_digest_resolution_uses_request_flag_not_messages(self) -> None:
        digest = f"sha256:{'b' * 64}"
        verifier = mock.Mock()
        verifier.resolve_tag_digest.return_value = DigestResolveResult(
            ok=True,
            status="resolved",
            reason="tag-digest-resolved",
            digest=digest,
        )
        request = PendingSecurityRequest(
            line_no=1,
            raw="repo/app:1.0 tag=2.0 platform=linux/amd64",
            image="repo/app:1.0",
            candidate_image="repo/app:2.0",
            reported_digest="",
            platform=ImagePlatform("linux", "amd64"),
            platform_source="wud",
            missing_reported_digest_resolvable=True,
            identity_status="waiting",
            error="digest missing",
        )

        with mock.patch(
            "wudup.security_subjects.default_digest_verifier",
            return_value=verifier,
        ):
            resolved = _resolve_missing_reported_digests(_settings(), (request,))

        self.assertEqual(resolved[0].reported_digest, digest)
        self.assertFalse(resolved[0].missing_reported_digest_resolvable)
        self.assertEqual(resolved[0].identity_status, "pending")
        self.assertEqual(resolved[0].error, "")


def _settings():
    return SimpleNamespace(security_scan=SimpleNamespace(enabled=True))


def _pending_source(text: str) -> PendingSourceResult:
    return PendingSourceResult(
        configured="api",
        active="api",
        label="WUD API",
        source_file="WUD API",
        exists=True,
        parsed=parse_wud_text(text),
        text=text,
        source_hash="test",
    )


if __name__ == "__main__":
    unittest.main()
