from __future__ import annotations

import json
import unittest
from pathlib import Path

from wudup.release_note_models import ReleaseNoteContext
from wudup.release_note_providers import (
    GitHubAuthenticationError,
    GitHubClient,
    _fetch_github_release_note,
    _fetch_lsio_release_note,
    _fetch_lsio_upstream_release,
)
from wudup.release_tag_matching import (
    image_tag_version,
    match_release_by_version,
    release_tag_version,
)

FIXTURES = Path(__file__).with_name("fixtures") / "github-release-tag-formats.json"
NOT_FOUND = {"message": "Not Found"}


def fixture_cases() -> list[dict[str, object]]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]


def release(
    tag: str, *, draft: bool = False, prerelease: bool = False
) -> dict[str, object]:
    return {
        "tag_name": tag,
        "name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/acme/app/releases/tag/{tag}",
        "body": f"Notes for {tag}.",
    }


def context(repo: str, target_tag: str) -> ReleaseNoteContext:
    return ReleaseNoteContext(
        line_no=1,
        cache_key=f"{repo}:{target_tag}",
        provider="github",
        image_repo=repo,
        upstream_repo=repo,
        current_tag="",
        target_tag=target_tag,
    )


def recording_client(
    responses: dict[str, object],
) -> tuple[GitHubClient, list[str]]:
    calls: list[str] = []

    def fetch_json(url: str) -> object:
        calls.append(url)
        return responses.get(url, NOT_FOUND)

    return GitHubClient(fetch_json=fetch_json), calls


def list_url(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}/releases?per_page=100"


class ReleaseTagVersionTests(unittest.TestCase):
    def test_image_tag_version_strips_variant_suffixes(self) -> None:
        cases = {
            "2.35.3-distroless": "2.35.3",
            "v1.8.0-rc2-slim": "1.8.0-rc2",
            "2.11.9-alpine": "2.11.9",
            "1.2.3-alpine3.20": "1.2.3",
            "5.0-debian12": "5.0",
            "8.23.0": "8.23.0",
            "12.4": "12.4",
        }
        for tag, expected in cases.items():
            with self.subTest(tag=tag):
                self.assertEqual(image_tag_version(tag), expected)

    def test_image_tag_version_is_empty_without_a_version(self) -> None:
        for tag in ("stable", "latest", "alpine", "16", "v2", "nightly-2026"):
            with self.subTest(tag=tag):
                self.assertEqual(image_tag_version(tag), "")

    def test_release_tag_version_strips_known_prefixes(self) -> None:
        cases = {
            ("n8n@2.35.3", "n8n-io/n8n"): "2.35.3",
            ("v1.8.0-rc2", "crowdsecurity/crowdsec"): "1.8.0-rc2",
            ("release-1.31.10", "nginx/nginx"): "1.31.10",
            ("curl-8_23_0", "curl/curl"): "8.23.0",
            ("v12.4-202610261649", "jellyfin/jellyfin-packaging"): "12.4-202610261649",
            ("app-v1.2.3", "acme/app"): "1.2.3",
        }
        for (tag, repo), expected in cases.items():
            with self.subTest(tag=tag):
                self.assertEqual(release_tag_version(tag, repo), expected)

    def test_release_tag_version_ignores_other_packages_and_unversioned_tags(
        self,
    ) -> None:
        for tag in ("@n8n/task-runner-python@2.35.3", "nightly", "other-1.2.3"):
            with self.subTest(tag=tag):
                self.assertEqual(release_tag_version(tag, "n8n-io/n8n"), "")


class MatchReleaseByVersionTests(unittest.TestCase):
    def test_ambiguous_exact_matches_return_none(self) -> None:
        releases = [release("v1.2.3"), release("release-1.2.3")]
        self.assertIsNone(match_release_by_version(releases, "1.2.3", "acme/app"))

    def test_ambiguous_build_stamped_matches_return_none(self) -> None:
        releases = [release("v12.4-202610261649"), release("v12.4-202611011200")]
        self.assertIsNone(match_release_by_version(releases, "12.4", "acme/app"))

    def test_exact_version_wins_over_build_stamped_release(self) -> None:
        releases = [release("v12.4-202610261649"), release("release-12.4")]
        match = match_release_by_version(releases, "12.4", "acme/app")
        self.assertEqual(match and match["tag_name"], "release-12.4")

    def test_build_stamp_must_be_numeric_and_separated(self) -> None:
        releases = [release("v1.2-rc1"), release("v1.2.1"), release("v1.20")]
        self.assertIsNone(match_release_by_version(releases, "1.2", "acme/app"))

    def test_short_numeric_suffix_is_a_prerelease_not_a_build_stamp(self) -> None:
        releases = [release("v1.0.0-1"), release("v1.0.0-12")]
        self.assertIsNone(match_release_by_version(releases, "1.0.0", "acme/app"))

    def test_release_marked_prerelease_is_not_a_build_stamp(self) -> None:
        releases = [release("v1.0.0-202610261649", prerelease=True)]
        self.assertIsNone(match_release_by_version(releases, "1.0.0", "acme/app"))

    def test_semver_build_metadata_is_a_build_stamp(self) -> None:
        match = match_release_by_version([release("v1.0.0+5")], "1.0.0", "acme/app")
        self.assertEqual(match and match["tag_name"], "v1.0.0+5")

    def test_draft_releases_are_ignored(self) -> None:
        releases = [release("release-1.2.3", draft=True), release("app-1.2.3")]
        match = match_release_by_version(releases, "1.2.3", "acme/app")
        self.assertEqual(match and match["tag_name"], "app-1.2.3")

    def test_image_tag_without_version_returns_none(self) -> None:
        releases = [release("stable"), release("v1.2.3")]
        self.assertIsNone(match_release_by_version(releases, "stable", "acme/app"))


class GitHubReleaseFallbackTests(unittest.TestCase):
    def test_fixture_rows_resolve_to_listed_release(self) -> None:
        for case in fixture_cases():
            repo = str(case["repo"])
            image_tag = str(case["image_tag"])
            with self.subTest(case=case["name"]):
                client, calls = recording_client({list_url(repo): case["releases"]})

                info = _fetch_github_release_note(
                    context(repo, image_tag), client, "2026-11-26T00:00:00Z"
                )

                expected = str(case["expected_tag"])
                self.assertEqual(info.status, "ready")
                self.assertEqual(info.release_tag, expected)
                self.assertEqual(info.links[0].kind, "github_release")
                self.assertEqual(
                    info.links[0].url,
                    next(
                        item["html_url"]
                        for item in case["releases"]
                        if item["tag_name"] == expected
                    ),
                )
                self.assertEqual(calls.count(list_url(repo)), 1)
                self.assertEqual(calls[-1], list_url(repo))

    def test_exact_tag_hit_skips_listing(self) -> None:
        client, calls = recording_client(
            {
                "https://api.github.com/repos/acme/app/releases/tags/v1.2.3": release(
                    "v1.2.3"
                )
            }
        )

        info = _fetch_github_release_note(
            context("acme/app", "1.2.3"), client, "2026-11-26T00:00:00Z"
        )

        self.assertEqual(info.release_tag, "v1.2.3")
        self.assertEqual(
            calls, ["https://api.github.com/repos/acme/app/releases/tags/v1.2.3"]
        )

    def test_ambiguous_listing_reports_not_found(self) -> None:
        client, _calls = recording_client(
            {
                list_url("acme/app"): [
                    release("v1.2.3-20261101"),
                    release("v1.2.3-20261102"),
                ]
            }
        )

        info = _fetch_github_release_note(
            context("acme/app", "1.2.3-alpine"), client, "2026-11-26T00:00:00Z"
        )

        self.assertEqual(info.status, "not_found")
        self.assertEqual(info.release_tag, "1.2.3-alpine")

    def test_tag_without_version_reports_not_found_without_listing(self) -> None:
        client, calls = recording_client({list_url("acme/app"): [release("v1.2.3")]})

        info = _fetch_github_release_note(
            context("acme/app", "stable"), client, "2026-11-26T00:00:00Z"
        )

        self.assertEqual(info.status, "not_found")
        self.assertNotIn(list_url("acme/app"), calls)

    def test_lsio_upstream_lists_releases_once_for_composite_tag(self) -> None:
        repo = "qbittorrent/qBittorrent"
        client, calls = recording_client(
            {list_url(repo): [release("release-5.2.2"), release("release-5.2.1")]}
        )

        match = _fetch_lsio_upstream_release(client, repo, "5.2.2_v2.0.11")

        self.assertEqual(match and match["tag_name"], "release-5.2.2")
        self.assertEqual(calls.count(list_url(repo)), 1)

    def test_lsio_upstream_listing_failure_returns_no_upstream_release(self) -> None:
        repo = "Radarr/Radarr"

        def fetch_json(url: str) -> object:
            if url == list_url(repo):
                raise TimeoutError("timed out")
            return NOT_FOUND

        with self.assertLogs("wudup.release_note_providers", "WARNING") as logs:
            match = _fetch_lsio_upstream_release(
                GitHubClient(fetch_json=fetch_json), repo, "5.1.0"
            )

        self.assertIsNone(match)
        self.assertIn("without upstream notes", logs.output[0])

    def test_lsio_upstream_exact_lookup_failure_returns_no_upstream_release(
        self,
    ) -> None:
        def fetch_json(url: str) -> object:
            raise ValueError("invalid JSON")

        with self.assertLogs("wudup.release_note_providers", "WARNING"):
            match = _fetch_lsio_upstream_release(
                GitHubClient(fetch_json=fetch_json), "Radarr/Radarr", "5.1.0"
            )

        self.assertIsNone(match)

    def test_lsio_note_survives_failed_upstream_lookup(self) -> None:
        lsio_release = {
            "tag_name": "5.1.0-ls1",
            "name": "5.1.0-ls1",
            "html_url": (
                "https://github.com/linuxserver/docker-radarr/releases/tag/5.1.0-ls1"
            ),
            "body": "Remote Changes:\n- Updating to 5.1.0",
        }

        def fetch_json(url: str) -> object:
            if url.endswith("/linuxserver/docker-radarr/releases/tags/5.1.0-ls1"):
                return lsio_release
            raise TimeoutError("timed out")

        lsio_context = ReleaseNoteContext(
            line_no=1,
            cache_key="radarr",
            provider="lsio",
            image_repo="linuxserver/docker-radarr",
            upstream_repo="Radarr/Radarr",
            current_tag="4.0.0-ls1",
            target_tag="5.1.0-ls1",
        )
        with self.assertLogs("wudup.release_note_providers", "WARNING"):
            info = _fetch_lsio_release_note(
                lsio_context,
                GitHubClient(fetch_json=fetch_json),
                "2026-11-26T00:00:00Z",
            )

        self.assertEqual(info.status, "ready")
        self.assertEqual(info.release_tag, "5.1.0-ls1")
        self.assertEqual(
            [link.kind for link in info.links], ["lsio_release"]
        )

    def test_lsio_upstream_listing_keeps_rejected_token_error(self) -> None:
        repo = "Radarr/Radarr"

        def fetch_json(url: str) -> object:
            if url == list_url(repo):
                raise GitHubAuthenticationError("token rejected")
            return NOT_FOUND

        with self.assertRaises(GitHubAuthenticationError):
            _fetch_lsio_upstream_release(
                GitHubClient(fetch_json=fetch_json), repo, "5.1.0"
            )


if __name__ == "__main__":
    unittest.main()
