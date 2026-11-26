"""Checked-in map from images and packaging repos to their release-note upstream.

Some images name a packaging repo as their source (for example
``curl/curl-container``) whose releases are stubs, and Docker Official Images
name no source at all. ``wud/release-note-upstreams.txt`` points those at the
upstream that publishes the notes: a GitHub repo, or a link for upstreams that
do not publish GitHub releases. Reading the map never touches the network."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .images import image_repo_ref
from .release_note_models import ReleaseNoteContext, ReleaseNoteInfo, ReleaseNoteLink
from .release_note_providers import GITHUB_REPO_RE, _classify_context

RELEASE_NOTE_UPSTREAM_MAP_FILENAME = "release-note-upstreams.txt"
LINK_ONLY_PROVIDER = "link"
GITHUB_KEY_PREFIX = "github.com/"
DOCKER_HUB_HOSTS = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})
IMAGE_KEY_RE = re.compile(
    r"^(?:[a-z0-9.-]+(?::[0-9]+)?/)?[a-z0-9._-]+(?:/[a-z0-9._-]+)*$",
    re.ASCII,
)
LINK_ONLY_URL_RE = re.compile(r"^https://[^\s/]+(?:/\S*)?$", re.ASCII)


@dataclass(frozen=True)
class ReleaseNoteUpstream:
    """Where an image's release notes live: a GitHub repo or a link-only URL."""

    repo: str = ""
    notes_url: str = ""


def load_release_note_upstream_map(
    environ: Mapping[str, str],
) -> dict[str, ReleaseNoteUpstream]:
    """Return the first map file found; invalid lines are skipped.

    ``tests/test-release-note-upstreams-map.sh`` rejects invalid lines in the
    checked-in file, so skipping here only affects hand-edited copies."""

    for path in _map_paths(environ):
        if path.is_file():
            return parse_release_note_upstream_map(path.read_text(encoding="utf-8"))
    return {}


def parse_release_note_upstream_map(text: str) -> dict[str, ReleaseNoteUpstream]:
    mapping: dict[str, ReleaseNoteUpstream] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        raw_key, sep, raw_value = stripped.partition(": ")
        if not sep:
            continue
        key = _normalize_key(raw_key.strip())
        upstream = _parse_value(raw_value.strip())
        if key and upstream is not None:
            mapping[key] = upstream
    return mapping


def resolve_release_note_upstream(
    mapping: Mapping[str, ReleaseNoteUpstream],
    *,
    image: str,
    source_repo: str,
) -> ReleaseNoteUpstream | None:
    """Return the mapped upstream for a discovered repo or, failing that, an image.

    A found GitHub repo is only looked up by its packaging-repo key, so an
    operator's per-container WUD link is never replaced by an image key."""

    if source_repo:
        return mapping.get(f"{GITHUB_KEY_PREFIX}{source_repo.lower()}")
    return mapping.get(image_map_key(image))


def image_map_key(image: str) -> str:
    """Return the map key for an image: Docker Hub refs lose host and library/."""

    ref = image_repo_ref(image.strip()).lower()
    host, sep, rest = ref.partition("/")
    if sep and host in DOCKER_HUB_HOSTS:
        ref = rest
    elif sep and ("." in host or ":" in host or host == "localhost"):
        return ref
    return ref.removeprefix("library/")


def packaging_repo_link(repo: str) -> ReleaseNoteLink:
    return ReleaseNoteLink("Packaging repo", f"https://github.com/{repo}", "packaging_repo")


def link_only_notes_link(url: str) -> ReleaseNoteLink:
    return ReleaseNoteLink("Upstream release notes", url, "upstream_release_notes")


def link_only_source(url: str) -> str:
    """Return the readable source name shown for a link-only upstream."""

    host = urllib.parse.urlsplit(url).hostname or url
    return host.removeprefix("www.")


def link_only_info(context: ReleaseNoteContext) -> ReleaseNoteInfo:
    """Return the network-free result for a link-only upstream."""

    return ReleaseNoteInfo(
        line_no=context.line_no,
        status="not_found",
        provider=context.provider,
        image_repo=context.image_repo,
        upstream_repo=context.upstream_repo,
        title=f"{context.upstream_repo} release notes",
        links=list(context.links),
        error=(
            f"{context.upstream_repo} publishes its release notes outside GitHub "
            "Releases. Open the upstream release notes link to read them."
        ),
        classification=_classify_context(context),
    )


def _normalize_key(key: str) -> str:
    lowered = key.lower()
    if lowered.startswith(GITHUB_KEY_PREFIX):
        repo = lowered.removeprefix(GITHUB_KEY_PREFIX)
        return lowered if GITHUB_REPO_RE.fullmatch(repo) else ""
    if not IMAGE_KEY_RE.fullmatch(lowered):
        return ""
    return image_map_key(lowered)


def _parse_value(value: str) -> ReleaseNoteUpstream | None:
    if GITHUB_REPO_RE.fullmatch(value):
        return ReleaseNoteUpstream(repo=value)
    if LINK_ONLY_URL_RE.fullmatch(value):
        return ReleaseNoteUpstream(notes_url=value)
    return None


def _map_paths(environ: Mapping[str, str]) -> list[Path]:
    paths: list[Path] = []
    app_dir = environ.get("WUD_APP_DIR", "/app")
    if app_dir:
        paths.append(Path(app_dir) / "wud" / RELEASE_NOTE_UPSTREAM_MAP_FILENAME)
    paths.append(Path("/app/wud") / RELEASE_NOTE_UPSTREAM_MAP_FILENAME)
    paths.append(
        Path(__file__).resolve().parents[2] / "wud" / RELEASE_NOTE_UPSTREAM_MAP_FILENAME
    )
    return list(dict.fromkeys(paths))
