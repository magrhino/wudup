from __future__ import annotations

import json
import unittest
import urllib.error
from unittest import mock

from wudup.db import init_db, open_db
from wudup.release_note_cache import ERROR_CACHE_TTL_SECONDS
from wudup.release_note_providers import (
    CVE_ID_RE,
    GHSA_ID_RE,
    GITHUB_TOKEN_REJECTED_MESSAGE,
    GitHubAuthenticationError,
    _strip_lsio_suffix,
)
from wudup.release_notes import (
    GitHubClient,
    ReleaseNoteInfo,
    ReleaseNoteLink,
    ReleaseSecurityAssessment,
    assess_release_security,
    cached_release_notes,
    refresh_release_notes,
    release_note_contexts,
)
from wudup.wud_file import parse_wud_text


class ProviderPatternContractTests(unittest.TestCase):
    def test_advisory_identifiers_remain_ascii_and_case_insensitive(self) -> None:
        self.assertIsNotNone(GHSA_ID_RE.fullmatch("ghsa-Ab12-cD34-Ef56"))
        self.assertIsNone(GHSA_ID_RE.fullmatch("GHSA-ıaaa-bbbb-cccc"))
        self.assertIsNotNone(CVE_ID_RE.fullmatch("cve-2026-12345"))
        self.assertIsNone(CVE_ID_RE.fullmatch("CVE-２０２６-１２３４５"))

    def test_lsio_suffix_keeps_ascii_digits_and_unicode_ignore_case_letters(self) -> None:
        self.assertEqual(_strip_lsio_suffix("v1.2.3-LS42-aB1"), "v1.2.3")
        self.assertEqual(_strip_lsio_suffix("v1.2.3-ls42-K"), "v1.2.3")
        self.assertEqual(_strip_lsio_suffix("v1.2.3-ls４２"), "v1.2.3-ls４２")


class GitHubTransportContractTests(unittest.TestCase):
    def test_request_preserves_headers_and_timeout(self) -> None:
        url = "https://api.github.com/repos/acme/app/releases/latest"
        with mock.patch("urllib.request.urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = (
                b'{"tag_name":"v1.2.1"}'
            )
            self.assertEqual(
                GitHubClient(token="example-token").get_json(url),
                {"tag_name": "v1.2.1"},
            )

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(request.get_header("User-agent"), "wudup-webui-release-notes/1.0")
        self.assertEqual(request.get_header("Authorization"), "Bearer example-token")
        self.assertEqual(open_url.call_args.kwargs, {"timeout": 6.0})

    def test_only_not_found_is_converted_to_missing_response(self) -> None:
        url = "https://api.github.com/repos/acme/app/releases/latest"
        for code in (404, 403, 401):
            with self.subTest(code=code):
                error = urllib.error.HTTPError(url, code, "example failure", {}, None)
                self.addCleanup(error.close)
                client = GitHubClient(timeout=2.5)
                with mock.patch("urllib.request.urlopen", side_effect=error) as open_url:
                    if code == 404:
                        self.assertEqual(
                            client.get_json(url),
                            {"message": "Not Found"},
                        )
                    else:
                        with self.assertRaises(urllib.error.HTTPError) as caught:
                            client.get_json(url)
                        self.assertIs(caught.exception, error)
                self.assertEqual(open_url.call_args.kwargs, {"timeout": 2.5})
                self.assertIsNone(open_url.call_args.args[0].get_header("Authorization"))

    def test_rejected_token_raises_operator_error_without_token(self) -> None:
        url = "https://api.github.com/repos/acme/app/releases/latest"
        error = urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        self.addCleanup(error.close)
        client = GitHubClient(token="example-dead-token")
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GitHubAuthenticationError) as caught:
                client.get_json(url)

        message = str(caught.exception)
        self.assertTrue(
            message.startswith(
                "GitHub rejected the configured GITHUB_TOKEN (expired, revoked, or "
                "mistyped). Create a new token or remove GITHUB_TOKEN to use "
                "anonymous access, then restart WUDup. Cached release-note errors "
                "are retried after 15 minutes. Detail: "
            )
        )
        self.assertEqual(ERROR_CACHE_TTL_SECONDS, 15 * 60)
        self.assertIn(f"HTTP 401 Unauthorized for {url}", message)
        self.assertNotIn("example-dead-token", message)
        self.assertIs(caught.exception.__cause__, error)

    def test_rejected_token_is_stored_as_release_note_error(self) -> None:
        targets = parse_wud_text("ghcr.io/acme/app:1.0.0 tag=1.1.0\n").targets
        error = urllib.error.HTTPError("https://api.github.com/", 401, "Unauthorized", {}, None)
        self.addCleanup(error.close)
        with open_db(":memory:") as conn, mock.patch(
            "urllib.request.urlopen", side_effect=error
        ):
            init_db(conn)
            info = refresh_release_notes(
                conn,
                targets,
                {"GITHUB_TOKEN": "example-dead-token"},
                now="2026-01-02T00:00:00+00:00",
            )[0]
            row = conn.execute("SELECT error FROM release_note_cache").fetchone()

        self.assertEqual(info.status, "error")
        self.assertTrue(info.error.startswith(GITHUB_TOKEN_REJECTED_MESSAGE))
        self.assertEqual(row["error"], info.error)
        self.assertNotIn("example-dead-token", info.error)

    def test_rejected_token_is_named_when_security_backfill_fails(self) -> None:
        targets = parse_wud_text("ghcr.io/acme/app:1.2.0 tag=1.2.1\n").targets
        release = {
            "tag_name": "v1.2.1",
            "name": "v1.2.1",
            "html_url": "https://github.com/acme/app/releases/tag/v1.2.1",
            "body": "Routine fixes.",
        }
        error = urllib.error.HTTPError("https://api.github.com/", 401, "Unauthorized", {}, None)
        self.addCleanup(error.close)
        with open_db(":memory:") as conn:
            init_db(conn)
            refresh_release_notes(
                conn,
                targets,
                {},
                client=GitHubClient(fetch_json=lambda _url: release),
                now="2026-01-01T00:00:00+00:00",
            )
            conn.execute(
                "UPDATE release_note_cache SET metadata_json = ?",
                (json.dumps({"line_no": 1}),),
            )
            with mock.patch("urllib.request.urlopen", side_effect=error):
                info = refresh_release_notes(
                    conn,
                    targets,
                    {},
                    client=GitHubClient(token="example-dead-token"),
                    now="2026-01-02T00:00:00+00:00",
                )[0]
            stored = cached_release_notes(conn, targets, {})[0]

        self.assertEqual(info.status, "ready")
        self.assertEqual(
            info.security.reason,
            "Existing release metadata was preserved, but GitHub security evidence "
            f"could not be checked. {GITHUB_TOKEN_REJECTED_MESSAGE}",
        )
        self.assertEqual(stored.security.reason, info.security.reason)

    def test_rejected_token_is_named_when_advisory_lookup_fails(self) -> None:
        context = release_note_contexts(
            parse_wud_text("ghcr.io/acme/app:1.2.0 tag=1.2.1\n").targets,
            {},
        )[0]
        info = ReleaseNoteInfo(
            line_no=1,
            status="ready",
            provider="github",
            image_repo="acme/app",
            upstream_repo="acme/app",
            release_tag="v1.2.1",
            body="Fixes GHSA-aaaa-bbbb-cccc.",
        )
        error = urllib.error.HTTPError("https://api.github.com/", 401, "Unauthorized", {}, None)
        self.addCleanup(error.close)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            assessment, _links = assess_release_security(
                context,
                info,
                GitHubClient(token="example-dead-token"),
            )

        self.assertEqual(assessment.reason_code, "advisory_lookup_failed")
        self.assertEqual(
            assessment.reason,
            "Security signals were found, but GitHub advisory evidence could not "
            f"be checked. {GITHUB_TOKEN_REJECTED_MESSAGE}",
        )


class ReleaseNoteCacheContractTests(unittest.TestCase):
    def test_legacy_row_read_preserves_values_without_network_or_writes(self) -> None:
        targets = parse_wud_text("ghcr.io/acme/app:1.0.0 tag=1.1.0\n").targets
        context = release_note_contexts(targets, {})[0]
        url = "https://github.com/acme/app/releases/tag/v1.1.0"
        with open_db(":memory:") as conn:
            init_db(conn)
            conn.execute(
                """
                INSERT INTO release_note_cache (
                    cache_key, provider, image_repo, upstream_repo, current_tag,
                    target_tag, status, release_tag, body, links_json, metadata_json,
                    created_at, updated_at
                ) VALUES (?, 'github', 'acme/app', 'acme/app', '1.0.0', '1.1.0',
                    'ready', 'v1.1.0', 'Existing notes', ?, '{"line_no":77}', ?, ?)
                """,
                (
                    context.cache_key,
                    '[{"label":"GitHub release","url":"' + url + '","kind":"github_release"}]',
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-02T00:00:00+00:00",
                ),
            )
            before = conn.total_changes
            with mock.patch.object(GitHubClient, "get_json") as get_json:
                info = cached_release_notes(conn, targets, {})[0]
            get_json.assert_not_called()
            self.assertEqual(conn.total_changes, before)

        self.assertEqual(info.line_no, 1)
        self.assertEqual(info.status, "ready")
        self.assertEqual(info.body, "Existing notes")
        self.assertEqual(info.release_tag, "v1.1.0")
        self.assertEqual(info.links, [ReleaseNoteLink("GitHub release", url, "github_release")])
        self.assertEqual(info.refreshed_at, "2026-01-02T00:00:00+00:00")
        self.assertEqual(info.security, ReleaseSecurityAssessment())

    def test_provider_errors_retry_at_boundary_and_keep_created_timestamp(self) -> None:
        targets = parse_wud_text("ghcr.io/acme/app:1.0.0 tag=1.1.0\n").targets
        fetch = mock.Mock(side_effect=RuntimeError("example sensitive failure"))
        client = GitHubClient(fetch_json=fetch)
        with open_db(":memory:") as conn:
            init_db(conn)
            for timestamp, expected_calls in (
                ("2026-01-02T00:00:00+00:00", 1),
                ("2026-01-02T00:14:59+00:00", 1),
                ("2026-01-02T00:15:00+00:00", 2),
            ):
                info = refresh_release_notes(
                    conn, targets, {}, client=client, now=timestamp,
                    redact_error=lambda _error: "safe failure",
                )[0]
                self.assertEqual(fetch.call_count, expected_calls)
                self.assertEqual(info.status, "error")
                self.assertEqual(info.error, "safe failure")
            row = conn.execute("SELECT * FROM release_note_cache").fetchone()

        self.assertEqual(row["created_at"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(row["updated_at"], "2026-01-02T00:15:00+00:00")
        self.assertEqual(row["error"], "safe failure")
