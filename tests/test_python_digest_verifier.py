from __future__ import annotations

import unittest
from collections.abc import Mapping
from unittest import mock

from tests.update_from_wud_helpers import (
    MANIFEST_INDEX_TYPE,
    FakeDockerTestCase,
    manifest_index,
    manifest_index_digest,
    verbose_manifest_item,
)

from wudup.command import CommandRunner
from wudup.digest_verifier import (
    DigestVerifier,
    DockerManifestResolver,
    ManifestDocument,
    ManifestLookupError,
    RegistryHttpManifestResolver,
    _payload_digest,
    _same_local_platform,
    parse_registry_image,
)
from wudup.docker_cli import DockerCli
from wudup.platforms import ImagePlatform, parse_platform
from wudup.updater_digest_pin import _digest_pin_resolve_error

INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"


class FakeDocker:
    def __init__(
        self,
        *,
        image_id: str = "sha256:config",
        repo_digests: tuple[str, ...] = (),
        platform: str = "linux/amd64",
    ) -> None:
        self.image_id_value = image_id
        self.repo_digest_values = repo_digests
        self.platform_value = platform
        self.platform_calls: list[str] = []

    def image_repo_digests(self, image: str) -> list[str]:
        return list(self.repo_digest_values)

    def image_id(self, image: str) -> str:
        return self.image_id_value

    def image_platform(self, image: str) -> ImagePlatform | None:
        self.platform_calls.append(image)
        return parse_platform(self.platform_value) if self.platform_value else None


class StaticResolver:
    def __init__(
        self,
        documents: Mapping[tuple[str, str, str], ManifestDocument],
    ) -> None:
        self.documents = dict(documents)
        self.calls: list[tuple[str, str, str]] = []

    def fetch(self, image: object, reference: str) -> ManifestDocument:
        registry = image.registry
        repo = image.repo
        self.calls.append((registry, repo, reference))
        try:
            return self.documents[(registry, repo, reference)]
        except KeyError as exc:
            raise ManifestLookupError(f"missing {registry}/{repo}@{reference}") from exc


class FailingResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def fetch(self, image: object, reference: str) -> ManifestDocument:
        registry = image.registry
        repo = image.repo
        self.calls.append((registry, repo, reference))
        raise ManifestLookupError("primary unavailable")


class SequencedHttpResolver(RegistryHttpManifestResolver):
    def __init__(self, documents: tuple[ManifestDocument, ...]) -> None:
        self.documents = list(documents)
        self.calls: list[tuple[str, str, str]] = []

    def fetch(self, image: object, reference: str) -> ManifestDocument:
        registry = image.registry
        repo = image.repo
        self.calls.append((registry, repo, reference))
        if not self.documents:
            raise ManifestLookupError("no more documents")
        return self.documents.pop(0)


def index_doc(
    digest: str,
    children: tuple[str, ...],
    *,
    source: str = "static",
) -> ManifestDocument:
    return ManifestDocument(
        source=source,
        digest=digest,
        media_type=INDEX_TYPE,
        payload={
            "schemaVersion": 2,
            "mediaType": INDEX_TYPE,
            "manifests": [
                {
                    "mediaType": MANIFEST_TYPE,
                    "digest": child,
                    "platform": {"os": "linux", "architecture": "amd64"},
                }
                for child in children
            ],
        },
    )


def variant_index_doc(
    digest: str,
    children: tuple[tuple[str, str, str, str], ...],
    *,
    source: str = "static",
) -> ManifestDocument:
    return ManifestDocument(
        source=source,
        digest=digest,
        media_type=INDEX_TYPE,
        payload={
            "schemaVersion": 2,
            "mediaType": INDEX_TYPE,
            "manifests": [
                {
                    "mediaType": MANIFEST_TYPE,
                    "digest": child,
                    "platform": {
                        "os": os_value,
                        "architecture": architecture,
                        "variant": variant,
                    },
                }
                for child, os_value, architecture, variant in children
            ],
        },
    )


def unknown_platform_index_doc(
    digest: str,
    child: str,
    *,
    source: str = "static",
) -> ManifestDocument:
    return ManifestDocument(
        source=source,
        digest=digest,
        media_type=INDEX_TYPE,
        payload={
            "schemaVersion": 2,
            "mediaType": INDEX_TYPE,
            "manifests": [
                {
                    "mediaType": MANIFEST_TYPE,
                    "digest": child,
                    "platform": {"os": "unknown", "architecture": "unknown"},
                },
            ],
        },
    )


def manifest_doc(
    digest: str,
    config_digest: str,
    *,
    source: str = "static",
) -> ManifestDocument:
    return ManifestDocument(
        source=source,
        digest=digest,
        media_type=MANIFEST_TYPE,
        payload={
            "schemaVersion": 2,
            "mediaType": MANIFEST_TYPE,
            "config": {"digest": config_digest},
        },
    )


class DigestVerifierTests(unittest.TestCase):
    def test_parse_registry_image_supports_docker_hub_and_explicit_registries(self) -> None:
        alpine = parse_registry_image("alpine:3.20")
        self.assertIsNotNone(alpine)
        assert alpine is not None
        self.assertEqual(alpine.registry, "docker.io")
        self.assertEqual(alpine.http_registry, "registry-1.docker.io")
        self.assertEqual(alpine.repo, "library/alpine")
        self.assertEqual(alpine.tag, "3.20")

        docker = parse_registry_image("docker.io/library/alpine:3.20")
        self.assertIsNotNone(docker)
        assert docker is not None
        self.assertEqual(docker.registry, "docker.io")
        self.assertEqual(docker.http_registry, "registry-1.docker.io")
        self.assertEqual(docker.repo, "library/alpine")
        self.assertEqual(docker.tag, "3.20")

        quay = parse_registry_image("quay.io/prometheus/busybox:latest")
        self.assertIsNotNone(quay)
        assert quay is not None
        self.assertEqual(quay.registry, "quay.io")
        self.assertEqual(quay.http_registry, "quay.io")
        self.assertEqual(quay.repo, "prometheus/busybox")
        self.assertEqual(quay.tag, "latest")

    def test_parse_registry_image_identifies_tagged_ghcr_reference(self) -> None:
        parsed = parse_registry_image("ghcr.io/acme/app:latest@sha256:old")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed.is_ghcr())
        self.assertEqual(parsed.repo, "acme/app")
        self.assertEqual(parsed.tag, "latest")
        hub = parse_registry_image("acme/app:latest")
        self.assertIsNotNone(hub)
        assert hub is not None
        self.assertFalse(hub.is_ghcr())
        self.assertIsNone(parse_registry_image("ghcr.io/acme/app"))

    def test_platform_child_digest_matches_local_config_digest(self) -> None:
        expected = "sha256:child"
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc("sha256:index", (expected,)),
                ("ghcr.io", "acme/app", expected): manifest_doc(expected, "sha256:config"),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:config"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("ghcr.io/acme/app:latest", expected)

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "ghcr-platform-manifest-match")
        self.assertEqual(result.matched_child_digest, expected)
        self.assertEqual(result.expected_config_digest, "sha256:config")

    def test_current_index_digest_matches_local_platform_child_config(self) -> None:
        expected = "sha256:index"
        child = "sha256:child"
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc(expected, (child,)),
                ("ghcr.io", "acme/app", child): manifest_doc(child, "sha256:config"),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:config"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("ghcr.io/acme/app:latest", expected)

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "ghcr-index-manifest-match")
        self.assertEqual(result.tag_digest, expected)
        self.assertEqual(result.matched_child_digest, child)

    def test_stale_digest_fails_when_absent_from_current_index(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc(
                    "sha256:index",
                    ("sha256:child",),
                ),
                ("ghcr.io", "acme/app", "sha256:stale"): manifest_doc(
                    "sha256:stale",
                    "sha256:config",
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:config"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("ghcr.io/acme/app:latest", "sha256:stale")

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "stale-digest")
        self.assertEqual(result.tag_digest, "sha256:index")

    def test_platform_child_digest_fails_when_local_image_id_differs(self) -> None:
        expected = "sha256:child"
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc("sha256:index", (expected,)),
                ("ghcr.io", "acme/app", expected): manifest_doc(expected, "sha256:config"),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:other"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("ghcr.io/acme/app:latest", expected)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "platform-mismatch")
        self.assertEqual(result.expected_config_digest, "sha256:config")
        self.assertEqual(result.local_image_id, "sha256:other")

    def test_primary_failure_uses_fallback_manifest_data(self) -> None:
        expected = "sha256:child"
        primary = FailingResolver()
        fallback = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc(
                    "sha256:index",
                    (expected,),
                    source="fallback",
                ),
                ("ghcr.io", "acme/app", expected): manifest_doc(
                    expected,
                    "sha256:config",
                    source="fallback",
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:config"),
            primary_resolver=primary,
            fallback_resolver=fallback,
        )

        result = verifier.verify("ghcr.io/acme/app:latest", expected)

        self.assertTrue(result.ok)
        self.assertEqual(result.source, "fallback")
        self.assertGreaterEqual(len(primary.calls), 1)
        self.assertIn(("ghcr.io", "acme/app", "latest"), fallback.calls)

    def test_resolve_tag_digest_reuses_primary_http_resolver_for_index_digest(self) -> None:
        resolver = SequencedHttpResolver(
            (
                index_doc("", ("sha256:child",)),
                index_doc("sha256:index", ("sha256:child",)),
            )
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=FailingResolver(),
        )

        with mock.patch(
            "wudup.digest_verifier.RegistryHttpManifestResolver.fetch",
            side_effect=AssertionError("fresh HTTP resolver used"),
        ):
            result = verifier.resolve_tag_digest("ghcr.io/acme/app:latest")

        self.assertTrue(result.ok)
        self.assertEqual(result.digest, "sha256:index")
        self.assertEqual(
            resolver.calls,
            [
                ("ghcr.io", "acme/app", "latest"),
                ("ghcr.io", "acme/app", "latest"),
            ],
        )

    def test_verify_tag_digest_accepts_current_tag_digest(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc(
                    "sha256:index",
                    ("sha256:child",),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify_tag_digest("ghcr.io/acme/app:latest", "sha256:index")

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "tag-digest-current")
        self.assertEqual(result.digest, "sha256:index")

    def test_verify_tag_digest_accepts_current_platform_child_digest(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc(
                    "sha256:index",
                    ("sha256:child",),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify_tag_digest("ghcr.io/acme/app:latest", "sha256:child")

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "tag-child-digest-current")
        self.assertEqual(result.digest, "sha256:child")

    def test_verify_tag_digest_rejects_digest_missing_from_current_index(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc(
                    "sha256:index",
                    ("sha256:child",),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify_tag_digest("ghcr.io/acme/app:latest", "sha256:stale")

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "stale-digest")
        self.assertEqual(result.digest, "sha256:index")

    def test_verify_tag_digest_rejects_unknown_platform_child_digest(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): unknown_platform_index_doc(
                    "sha256:index",
                    "sha256:attestation",
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify_tag_digest(
            "ghcr.io/acme/app:latest",
            "sha256:attestation",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "stale-digest")
        self.assertEqual(result.digest, "sha256:index")

    def test_non_ghcr_platform_child_digest_matches_local_config_digest(self) -> None:
        expected = "sha256:child"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): index_doc("sha256:index", (expected,)),
                ("quay.io", "acme/app", expected): manifest_doc(expected, "sha256:config"),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:config"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.reason, "registry-platform-manifest-match")
        self.assertEqual(result.matched_child_digest, expected)
        self.assertEqual(result.expected_config_digest, "sha256:config")

    def test_non_ghcr_current_index_digest_matches_local_platform_child(self) -> None:
        expected = "sha256:index"
        child = "sha256:child"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): index_doc(expected, (child,)),
                ("quay.io", "acme/app", child): manifest_doc(child, "sha256:config"),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:config"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.reason, "registry-index-manifest-match")
        self.assertEqual(result.tag_digest, expected)
        self.assertEqual(result.matched_child_digest, child)

    def test_containerd_index_image_id_matches_platform_child_digest(self) -> None:
        # On Docker's containerd image store the local image ID is the pulled
        # index digest, not the platform image's config digest. The index
        # descriptor's platform must match the local image's platform; the
        # child manifest itself does not need to be fetched.
        expected = "sha256:child"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): index_doc("sha256:index", (expected,)),
            }
        )
        docker = FakeDocker(
            image_id="sha256:index",
            repo_digests=("quay.io/acme/app@sha256:index",),
            platform="linux/amd64",
        )
        verifier = DigestVerifier(
            docker,
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "registry-platform-manifest-match")
        self.assertEqual(result.matched_child_digest, expected)
        self.assertEqual(result.local_image_id, "sha256:index")
        self.assertEqual(resolver.calls, [("quay.io", "acme/app", "latest")])
        self.assertEqual(docker.platform_calls, ["quay.io/acme/app:latest"])

    def test_containerd_index_image_id_rejects_other_platform_child(self) -> None:
        expected = "sha256:amd64"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): variant_index_doc(
                    "sha256:index",
                    (
                        (expected, "linux", "amd64", ""),
                        ("sha256:arm64", "linux", "arm64", "v8"),
                    ),
                ),
                ("quay.io", "acme/app", expected): manifest_doc(expected, "sha256:config"),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:index", platform="linux/arm64/v8"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "platform-mismatch")
        self.assertEqual(result.local_image_id, "sha256:index")

    def test_containerd_index_image_id_fails_without_local_platform(self) -> None:
        expected = "sha256:child"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): index_doc("sha256:index", (expected,)),
                ("quay.io", "acme/app", expected): manifest_doc(expected, "sha256:config"),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:index", platform=""),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "platform-mismatch")

    def test_containerd_index_image_id_ignores_child_without_real_platform(self) -> None:
        expected = "sha256:attestation"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): unknown_platform_index_doc(
                    "sha256:index",
                    expected,
                ),
                ("quay.io", "acme/app", expected): manifest_doc(expected, "sha256:empty"),
            }
        )
        docker = FakeDocker(image_id="sha256:index")
        verifier = DigestVerifier(
            docker,
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "platform-mismatch")
        self.assertEqual(docker.platform_calls, [])

    def test_current_index_digest_fails_when_no_child_config_matches(self) -> None:
        expected = "sha256:index"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): index_doc(
                    expected,
                    ("sha256:child", "sha256:missing"),
                ),
                ("quay.io", "acme/app", "sha256:child"): manifest_doc(
                    "sha256:child",
                    "sha256:config",
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:old-index"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "platform-mismatch")
        self.assertEqual(result.local_image_id, "sha256:old-index")

    def test_same_local_platform_keeps_other_variants_strict(self) -> None:
        def platform(value: str) -> ImagePlatform:
            parsed = parse_platform(value)
            assert parsed is not None
            return parsed

        self.assertTrue(_same_local_platform(platform("linux/arm/v7"), platform("linux/arm/v7")))
        self.assertFalse(_same_local_platform(platform("linux/arm/v6"), platform("linux/arm/v7")))
        self.assertFalse(_same_local_platform(platform("linux/arm"), platform("linux/arm/v7")))
        self.assertFalse(_same_local_platform(platform("windows/amd64"), platform("linux/amd64")))
        self.assertTrue(_same_local_platform(platform("linux/arm64/v8"), platform("linux/arm64")))

    def test_containerd_index_image_id_treats_arm64_v8_as_default_variant(self) -> None:
        expected = "sha256:arm64"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): variant_index_doc(
                    "sha256:index",
                    ((expected, "linux", "arm64", ""),),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:index", platform="linux/arm64/v8"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "registry-platform-manifest-match")

    def test_containerd_index_image_id_matches_current_index_digest(self) -> None:
        expected = "sha256:index"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): index_doc(expected, ("sha256:child",)),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id=expected),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "registry-index-manifest-match")
        self.assertEqual(result.tag_digest, expected)
        self.assertEqual(resolver.calls, [("quay.io", "acme/app", "latest")])

    def test_containerd_manifest_image_id_matches_single_manifest_tag(self) -> None:
        expected = "sha256:manifest"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): manifest_doc(expected, "sha256:config"),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id=expected),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "registry-tag-manifest-match")

    def test_containerd_image_id_from_other_index_still_fails(self) -> None:
        expected = "sha256:child"
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): index_doc("sha256:index", (expected,)),
                ("quay.io", "acme/app", expected): manifest_doc(expected, "sha256:config"),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:old-index"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", expected)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "platform-mismatch")
        self.assertEqual(result.local_image_id, "sha256:old-index")

    def test_non_ghcr_stale_digest_fails_when_registry_proves_current_index(self) -> None:
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): index_doc(
                    "sha256:index",
                    ("sha256:child",),
                ),
                ("quay.io", "acme/app", "sha256:stale"): manifest_doc(
                    "sha256:stale",
                    "sha256:config",
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(image_id="sha256:config"),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        result = verifier.verify("quay.io/acme/app:latest", "sha256:stale")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "stale-digest")
        self.assertEqual(result.tag_digest, "sha256:index")

    def test_resolve_subject_uses_current_index_child_for_platform(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc(
                    "sha256:index",
                    ("sha256:child",),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        subject = verifier.resolve_subject(
            "ghcr.io/acme/app:latest",
            "sha256:index",
            ImagePlatform("linux", "amd64"),
            platform_source="compose",
        )

        self.assertEqual(subject.identity_status, "exact")
        self.assertEqual(subject.canonical_registry, "ghcr.io")
        self.assertEqual(subject.canonical_repository, "acme/app")
        self.assertEqual(subject.index_digest, "sha256:index")
        self.assertEqual(subject.manifest_digest, "sha256:child")
        self.assertEqual(subject.immutable_ref, "ghcr.io/acme/app@sha256:child")
        self.assertEqual(subject.platform, "linux/amd64")
        self.assertEqual(subject.platform_source, "compose")

    def test_resolve_digest_subject_scans_historical_index_digest(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "sha256:old-index"): index_doc(
                    "sha256:old-index",
                    ("sha256:old-child",),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        subject = verifier.resolve_digest_subject(
            "ghcr.io/acme/app:latest",
            "sha256:old-index",
            ImagePlatform("linux", "amd64"),
            platform_source="wud",
        )

        self.assertEqual(subject.identity_status, "exact")
        self.assertEqual(subject.index_digest, "sha256:old-index")
        self.assertEqual(subject.manifest_digest, "sha256:old-child")
        self.assertEqual(subject.immutable_ref, "ghcr.io/acme/app@sha256:old-child")
        self.assertEqual(resolver.calls, [("ghcr.io", "acme/app", "sha256:old-index")])

    def test_resolve_digest_subject_accepts_historical_manifest_digest(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "sha256:old-child"): manifest_doc(
                    "sha256:old-child",
                    "sha256:config",
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        subject = verifier.resolve_digest_subject(
            "ghcr.io/acme/app:latest",
            "sha256:old-child",
            ImagePlatform("linux", "amd64"),
        )

        self.assertEqual(subject.identity_status, "exact")
        self.assertEqual(subject.manifest_digest, "sha256:old-child")
        self.assertEqual(subject.immutable_ref, "ghcr.io/acme/app@sha256:old-child")

    def test_resolve_subject_accepts_current_platform_child_digest(self) -> None:
        resolver = StaticResolver(
            {
                ("quay.io", "acme/app", "latest"): index_doc(
                    "sha256:index",
                    ("sha256:child",),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        subject = verifier.resolve_subject(
            "quay.io/acme/app:latest",
            "sha256:child",
            ImagePlatform("linux", "amd64"),
            platform_source="wud",
        )

        self.assertEqual(subject.identity_status, "exact")
        self.assertEqual(subject.canonical_registry, "quay.io")
        self.assertEqual(subject.index_digest, "sha256:index")
        self.assertEqual(subject.manifest_digest, "sha256:child")
        self.assertEqual(subject.immutable_ref, "quay.io/acme/app@sha256:child")
        self.assertEqual(subject.platform_source, "wud")

    def test_resolve_subject_rejects_stale_reported_digest(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc(
                    "sha256:index",
                    ("sha256:child",),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        subject = verifier.resolve_subject(
            "ghcr.io/acme/app:latest",
            "sha256:stale",
            ImagePlatform("linux", "amd64"),
        )

        self.assertEqual(subject.identity_status, "stale")
        self.assertEqual(subject.index_digest, "sha256:index")
        self.assertEqual(subject.manifest_digest, "")
        self.assertEqual(subject.immutable_ref, "")

    def test_resolve_subject_rejects_platform_mismatch(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): index_doc(
                    "sha256:index",
                    ("sha256:child",),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        subject = verifier.resolve_subject(
            "ghcr.io/acme/app:latest",
            "sha256:child",
            ImagePlatform("linux", "arm64"),
        )

        self.assertEqual(subject.identity_status, "mismatch")
        self.assertEqual(subject.error, "reported child digest platform does not match")
        self.assertEqual(subject.manifest_digest, "")
        self.assertEqual(subject.immutable_ref, "")

    def test_resolve_subject_rejects_variantless_platform_for_variant_child(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): variant_index_doc(
                    "sha256:index",
                    (("sha256:armv7", "linux", "arm", "v7"),),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        subject = verifier.resolve_subject(
            "ghcr.io/acme/app:latest",
            "sha256:armv7",
            ImagePlatform("linux", "arm"),
        )

        self.assertEqual(subject.identity_status, "mismatch")
        self.assertEqual(subject.error, "reported child digest platform does not match")
        self.assertEqual(subject.manifest_digest, "")
        self.assertEqual(subject.immutable_ref, "")

    def test_resolve_subject_rejects_ambiguous_platform_children(self) -> None:
        resolver = StaticResolver(
            {
                ("ghcr.io", "acme/app", "latest"): variant_index_doc(
                    "sha256:index",
                    (
                        ("sha256:first", "linux", "amd64", ""),
                        ("sha256:second", "linux", "amd64", ""),
                    ),
                ),
            }
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        subject = verifier.resolve_subject(
            "ghcr.io/acme/app:latest",
            "sha256:index",
            ImagePlatform("linux", "amd64"),
        )

        self.assertEqual(subject.identity_status, "mismatch")
        self.assertEqual(
            subject.error,
            "reported index digest has no matching platform child",
        )
        self.assertEqual(subject.manifest_digest, "")
        self.assertEqual(subject.immutable_ref, "")

    def test_resolve_subject_does_not_mark_unknown_index_digest_stale(self) -> None:
        resolver = SequencedHttpResolver(
            (
                index_doc("", ("sha256:child",)),
            )
        )
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=FailingResolver(),
        )

        subject = verifier.resolve_subject(
            "ghcr.io/acme/app:latest",
            "sha256:index",
            ImagePlatform("linux", "amd64"),
        )

        self.assertEqual(subject.identity_status, "error")
        self.assertEqual(subject.error, "current index digest could not be resolved")
        self.assertEqual(subject.manifest_digest, "")
        self.assertEqual(subject.immutable_ref, "")

    def test_resolve_subject_requires_reported_digest_and_platform(self) -> None:
        resolver = StaticResolver({})
        verifier = DigestVerifier(
            FakeDocker(),
            primary_resolver=resolver,
            fallback_resolver=resolver,
        )

        no_digest = verifier.resolve_subject(
            "ghcr.io/acme/app:latest",
            "",
            ImagePlatform("linux", "amd64"),
        )
        no_platform = verifier.resolve_subject(
            "ghcr.io/acme/app:latest",
            "sha256:index",
            None,
        )

        self.assertEqual(no_digest.identity_status, "unsupported")
        self.assertEqual(no_digest.error, "reported digest is required")
        self.assertEqual(no_platform.identity_status, "unsupported")
        self.assertEqual(no_platform.error, "platform is required")
        self.assertEqual(resolver.calls, [])

    def test_non_ghcr_manifest_unavailable_is_untrusted(self) -> None:
        verifier = DigestVerifier(
            FakeDocker(
                image_id="sha256:config",
                repo_digests=("docker.io/acme/app@sha256:other",),
            ),
            primary_resolver=FailingResolver(),
            fallback_resolver=FailingResolver(),
        )

        result = verifier.verify("docker.io/acme/app:latest", "sha256:expected")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "untrusted")
        self.assertEqual(result.reason, "manifest-unavailable-untrusted")
        self.assertEqual(result.seen_repo_digests, ("docker.io/acme/app@sha256:other",))

    def test_ghcr_manifest_unavailable_fails_closed(self) -> None:
        verifier = DigestVerifier(
            FakeDocker(
                image_id="sha256:config",
                repo_digests=("ghcr.io/acme/app@sha256:other",),
            ),
            primary_resolver=FailingResolver(),
            fallback_resolver=FailingResolver(),
        )

        result = verifier.verify("ghcr.io/acme/app:latest", "sha256:expected")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "manifest-unavailable")
        self.assertEqual(result.seen_repo_digests, ("ghcr.io/acme/app@sha256:other",))


class PayloadDigestTests(unittest.TestCase):
    def test_direct_digest_field(self) -> None:
        payload = {"digest": "sha256:abc123", "other": "value"}
        self.assertEqual(_payload_digest(payload), "sha256:abc123")

    def test_descriptor_digest_field(self) -> None:
        payload = {"Descriptor": {"digest": "sha256:desc456"}}
        self.assertEqual(_payload_digest(payload), "sha256:desc456")

    def test_direct_digest_takes_precedence_over_descriptor(self) -> None:
        payload = {
            "digest": "sha256:direct",
            "Descriptor": {"digest": "sha256:descriptor"},
        }
        self.assertEqual(_payload_digest(payload), "sha256:direct")

    def test_non_sha256_direct_digest_falls_through_to_descriptor(self) -> None:
        payload = {
            "digest": "md5:notsha",
            "Descriptor": {"digest": "sha256:descriptor"},
        }
        self.assertEqual(_payload_digest(payload), "sha256:descriptor")

    def test_no_digest_returns_empty_string(self) -> None:
        self.assertEqual(_payload_digest({}), "")

    def test_descriptor_without_sha256_returns_empty(self) -> None:
        payload = {"Descriptor": {"digest": "notsha256"}}
        self.assertEqual(_payload_digest(payload), "")

    def test_non_string_digest_returns_empty(self) -> None:
        self.assertEqual(_payload_digest({"digest": 12345}), "")

    def test_non_mapping_descriptor_is_skipped(self) -> None:
        payload = {"Descriptor": "not-a-mapping"}
        self.assertEqual(_payload_digest(payload), "")

    def test_descriptor_with_non_string_digest_returns_empty(self) -> None:
        payload = {"Descriptor": {"digest": None}}
        self.assertEqual(_payload_digest(payload), "")


class DockerManifestResolverVerboseTests(FakeDockerTestCase):
    def test_verbose_false_uses_regular_manifest_inspect(self) -> None:
        self._set_manifest_stdout(
            "docker.io/repo/app:1.0",
            {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json"},
        )
        command_runner = CommandRunner(env=self.env)
        docker = DockerCli(runner=command_runner)
        resolver = DockerManifestResolver(docker, verbose=False)

        from wudup.digest_verifier import parse_registry_image
        reg_image = parse_registry_image("docker.io/repo/app:1.0")
        assert reg_image is not None
        doc = resolver.fetch(reg_image, reg_image.tag)

        self.assertEqual(doc.source, "docker-manifest")
        self.assertEqual(doc.digest, "")
        self.assertIn("manifest inspect docker.io/repo/app:1.0", self._calls())
        self.assertNotIn("--verbose", self._calls())

    def test_verbose_true_uses_verbose_manifest_inspect(self) -> None:
        self._set_manifest_stdout(
            "docker.io/repo/app:1.0",
            manifest_index_digest("sha256:idx", "sha256:child"),
        )
        command_runner = CommandRunner(env=self.env)
        docker = DockerCli(runner=command_runner)
        resolver = DockerManifestResolver(docker, verbose=True)

        from wudup.digest_verifier import parse_registry_image
        reg_image = parse_registry_image("docker.io/repo/app:1.0")
        assert reg_image is not None
        doc = resolver.fetch(reg_image, reg_image.tag)

        self.assertEqual(doc.source, "docker-manifest-verbose")
        self.assertEqual(doc.digest, "sha256:idx")
        self.assertIn("manifest inspect --verbose docker.io/repo/app:1.0", self._calls())

    def test_verbose_true_with_direct_digest_field(self) -> None:
        self._set_manifest_stdout(
            "docker.io/repo/app:2.0",
            {"digest": "sha256:direct", "schemaVersion": 2},
        )
        command_runner = CommandRunner(env=self.env)
        docker = DockerCli(runner=command_runner)
        resolver = DockerManifestResolver(docker, verbose=True)

        from wudup.digest_verifier import parse_registry_image
        reg_image = parse_registry_image("docker.io/repo/app:2.0")
        assert reg_image is not None
        doc = resolver.fetch(reg_image, reg_image.tag)

        self.assertEqual(doc.source, "docker-manifest-verbose")
        self.assertEqual(doc.digest, "sha256:direct")

    def test_verbose_true_accepts_manifest_list_array(self) -> None:
        self._set_manifest_stdout(
            "docker.io/repo/app:3.0",
            [
                verbose_manifest_item("sha256:amd64"),
                verbose_manifest_item("sha256:arm64", architecture="arm64"),
            ],
        )
        command_runner = CommandRunner(env=self.env)
        docker = DockerCli(runner=command_runner)
        resolver = DockerManifestResolver(docker, verbose=True)

        from wudup.digest_verifier import parse_registry_image
        reg_image = parse_registry_image("docker.io/repo/app:3.0")
        assert reg_image is not None
        doc = resolver.fetch(reg_image, reg_image.tag)

        self.assertEqual(doc.source, "docker-manifest-verbose")
        self.assertEqual(doc.digest, "")
        self.assertTrue(doc.is_index())
        self.assertEqual(doc.child_digests(), ("sha256:amd64", "sha256:arm64"))


class DigestVerifierResolveTagDigestTests(FakeDockerTestCase):
    def _make_verifier(self) -> DigestVerifier:
        command_runner = CommandRunner(env=self.env)
        docker = DockerCli(runner=command_runner)
        resolver = DockerManifestResolver(docker, verbose=True)
        return DigestVerifier(docker, primary_resolver=resolver, fallback_resolver=resolver)

    def test_unsupported_image_reference_returns_not_ok(self) -> None:
        verifier = self._make_verifier()
        result = verifier.resolve_tag_digest("local-image-no-registry")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "untrusted")
        self.assertEqual(result.reason, "unsupported-image-reference")

    def test_failed_manifest_lookup_returns_not_ok(self) -> None:
        self._set_manifest_failure("docker.io/repo/app:1.0", "manifest not found\n")
        verifier = self._make_verifier()
        result = verifier.resolve_tag_digest("repo/app:1.0")
        self.assertFalse(result.ok)
        self.assertIn("unavailable", result.reason)
        self.assertIn("manifest", result.error.lower())

    def test_manifest_without_digest_returns_not_ok(self) -> None:
        self._set_manifest_stdout(
            "docker.io/repo/app:1.0",
            {"schemaVersion": 2},
        )
        verifier = self._make_verifier()
        result = verifier.resolve_tag_digest("repo/app:1.0")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "manifest-digest-missing")

    def test_success_with_descriptor_digest(self) -> None:
        self._set_manifest_stdout(
            "docker.io/repo/app:2.0",
            manifest_index_digest("sha256:resolved", "sha256:child"),
        )
        verifier = self._make_verifier()
        result = verifier.resolve_tag_digest("repo/app:2.0")
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.reason, "tag-digest-resolved")
        self.assertEqual(result.digest, "sha256:resolved")

    def test_success_source_is_populated(self) -> None:
        self._set_manifest_stdout(
            "docker.io/repo/app:3.0",
            manifest_index_digest("sha256:abc", "sha256:child"),
        )
        verifier = self._make_verifier()
        result = verifier.resolve_tag_digest("repo/app:3.0")
        self.assertTrue(result.ok)
        self.assertIn("docker-manifest", result.source)

    def test_verbose_manifest_list_uses_registry_header_for_index_digest(self) -> None:
        self._set_manifest_stdout(
            "docker.io/repo/app:4.0",
            [
                verbose_manifest_item("sha256:amd64"),
                verbose_manifest_item("sha256:arm64", architecture="arm64"),
            ],
        )
        verifier = self._make_verifier()

        with mock.patch(
            "wudup.digest_verifier.RegistryHttpManifestResolver.fetch",
            return_value=ManifestDocument(
                source="registry-http:registry-1.docker.io",
                digest="sha256:index",
                media_type=MANIFEST_INDEX_TYPE,
                payload=manifest_index("sha256:amd64", "sha256:arm64"),
            ),
        ):
            result = verifier.resolve_tag_digest("repo/app:4.0")

        self.assertTrue(result.ok)
        self.assertEqual(result.digest, "sha256:index")

    def _set_verbose_index(self, image: str, *children: str) -> None:
        self._set_manifest_stdout(
            image,
            [
                verbose_manifest_item(child, architecture=f"arch{index}")
                for index, child in enumerate(children)
            ],
        )

    def _verify_index_without_https(self, expected: str) -> object:
        verifier = self._make_verifier()
        # A plain-HTTP registry, like the e2e one, cannot report the index digest.
        with mock.patch(
            "wudup.digest_verifier.RegistryHttpManifestResolver.fetch",
            side_effect=ManifestLookupError("https unavailable"),
        ):
            return verifier.verify_tag_digest("repo/app:latest", expected)

    def test_verify_tag_digest_accepts_index_matching_planned_digest(self) -> None:
        self._set_verbose_index("docker.io/repo/app:latest", "sha256:amd64", "sha256:att")
        self._set_verbose_index("docker.io/repo/app@sha256:index", "sha256:amd64", "sha256:att")

        result = self._verify_index_without_https("sha256:index")

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "tag-manifest-current")
        self.assertEqual(result.digest, "sha256:index")

    def test_verify_tag_digest_rejects_index_that_differs_from_planned_digest(self) -> None:
        self._set_verbose_index("docker.io/repo/app:latest", "sha256:newer", "sha256:att")
        self._set_verbose_index("docker.io/repo/app@sha256:index", "sha256:amd64", "sha256:att")

        result = self._verify_index_without_https("sha256:index")

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "stale-digest")
        self.assertEqual(result.digest, "")

    def test_verify_tag_digest_reports_unknown_index_digest_as_unavailable(self) -> None:
        self._set_verbose_index("docker.io/repo/app:latest", "sha256:amd64", "sha256:att")
        self._set_manifest_failure("docker.io/repo/app@sha256:index", "manifest unknown\n")

        result = self._verify_index_without_https("sha256:index")

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "tag-digest-unavailable")
        self.assertEqual(result.digest, "")
        message = _digest_pin_resolve_error("repo/app:latest", result)
        self.assertNotIn("moved", message)
        self.assertIn("Could not confirm digest-pin target for repo/app:latest", message)


if __name__ == "__main__":
    unittest.main()
