from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wudup.db import init_db, open_db
from wudup.release_note_upstreams import (
    LINK_ONLY_PROVIDER,
    ReleaseNoteUpstream,
    image_map_key,
    load_release_note_upstream_map,
    parse_release_note_upstream_map,
)
from wudup.release_notes import (
    GitHubClient,
    ReleaseNoteLink,
    cached_release_notes,
    refresh_release_notes,
    release_note_contexts,
)
from wudup.wud_file import parse_wud_text

MAP_FILE = Path(__file__).resolve().parents[1] / "wud" / "release-note-upstreams.txt"
# An empty app dir keeps lookups on the checked-in map, not an installed /app copy.
CHECKED_IN_ENV = {"WUD_APP_DIR": ""}


def _no_network(url: str) -> object:
    raise AssertionError(f"unexpected GitHub request: {url}")


def _release(repo: str, tag: str) -> dict[str, str]:
    return {
        "tag_name": tag,
        "name": tag,
        "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
        "body": "Upstream notes",
        "published_at": "2026-10-14T08:00:00Z",
    }


class ReleaseNoteUpstreamMapTests(unittest.TestCase):
    def test_checked_in_map_loads_every_entry(self) -> None:
        lines = [
            line
            for line in MAP_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]

        mapping = load_release_note_upstream_map(CHECKED_IN_ENV)

        # The shell validator and this loader must accept the same entries.
        self.assertEqual(len(mapping), len(lines))
        self.assertEqual(mapping["github.com/curl/curl-container"].repo, "curl/curl")
        self.assertEqual(
            mapping["github.com/nginxinc/docker-nginx-unprivileged"].repo,
            "nginx/nginx",
        )
        self.assertEqual(
            mapping["github.com/caddyserver/caddy-docker"].repo,
            "caddyserver/caddy",
        )
        self.assertEqual(mapping["caddy"].repo, "caddyserver/caddy")
        self.assertEqual(
            mapping["postgres"].notes_url,
            "https://www.postgresql.org/docs/release/",
        )

    def test_parse_skips_invalid_lines_and_normalizes_image_keys(self) -> None:
        mapping = parse_release_note_upstream_map(
            "# comment\n"
            "docker.io/library/caddy: caddyserver/caddy\n"
            "GitHub.com/Owner/Packaging: Owner/App\n"
            "missing-separator owner/repo\n"
            "caddy:2: caddyserver/caddy\n"
            "bad-value: not a repo\n"
            "insecure: http://example.invalid/notes\n"
        )

        self.assertEqual(
            mapping,
            {
                "caddy": ReleaseNoteUpstream(repo="caddyserver/caddy"),
                "github.com/owner/packaging": ReleaseNoteUpstream(repo="Owner/App"),
            },
        )

    def test_image_map_key_normalizes_docker_hub_and_keeps_other_registries(
        self,
    ) -> None:
        self.assertEqual(image_map_key("caddy:2.11.9"), "caddy")
        self.assertEqual(image_map_key("library/caddy"), "caddy")
        self.assertEqual(image_map_key("docker.io/library/caddy:2"), "caddy")
        self.assertEqual(image_map_key("index.docker.io/curlimages/curl:8"), "curlimages/curl")
        self.assertEqual(image_map_key("ghcr.io/Owner/App:1@sha256:abc"), "ghcr.io/owner/app")
        self.assertEqual(image_map_key("localhost:5000/app:1"), "localhost:5000/app")

    def test_custom_app_dir_map_replaces_checked_in_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "wud" / "release-note-upstreams.txt"
            map_path.parent.mkdir()
            map_path.write_text("example/app: example/upstream\n", encoding="utf-8")

            mapping = load_release_note_upstream_map({"WUD_APP_DIR": tmp})

        self.assertEqual(
            mapping,
            {"example/app": ReleaseNoteUpstream(repo="example/upstream")},
        )


class ReleaseNoteUpstreamContextTests(unittest.TestCase):
    def test_packaging_repo_source_resolves_to_upstream_with_packaging_link(
        self,
    ) -> None:
        parsed = parse_wud_text("curlimages/curl:8.23.0\n")

        contexts = release_note_contexts(
            parsed.targets,
            CHECKED_IN_ENV,
            source_resolver=lambda _target: "https://github.com/curl/curl-container",
        )

        self.assertEqual(contexts[0].provider, "github")
        self.assertEqual(contexts[0].image_repo, "curl/curl-container")
        self.assertEqual(contexts[0].upstream_repo, "curl/curl")
        self.assertEqual(
            contexts[0].links,
            (
                ReleaseNoteLink(
                    "Packaging repo",
                    "https://github.com/curl/curl-container",
                    "packaging_repo",
                ),
            ),
        )

    def test_packaging_repo_key_ignores_source_case(self) -> None:
        parsed = parse_wud_text("nginxinc/nginx-unprivileged:1.29.4\n")

        contexts = release_note_contexts(
            parsed.targets,
            CHECKED_IN_ENV,
            source_resolver=lambda _target: (
                "https://github.com/NginxInc/Docker-Nginx-Unprivileged"
            ),
        )

        self.assertEqual(contexts[0].upstream_repo, "nginx/nginx")
        self.assertEqual(
            contexts[0].links[0].url,
            "https://github.com/NginxInc/Docker-Nginx-Unprivileged",
        )

    def test_official_image_without_source_uses_image_key(self) -> None:
        parsed = parse_wud_text("caddy:2.11.9\n")

        contexts = release_note_contexts(parsed.targets, CHECKED_IN_ENV)

        self.assertEqual(contexts[0].provider, "github")
        self.assertEqual(contexts[0].image_repo, "caddy")
        self.assertEqual(contexts[0].upstream_repo, "caddyserver/caddy")
        self.assertEqual(contexts[0].links, ())

    def test_official_image_packaging_source_resolves_with_packaging_link(
        self,
    ) -> None:
        parsed = parse_wud_text("caddy:2.11.9\n")

        contexts = release_note_contexts(
            parsed.targets,
            CHECKED_IN_ENV,
            source_resolver=lambda _target: "https://github.com/caddyserver/caddy-docker",
        )

        self.assertEqual(contexts[0].image_repo, "caddyserver/caddy-docker")
        self.assertEqual(contexts[0].upstream_repo, "caddyserver/caddy")
        self.assertEqual(contexts[0].links[0].kind, "packaging_repo")

    def test_discovered_unmapped_source_beats_image_key(self) -> None:
        # A per-container WUD link (or source label) is the operator's override.
        parsed = parse_wud_text("caddy:2.11.9\n")

        contexts = release_note_contexts(
            parsed.targets,
            CHECKED_IN_ENV,
            source_resolver=lambda _target: (
                "https://github.com/example/caddy-fork/releases/tag/v2.11.9"
            ),
        )

        self.assertEqual(contexts[0].provider, "github")
        self.assertEqual(contexts[0].upstream_repo, "example/caddy-fork")
        self.assertEqual(contexts[0].links, ())

    def test_source_that_already_is_the_upstream_gets_no_packaging_link(self) -> None:
        parsed = parse_wud_text("caddy:2.11.9\n")

        contexts = release_note_contexts(
            parsed.targets,
            CHECKED_IN_ENV,
            source_resolver=lambda _target: "https://github.com/caddyserver/caddy",
        )

        self.assertEqual(contexts[0].upstream_repo, "caddyserver/caddy")
        self.assertEqual(contexts[0].links, ())

    def test_unmapped_and_lsio_images_are_unchanged(self) -> None:
        parsed = parse_wud_text("advplyr/audiobookshelf:2.35.1\nlinuxserver/nginx:1.28.0\n")
        with tempfile.TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "wud" / "release-note-upstreams.txt"
            map_path.parent.mkdir()
            # Packaging-repo keys never apply to LinuxServer.io images.
            map_path.write_text(
                "github.com/linuxserver/docker-nginx: nginx/nginx\n",
                encoding="utf-8",
            )
            upstreams = Path(tmp) / "upstreams.txt"
            upstreams.write_text(
                "linuxserver/docker-nginx: linuxserver/docker-nginx\n",
                encoding="utf-8",
            )

            contexts = release_note_contexts(
                parsed.targets,
                {"WUD_APP_DIR": tmp, "UPSTREAM_MAP": str(upstreams)},
                source_resolver=lambda target: (
                    "https://github.com/advplyr/audiobookshelf"
                    if target.line_no == 1
                    else "https://github.com/linuxserver/docker-nginx"
                ),
            )

        self.assertEqual(contexts[0].upstream_repo, "advplyr/audiobookshelf")
        self.assertEqual(contexts[0].links, ())
        self.assertEqual(contexts[1].provider, "lsio")
        self.assertEqual(contexts[1].upstream_repo, "linuxserver/docker-nginx")


class ReleaseNoteUpstreamRefreshTests(unittest.TestCase):
    def test_mapped_upstream_release_keeps_packaging_link(self) -> None:
        parsed = parse_wud_text("nginxinc/nginx-unprivileged:latest\n")
        requested: list[str] = []

        def fetch_json(url: str) -> object:
            requested.append(url)
            return {
                "https://api.github.com/repos/nginx/nginx/releases/latest": _release(
                    "nginx/nginx",
                    "release-1.29.4",
                )
            }[url]

        with open_db(":memory:") as conn:
            init_db(conn)
            items = refresh_release_notes(
                conn,
                parsed.targets,
                CHECKED_IN_ENV,
                client=GitHubClient(fetch_json=fetch_json),
                source_resolver=lambda _target: (
                    "https://github.com/nginxinc/docker-nginx-unprivileged"
                ),
            )
            cached = cached_release_notes(
                conn,
                parsed.targets,
                CHECKED_IN_ENV,
                source_resolver=lambda _target: (
                    "https://github.com/nginxinc/docker-nginx-unprivileged"
                ),
            )

        self.assertEqual(
            requested,
            ["https://api.github.com/repos/nginx/nginx/releases/latest"],
        )
        self.assertEqual(items[0].status, "ready")
        self.assertEqual(items[0].upstream_repo, "nginx/nginx")
        self.assertEqual(items[0].release_tag, "release-1.29.4")
        self.assertEqual(
            [link.kind for link in items[0].links],
            ["github_release", "packaging_repo"],
        )
        self.assertEqual(cached[0].links, items[0].links)

    def test_official_image_fetches_upstream_release(self) -> None:
        parsed = parse_wud_text("caddy:2.11.8\n")
        client = GitHubClient(
            fetch_json=lambda url: {
                "https://api.github.com/repos/caddyserver/caddy/releases/tags/v2.11.9": (
                    _release("caddyserver/caddy", "v2.11.9")
                ),
            }[url]
        )

        with open_db(":memory:") as conn:
            init_db(conn)
            items = refresh_release_notes(
                conn,
                parsed.targets,
                CHECKED_IN_ENV,
                client=client,
                target_tag_resolver=lambda _target: "2.11.9",
            )

        self.assertEqual(items[0].status, "ready")
        self.assertEqual(items[0].upstream_repo, "caddyserver/caddy")
        self.assertEqual([link.kind for link in items[0].links], ["github_release"])

    def test_mapped_upstream_without_matching_tag_keeps_packaging_link(self) -> None:
        # curl tags releases as curl-8_23_0, so an exact 8.23.0 lookup misses.
        parsed = parse_wud_text("curlimages/curl:8.22.0\n")
        responses: dict[str, object] = {
            "https://api.github.com/repos/curl/curl/releases/tags/v8.23.0": None,
            "https://api.github.com/repos/curl/curl/releases/tags/8.23.0": None,
            "https://api.github.com/repos/curl/curl": {
                "html_url": "https://github.com/curl/curl",
            },
        }

        with open_db(":memory:") as conn:
            init_db(conn)
            items = refresh_release_notes(
                conn,
                parsed.targets,
                CHECKED_IN_ENV,
                client=GitHubClient(fetch_json=lambda url: responses[url]),
                source_resolver=lambda _target: "https://github.com/curl/curl-container",
                target_tag_resolver=lambda _target: "8.23.0",
            )

        self.assertEqual(items[0].status, "not_found")
        self.assertEqual(items[0].upstream_repo, "curl/curl")
        self.assertEqual(
            [(link.kind, link.url) for link in items[0].links],
            [
                ("github_project", "https://github.com/curl/curl"),
                ("packaging_repo", "https://github.com/curl/curl-container"),
            ],
        )

    def test_link_only_upstream_needs_no_network_or_cache(self) -> None:
        parsed = parse_wud_text("postgres:17.2\n")
        client = GitHubClient(fetch_json=_no_network)

        with open_db(":memory:") as conn:
            init_db(conn)
            refreshed = refresh_release_notes(
                conn,
                parsed.targets,
                CHECKED_IN_ENV,
                client=client,
                force=True,
            )
            cached = cached_release_notes(conn, parsed.targets, CHECKED_IN_ENV)
            rows = conn.execute("SELECT COUNT(*) FROM release_note_cache").fetchone()[0]

        self.assertEqual(rows, 0)
        self.assertEqual(refreshed, cached)
        item = refreshed[0]
        self.assertEqual(item.provider, LINK_ONLY_PROVIDER)
        self.assertEqual(item.status, "not_found")
        self.assertEqual(item.image_repo, "postgres")
        self.assertEqual(item.upstream_repo, "postgresql.org")
        self.assertEqual(
            item.links,
            [
                ReleaseNoteLink(
                    "Upstream release notes",
                    "https://www.postgresql.org/docs/release/",
                    "upstream_release_notes",
                )
            ],
        )
        self.assertIn("outside GitHub Releases", item.error)


if __name__ == "__main__":
    unittest.main()
