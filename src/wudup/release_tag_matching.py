"""Match GitHub release tags to image tags by version when their formats differ.

Framework-free and network-free: callers list releases and pass them in. Image
tags drop variant suffixes (``-alpine``, ``-slim``, ...); release tags drop
prefixes such as ``v``, ``release-``, ``<repo-name>-``, and ``<repo-name>@``.
Only a single unambiguous match is returned, so a wrong release is never
guessed."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

IMAGE_VARIANT_SUFFIX_RE = re.compile(
    r"[-_](?:alpine|slim|distroless|debian|ubuntu)[0-9.]*$",
    re.IGNORECASE | re.ASCII,
)
RELEASE_PREFIX_RE = re.compile(r"^release[-_]", re.IGNORECASE | re.ASCII)
DOTTED_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+(?![0-9A-Za-z])", re.ASCII)
BUILD_SUFFIX_RE = re.compile(r"^[-+][0-9]+$", re.ASCII)


def image_tag_version(tag: str) -> str:
    """Return the version an image tag names, or "" when it names none."""
    value = tag.strip()
    while True:
        stripped = IMAGE_VARIANT_SUFFIX_RE.sub("", value)
        if stripped == value:
            break
        value = stripped
    return _versioned(_strip_v(value))


def release_tag_version(tag: str, repo: str) -> str:
    """Return the version a release tag names for ``repo``, or "" when none."""
    value = tag.strip()
    name = repo.rsplit("/", 1)[-1]
    if name:
        for separator in ("@", "-", "_"):
            prefix = f"{name}{separator}"
            if value.lower().startswith(prefix.lower()):
                value = value[len(prefix):]
                break
    value = _strip_v(RELEASE_PREFIX_RE.sub("", value))
    if "." not in value and "_" in value:
        value = value.replace("_", ".")
    return _versioned(value)


def match_release_by_version(
    releases: Iterable[Mapping[str, Any]],
    image_tag: str,
    repo: str,
) -> Mapping[str, Any] | None:
    """Return the one release whose tag names the image tag's version.

    An exact version match wins. Only when none exists is a release accepted
    whose version adds a numeric build stamp (``12.4`` -> ``v12.4-202610261649``).
    Two matches at the same level, a tag with no version, or a draft release
    yield ``None``."""
    version = image_tag_version(image_tag).lower()
    if not version:
        return None
    exact: list[Mapping[str, Any]] = []
    build: list[Mapping[str, Any]] = []
    for release in releases:
        if release.get("draft"):
            continue
        candidate = release_tag_version(str(release.get("tag_name") or ""), repo).lower()
        if candidate == version:
            exact.append(release)
        elif candidate.startswith(version) and BUILD_SUFFIX_RE.match(
            candidate[len(version):]
        ):
            build.append(release)
    for matches in (exact, build):
        if matches:
            return matches[0] if len(matches) == 1 else None
    return None


def _strip_v(value: str) -> str:
    return value[1:] if value[:1] in {"v", "V"} and value[1:2].isdigit() else value


def _versioned(value: str) -> str:
    return value if DOTTED_VERSION_RE.match(value) else ""
