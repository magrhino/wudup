from __future__ import annotations

import hashlib
import json
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from unittest import mock

from tests.test_python_digest_verifier import FakeDocker, StaticResolver, index_doc
from tests.update_from_wud_helpers import (
    UpdateFromWudRunnerTestCase,
    manifest_image,
    manifest_index,
)

from wudup.digest_verifier import (
    DigestCheckResult,
    DigestResolveResult,
    DigestVerifier,
    ManifestIntegrityError,
    RegistryHttpManifestResolver,
    parse_registry_image,
)
from wudup.platforms import ImagePlatform
from wudup.updater_digest_pin import digest_pin_update_from_values


def body_digest(body: bytes, algorithm: str = "sha256") -> str:
    return f"{algorithm}:{hashlib.new(algorithm, body).hexdigest()}"


def manifest_body(config: str = "sha256:local") -> bytes:
    # Whitespace is deliberately significant to the digest.
    return json.dumps(manifest_image(config), indent=2).encode() + b"\n"


def response(body: bytes, digest: str = "") -> mock.MagicMock:
    result = mock.MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = body
    result.headers = {}
    if digest:
        result.headers["Docker-Content-Digest"] = digest
    return result


class ManifestIntegrityTests(unittest.TestCase):
    image = "quay.io/acme/app:latest"
    expected = "sha256:" + "a" * 64

    def setUp(self) -> None:
        self.fallback = mock.Mock()
        self.verifier = DigestVerifier(
            FakeDocker(image_id="sha256:local"),
            fallback_resolver=self.fallback,
        )

    def assert_integrity_failure(self, result: object) -> None:
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "manifest-integrity-mismatch")
        self.fallback.fetch.assert_not_called()

    def test_forged_header_cannot_verify_wrong_local_image(self) -> None:
        for registry in ("quay.io", "ghcr.io", "docker.io"):
            with (
                self.subTest(registry=registry),
                mock.patch(
                    "wudup.digest_verifier.urllib.request.urlopen",
                    return_value=response(manifest_body(), self.expected),
                ),
            ):
                self.assert_integrity_failure(
                    self.verifier.verify(f"{registry}/acme/app:latest", self.expected)
                )

    def test_requested_digest_is_checked_even_with_missing_or_valid_other_header(
        self,
    ) -> None:
        body = manifest_body()
        image = parse_registry_image(self.image)
        for header in ("", self.expected, body_digest(body)):
            with (
                self.subTest(header=header),
                mock.patch(
                    "wudup.digest_verifier.urllib.request.urlopen",
                    return_value=response(body, header),
                ),
            ):
                with self.assertRaises(ManifestIntegrityError):
                    RegistryHttpManifestResolver().fetch(image, self.expected)

    def test_invalid_json_does_not_hide_proven_integrity_failure(self) -> None:
        with mock.patch(
            "wudup.digest_verifier.urllib.request.urlopen",
            return_value=response(b"not JSON", self.expected),
        ):
            self.assert_integrity_failure(
                self.verifier.verify(self.image, self.expected)
            )

    def test_malformed_and_unsupported_digest_headers_fail_closed(self) -> None:
        for header in ("sha256:bad", "SHA256:" + "a" * 64, "md5:" + "a" * 32):
            with (
                self.subTest(header=header),
                mock.patch(
                    "wudup.digest_verifier.urllib.request.urlopen",
                    return_value=response(manifest_body(), header),
                ),
            ):
                self.assert_integrity_failure(
                    self.verifier.verify(self.image, self.expected)
                )

    def test_exact_raw_bytes_are_retained_and_hashed_with_or_without_header(
        self,
    ) -> None:
        body = manifest_body()
        image = parse_registry_image(self.image)
        for algorithm in ("sha256", "sha512"):
            digest = body_digest(body, algorithm)
            for header in ("", digest):
                with (
                    self.subTest(algorithm=algorithm, header=header),
                    mock.patch(
                        "wudup.digest_verifier.urllib.request.urlopen",
                        return_value=response(body, header),
                    ),
                ):
                    document = RegistryHttpManifestResolver().fetch(image, digest)
                    self.assertEqual(document.raw_body, body)
                    self.assertEqual(document.digest, digest)
                    self.assertEqual(document.payload, json.loads(body))
        with mock.patch(
            "wudup.digest_verifier.urllib.request.urlopen",
            return_value=response(body),
        ):
            result = self.verifier.verify(self.image, body_digest(body))
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "registry-tag-manifest-match")

    def test_auth_retry_preserves_requested_digest_validation(self) -> None:
        unauthorized = urllib.error.HTTPError(
            "https://quay.io/v2/acme/app/manifests/digest",
            401,
            "unauthorized",
            {"WWW-Authenticate": 'Bearer realm="https://quay.io/token"'},
            BytesIO(),
        )
        with mock.patch(
            "wudup.digest_verifier.urllib.request.urlopen",
            side_effect=[
                unauthorized,
                response(b'{"token":"test"}'),
                response(manifest_body()),
            ],
        ):
            with self.assertRaises(ManifestIntegrityError):
                RegistryHttpManifestResolver().fetch(
                    parse_registry_image(self.image), self.expected
                )

    def test_different_valid_header_algorithm_preserves_requested_identity(
        self,
    ) -> None:
        body = manifest_body()
        expected = body_digest(body)
        with mock.patch(
            "wudup.digest_verifier.urllib.request.urlopen",
            return_value=response(body, body_digest(body, "sha512")),
        ):
            document = RegistryHttpManifestResolver().fetch(
                parse_registry_image(self.image), expected
            )
            self.assertEqual(document.digest, expected)
            self.assertTrue(self.verifier.verify(self.image, expected).ok)

    def test_valid_index_and_platform_child_verify(self) -> None:
        child = manifest_body()
        child_digest = body_digest(child)
        index = json.dumps(manifest_index(child_digest)).encode()
        for expected in (body_digest(index), child_digest):
            with (
                self.subTest(expected=expected),
                mock.patch(
                    "wudup.digest_verifier.urllib.request.urlopen",
                    side_effect=[response(index), response(child)],
                ),
            ):
                result = self.verifier.verify(self.image, expected)
                self.assertTrue(result.ok)
                self.assertEqual(result.matched_child_digest, child_digest)

    def test_corrupt_index_child_cannot_be_skipped_for_a_later_matching_child(
        self,
    ) -> None:
        child = manifest_body()
        index = json.dumps(manifest_index(self.expected, body_digest(child))).encode()
        for expected in (body_digest(index), self.expected):
            with (
                self.subTest(expected=expected),
                mock.patch(
                    "wudup.digest_verifier.urllib.request.urlopen",
                    side_effect=[response(index), response(child), response(child)],
                ) as fetch,
            ):
                self.assert_integrity_failure(
                    self.verifier.verify(self.image, expected)
                )
                self.assertEqual(fetch.call_count, 2)

    def test_resolution_and_subject_entrypoints_fail_on_forged_manifest(self) -> None:
        platform = ImagePlatform(os="linux", architecture="amd64")
        with mock.patch(
            "wudup.digest_verifier.urllib.request.urlopen",
            return_value=response(manifest_body(), self.expected),
        ):
            self.assert_integrity_failure(self.verifier.resolve_tag_digest(self.image))
            self.assert_integrity_failure(
                self.verifier.verify_tag_digest(self.image, self.expected)
            )
            for resolve in (
                self.verifier.resolve_subject,
                self.verifier.resolve_digest_subject,
            ):
                subject = resolve(self.image, self.expected, platform)
                self.assertEqual(subject.identity_status, "mismatch")
                self.assertFalse(subject.manifest_digest)
        self.fallback.fetch.assert_not_called()

    def test_index_child_must_be_a_digest_not_a_mutable_tag(self) -> None:
        for child_digest in ("mutable-child", "sha256:bad", "", None):
            index = json.dumps(manifest_index(child_digest)).encode()
            expected = body_digest(index)
            with (
                self.subTest(child_digest=child_digest),
                mock.patch(
                    "wudup.digest_verifier.urllib.request.urlopen",
                    side_effect=[response(index), response(manifest_body())],
                ) as fetch,
            ):
                self.assert_integrity_failure(
                    self.verifier.verify(self.image, expected)
                )
                self.assertEqual(fetch.call_count, 1)
            with (
                self.subTest(child_digest=child_digest),
                mock.patch(
                    "wudup.digest_verifier.urllib.request.urlopen",
                    return_value=response(index),
                ),
            ):
                subject = self.verifier.resolve_subject(
                    self.image,
                    expected,
                    ImagePlatform(os="linux", architecture="amd64"),
                )
                self.assertEqual(subject.identity_status, "mismatch")

    def test_index_digest_refresh_does_not_swallow_integrity_failure(self) -> None:
        resolver = StaticResolver(
            {("quay.io", "acme/app", "latest"): index_doc("", (self.expected,))}
        )
        verifier = DigestVerifier(FakeDocker(), primary_resolver=resolver)
        with mock.patch(
            "wudup.digest_verifier.urllib.request.urlopen",
            return_value=response(manifest_body(), self.expected),
        ):
            self.assert_integrity_failure(verifier.resolve_tag_digest(self.image))
            self.assert_integrity_failure(
                verifier.verify_tag_digest(self.image, self.expected)
            )
            subject = verifier.resolve_subject(
                self.image,
                self.expected,
                ImagePlatform(os="linux", architecture="amd64"),
            )
            self.assertEqual(subject.identity_status, "mismatch")

    def test_direct_repo_digest_match_needs_no_registry(self) -> None:
        verifier = DigestVerifier(
            FakeDocker(repo_digests=(f"quay.io/acme/app@{self.expected}",))
        )
        with mock.patch("wudup.digest_verifier.urllib.request.urlopen") as fetch:
            result = verifier.verify(self.image, self.expected)
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "repo-digest-match")
        fetch.assert_not_called()


class UpdaterManifestIntegrityTests(UpdateFromWudRunnerTestCase):
    def test_mixed_preflight_failures_keep_per_entry_audit_reasons(self) -> None:
        corrupt_line = "repo/corrupt:latest@sha256:expected\n"
        self.wud_file.write_text(
            "repo/stale:latest@sha256:old\n" + corrupt_line,
            encoding="utf-8",
        )
        self.make_stack(
            "app",
            [
                ("stale", "repo/stale:latest", "cid-stale"),
                ("corrupt", "repo/corrupt:latest", "cid-corrupt"),
            ],
        )
        for name in ("stale", "corrupt"):
            self.set_image_state(
                f"repo/{name}:latest", "sha256:local", "sha256:local-manifest"
            )
        verifier = mock.Mock()
        verifier.verify_tag_digest.side_effect = lambda image, _expected: (
            DigestResolveResult(False, "failed", "stale-digest", digest="sha256:moved")
            if image == "repo/stale:latest"
            else DigestResolveResult(False, "failed", "manifest-integrity-mismatch")
        )
        runner = self.make_runner(digest_verifier=verifier, db_path=self.db_path)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            status = runner.run()

        reasons = {
            "stale": "stale-pending-digest",
            "corrupt": "manifest-integrity-mismatch",
        }
        self.assertEqual(status, 1)
        self.assertNotRegex(self.calls(), r"compose .* (pull|up)\b")
        self.assertEqual(self.wud_file.read_text(encoding="utf-8"), corrupt_line)
        events = self.db_rows("SELECT service_name, metadata_json FROM update_events")
        self.assertEqual(
            {
                row["service_name"]: json.loads(row["metadata_json"])["reason"]
                for row in events
            },
            reasons,
        )
        pending = self.db_rows(
            "SELECT service_name, status_reason FROM pending_updates"
        )
        self.assertEqual(
            {row["service_name"]: row["status_reason"] for row in pending}, reasons
        )
        self.assertEqual(
            {failure.services: failure.reason for failure in runner.failures},
            {(name,): reason for name, reason in reasons.items()},
        )

    def test_digest_pin_integrity_failure_is_not_overwritten_by_an_image_alias(
        self,
    ) -> None:
        verifier = mock.Mock()
        failed = DigestCheckResult(False, "failed", "manifest-integrity-mismatch")
        verifier.verify.side_effect = [
            failed,
            DigestCheckResult(True, "verified", "repo-digest-match"),
        ]
        runner = self.make_runner(digest_verifier=verifier)
        update = digest_pin_update_from_values(
            old_image="acme/app:latest",
            resolved_tag="latest",
            planned_digest="sha256:" + "a" * 64,
            services=("app",),
        )
        result, matched = runner.lifecycle._verify_digest_pin_images(
            update,
            ("acme/app:latest", "docker.io/acme/app:latest"),
        )
        self.assertTrue(matched)
        self.assertEqual(result, failed)
        verifier.verify.assert_called_once()

    def prepare_update(self) -> tuple[str, str, bytes]:
        image = "quay.io/acme/app:latest"
        body = manifest_body()
        expected = body_digest(body)
        self.wud_file.write_text(f"{image}@{expected}\n", encoding="utf-8")
        self.make_stack("app", [("app", image, "cid-app")])
        self.set_image_state(image, "sha256:old", "sha256:old-manifest")
        self.set_image_after_pull(image, "sha256:wrong", "sha256:wrong-manifest")
        return image, expected, body

    def test_integrity_failure_stops_preflight_before_pull(self) -> None:
        _image, expected, _body = self.prepare_update()
        runner = self.make_runner(db_path=self.db_path)
        runner.digest_verifier = DigestVerifier(runner.docker)
        with (
            mock.patch(
                "wudup.digest_verifier.urllib.request.urlopen",
                return_value=response(manifest_body("sha256:wrong"), expected),
            ),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            status = runner.run()
        self.assertEqual(status, 1)
        self.assertNotRegex(self.calls(), r"compose .* (pull|up)\b")
        self.assertIn(expected, self.wud_file.read_text())
        events = self.db_rows("SELECT status, metadata_json FROM update_events")
        self.assertEqual(events[0]["status"], "failure")
        self.assertEqual(
            json.loads(events[0]["metadata_json"])["reason"],
            "manifest-integrity-mismatch",
        )

    def test_postpull_integrity_failure_prevents_recreation_and_cleanup(self) -> None:
        _image, expected, body = self.prepare_update()
        runner = self.make_runner(db_path=self.db_path)
        runner.digest_verifier = DigestVerifier(runner.docker)
        with (
            mock.patch(
                "wudup.digest_verifier.urllib.request.urlopen",
                side_effect=[
                    response(body),
                    response(manifest_body("sha256:wrong"), expected),
                ],
            ),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            status = runner.run()
        self.assertEqual(status, 1)
        self.assertRegex(self.calls(), r"compose .* pull\b")
        self.assertNotRegex(self.calls(), r"compose .* up\b")
        self.assertIn(expected, self.wud_file.read_text())
        events = self.db_rows("SELECT status, metadata_json FROM update_events")
        self.assertEqual(events[0]["status"], "failure")
        self.assertEqual(
            json.loads(events[0]["metadata_json"])["reason"],
            "expected-digest-not-reached",
        )
