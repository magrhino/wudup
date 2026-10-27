"""Release identity and matching for operator release skips.

A release skip hides one release of a service until WUD offers a different
one. A tag that is a version, optionally followed by a suffix such as
``-rc1`` or ``-alpine``, is identified by that whole version, so ``3.0.0-rc1``
and ``3.0.0`` are different releases. LinuxServer tags drop only their build
suffix and package revision, so rebuilds of one release (``5.3.0-r0-ls412``,
``5.3.0-r1-ls413``) stay skipped. Other tags fall back to the exact tag, and
digest-only updates fall back to the target digest.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .lsio_updates import is_lsio_repo, parse_lsio_tag

ReleaseSkipBasis = Literal["version", "tag", "digest"]
ReleaseSkipScope = Literal["release", "line"]

# A version must be the whole tag, optionally followed by a separated suffix,
# so commit-style tags such as ``3f9e1a2`` are not read as version ``3``.
_VERSION_TAG_RE = re.compile(
    r"^[vV]?(?P<number>\d+(?:\.\d+)*)(?P<suffix>[-_+][0-9A-Za-z][0-9A-Za-z._+-]*)?$",
    re.ASCII,
)
_VERSION_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*", re.ASCII)
_LSIO_PACKAGE_REVISION_RE = re.compile(r"-r\d+$", re.ASCII)
_SHORT_DIGEST_LENGTH = 12


@dataclass(frozen=True)
class ReleaseKey:
    """Identity of one offered release: what a skip compares against."""

    basis: ReleaseSkipBasis
    value: str
    line: str = ""

    @property
    def label(self) -> str:
        if self.basis == "digest":
            return f"build {short_digest(self.value)}"
        return self.value


@dataclass(frozen=True)
class ReleaseSkip:
    """One stored release skip, reduced to the fields matching needs."""

    id: int
    service_key: str
    basis: ReleaseSkipBasis
    value: str
    scope: ReleaseSkipScope
    skipped_tag: str
    reason: str
    created_at: str

    @property
    def label(self) -> str:
        return skip_label(self.basis, self.value, self.scope)


def release_version(image_repo: str, tag: str) -> str:
    """Return the release version a tag names, or ``""``.

    The version keeps any suffix, so ``3.0.0-rc1`` and ``16.4-alpine`` are
    versions of their own. LinuxServer tags drop their build suffix and
    package revision, so ``5.3.0-r0-ls412`` and ``version-5.3.0-r1`` both name
    ``5.3.0``.
    """

    source = tag.strip()
    if is_lsio_repo(image_repo):
        parts = parse_lsio_tag(source)
        if parts.kind in {"build", "version", "pseudo_semver"}:
            source = _LSIO_PACKAGE_REVISION_RE.sub("", parts.upstream_version)
    match = _VERSION_TAG_RE.match(source)
    if match is None:
        return ""
    number = ".".join(str(int(part)) for part in match.group("number").split("."))
    return number + (match.group("suffix") or "")


def release_line(version: str) -> str:
    """Return the major.minor line of a version, or ``""`` when it has one part."""

    match = _VERSION_NUMBER_RE.match(version)
    if match is None:
        return ""
    parts = match.group(0).split(".")
    if len(parts) < 2:
        return ""
    return ".".join(parts[:2])


def release_key(image_repo: str, target_tag: str, digest: str) -> ReleaseKey | None:
    """Return the release a pending update offers.

    A tag change is identified by its version, or by the exact tag when the
    tag has no leading version. A digest-only update is identified by digest.
    """

    tag = target_tag.strip()
    if tag:
        version = release_version(image_repo, tag)
        if version:
            return ReleaseKey("version", version, release_line(version))
        return ReleaseKey("tag", tag)
    target_digest = digest.strip()
    if target_digest:
        return ReleaseKey("digest", target_digest)
    return None


def skip_matches(skip: ReleaseSkip, key: ReleaseKey | None) -> bool:
    """Return whether an offered release is the one the skip holds back."""

    if key is None or key.basis != skip.basis:
        return False
    if skip.scope == "line":
        return bool(key.line) and key.line == release_line(skip.value)
    return key.value == skip.value


def skip_label(basis: str, value: str, scope: str) -> str:
    if basis == "digest":
        return f"build {short_digest(value)}"
    if basis == "version" and scope == "line":
        return f"{release_line(value)}.x"
    return value


def short_digest(digest: str) -> str:
    algorithm, sep, hex_value = digest.partition(":")
    if not sep:
        return digest[:_SHORT_DIGEST_LENGTH]
    return f"{algorithm}:{hex_value[:_SHORT_DIGEST_LENGTH]}"


def skip_from_row(row: Mapping[str, Any] | Any) -> ReleaseSkip:
    return ReleaseSkip(
        id=int(row["id"]),
        service_key=str(row["service_key"]),
        basis=_basis(str(row["basis"])),
        value=str(row["value"]),
        scope=_scope(str(row["scope"])),
        skipped_tag=str(row["skipped_tag"]),
        reason=str(row["reason"]),
        created_at=str(row["created_at"]),
    )


def skips_by_service(skips: Iterable[ReleaseSkip]) -> dict[str, tuple[ReleaseSkip, ...]]:
    """Group skips by service key, newest first within each service."""

    grouped: dict[str, list[ReleaseSkip]] = {}
    for skip in sorted(skips, key=lambda item: (item.created_at, item.id), reverse=True):
        grouped.setdefault(skip.service_key, []).append(skip)
    return {key: tuple(values) for key, values in grouped.items()}


def skip_for_release(
    skips: Mapping[str, tuple[ReleaseSkip, ...]],
    service_keys: Iterable[str],
    key: ReleaseKey | None,
) -> tuple[ReleaseSkip | None, bool]:
    """Return the skip that applies to an offered release and whether it matches.

    A matching skip hides the release. Otherwise the newest skip with the same
    basis is returned unmatched, so the newer release can say what was skipped
    before it.
    """

    candidates = [skip for service_key in service_keys for skip in skips.get(service_key, ())]
    for skip in candidates:
        if skip_matches(skip, key):
            return skip, True
    if key is None:
        return None, False
    previous = [skip for skip in candidates if skip.basis == key.basis]
    if not previous:
        return None, False
    return max(previous, key=lambda item: (item.created_at, item.id)), False


def _basis(value: str) -> ReleaseSkipBasis:
    if value in {"version", "tag", "digest"}:
        return value  # type: ignore[return-value]
    return "tag"


def _scope(value: str) -> ReleaseSkipScope:
    if value == "line":
        return "line"
    return "release"
