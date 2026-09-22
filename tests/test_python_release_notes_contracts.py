from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from wudup.db import init_db, open_db
from wudup.release_notes import (
    GitHubClient,
    ReleaseNoteLink,
    ReleaseSecurityAssessment,
    cached_release_notes,
    refresh_release_notes,
    release_note_contexts,
)
from wudup.wud_file import parse_wud_text


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
        for code in (404, 403):
            with self.subTest(code=code):
                error = urllib.error.HTTPError(url, code, "example failure", {}, None)
                self.addCleanup(error.close)
                with mock.patch("urllib.request.urlopen", side_effect=error) as open_url:
                    if code == 404:
                        self.assertEqual(
                            GitHubClient(timeout=2.5).get_json(url),
                            {"message": "Not Found"},
                        )
                    else:
                        with self.assertRaises(urllib.error.HTTPError) as caught:
                            GitHubClient(timeout=2.5).get_json(url)
                        self.assertIs(caught.exception, error)
                self.assertEqual(open_url.call_args.kwargs, {"timeout": 2.5})
                self.assertIsNone(open_url.call_args.args[0].get_header("Authorization"))


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
