from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wudup.db import init_db, open_db
from wudup.release_notes import (
    GitHubClient,
    refresh_release_notes,
    release_note_contexts,
)
from wudup.wud_file import parse_wud_text


class ImageOnlyReleaseNotesTests(unittest.TestCase):
    def assert_image_release(self, image: str, repo: str, environ: dict[str, str]):
        tag = "3.4.4-r0-ls97"
        for target_tag in ("latest", tag):
            with self.subTest(image=image, target_tag=target_tag):
                targets = parse_wud_text(
                    f"{image}:3.3.0-r0-ls90 tag={target_tag}\n"
                ).targets
                endpoint = "latest" if target_tag == "latest" else f"tags/{tag}"
                expected_url = f"https://api.github.com/repos/{repo}/releases/{endpoint}"
                calls = []

                def fetch_json(url, calls=calls, expected_url=expected_url):
                    calls.append(url)
                    self.assertEqual(url, expected_url)
                    return {
                        "tag_name": tag,
                        "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
                        "body": "Routine image maintenance.",
                    }

                with open_db(":memory:") as conn:
                    init_db(conn)
                    info = refresh_release_notes(
                        conn, targets, environ, client=GitHubClient(fetch_json=fetch_json)
                    )[0]

                self.assertEqual(info.status, "ready")
                self.assertEqual(info.provider, "lsio")
                self.assertEqual(info.image_repo, repo)
                self.assertEqual(info.release_tag, tag)
                self.assertEqual(info.body, "Routine image maintenance.")
                self.assertEqual([link.kind for link in info.links], ["lsio_release"])
                self.assertEqual(calls, [expected_url])

    def test_socket_proxy_uses_checked_in_self_mapping_across_registries(self):
        upstream_map = Path(__file__).resolve().parents[1] / "wud/upstreams.txt"
        for prefix in ("", "lscr.io/", "ghcr.io/"):
            self.assert_image_release(
                f"{prefix}linuxserver/socket-proxy",
                "linuxserver/docker-socket-proxy",
                {"UPSTREAM_MAP": str(upstream_map)},
            )

    def test_new_image_only_container_needs_only_a_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            upstream_map = Path(tmp) / "upstreams.txt"
            repo = "linuxserver/docker-image-only-example"
            upstream_map.write_text(f"{repo}: {repo}\n", encoding="utf-8")
            self.assert_image_release(
                "ghcr.io/linuxserver/image-only-example",
                repo,
                {"UPSTREAM_MAP": str(upstream_map)},
            )

    def test_unmapped_container_still_requires_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            upstream_map = Path(tmp) / "upstreams.txt"
            upstream_map.write_text("", encoding="utf-8")
            targets = parse_wud_text("ghcr.io/linuxserver/unmapped:latest\n").targets
            context = release_note_contexts(
                targets, {"UPSTREAM_MAP": str(upstream_map)}
            )[0]
        self.assertEqual(context.provider, "unsupported")
        self.assertIn("missing LSIO upstream mapping", context.error)
