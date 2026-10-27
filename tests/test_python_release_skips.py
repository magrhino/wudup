from __future__ import annotations

import pytest

from wudup.release_skips import (
    ReleaseKey,
    ReleaseSkip,
    release_key,
    release_line,
    release_version,
    skip_for_release,
    skip_label,
    skip_matches,
    skips_by_service,
)

LSIO_QBIT = "lscr.io/linuxserver/qbittorrent"


def _skip(
    *,
    skip_id: int = 1,
    service_key: str = "media/qbittorrent",
    basis: str = "version",
    value: str = "5.3.0",
    scope: str = "release",
    created_at: str = "2026-10-26T10:00:00+00:00",
    reason: str = "",
) -> ReleaseSkip:
    return ReleaseSkip(
        id=skip_id,
        service_key=service_key,
        basis=basis,  # type: ignore[arg-type]
        value=value,
        scope=scope,  # type: ignore[arg-type]
        skipped_tag="5.3.0-r0-ls412",
        reason=reason,
        created_at=created_at,
    )


@pytest.mark.parametrize(
    ("image_repo", "tag", "expected"),
    [
        (LSIO_QBIT, "5.3.0-r0-ls412", "5.3.0"),
        (LSIO_QBIT, "5.3.0-r1-ls413", "5.3.0"),
        (LSIO_QBIT, "version-5.3.0-r0", "5.3.0"),
        (LSIO_QBIT, "amd64-5.3.0-r0-ls412", "5.3.0"),
        (LSIO_QBIT, "4.0.0-rc1-ls5", "4.0.0-rc1"),
        (LSIO_QBIT, "release-5.0.3_v2.0.10-ls380", "5.0.3_v2.0.10"),
        (LSIO_QBIT, "latest", ""),
        ("postgres", "16.4-alpine", "16.4-alpine"),
        ("ghcr.io/example/api", "3.0.0-rc1", "3.0.0-rc1"),
        ("debian", "1.2.3-1", "1.2.3-1"),
        ("ghcr.io/example/api", "3f9e1a2", ""),
        ("ghcr.io/example/api", "20241001-abc123", "20241001-abc123"),
        ("ghcr.io/example/api", "1.2.beta", ""),
        ("ghcr.io/example/api", "v2.9.0", "2.9.0"),
        ("ghcr.io/example/api", "2.09.0", "2.9.0"),
        ("redis", "7", "7"),
        ("nginx", "mainline", ""),
        ("nginx", "release-1.2", ""),
    ],
)
def test_release_version_keeps_suffixes_but_drops_lsio_builds(
    image_repo: str,
    tag: str,
    expected: str,
) -> None:
    assert release_version(image_repo, tag) == expected


def test_release_line_needs_two_parts() -> None:
    assert release_line("5.3.0") == "5.3"
    assert release_line("16.4-alpine") == "16.4"
    assert release_line("20241001-abc123") == ""
    assert release_line("7") == ""


def test_release_key_prefers_version_then_tag_then_digest() -> None:
    assert release_key(LSIO_QBIT, "5.3.0-r0-ls412", "sha256:abc") == ReleaseKey(
        "version", "5.3.0", "5.3"
    )
    assert release_key("nginx", "mainline", "sha256:abc") == ReleaseKey("tag", "mainline")
    digest_key = release_key("nginx", "", "sha256:0123456789abcdef0123")
    assert digest_key == ReleaseKey("digest", "sha256:0123456789abcdef0123")
    assert digest_key.label == "build sha256:0123456789ab"
    assert release_key("nginx", " ", "") is None


def test_release_skip_keeps_rebuilds_skipped_and_releases_the_next_patch() -> None:
    skip = _skip()

    assert skip_matches(skip, release_key(LSIO_QBIT, "5.3.0-r0-ls413", ""))
    assert not skip_matches(skip, release_key(LSIO_QBIT, "5.3.1-r0-ls420", ""))
    assert not skip_matches(skip, release_key(LSIO_QBIT, "", "sha256:abc"))
    assert not skip_matches(skip, None)


def test_release_skip_never_hides_a_different_release_with_the_same_prefix() -> None:
    rc_skip = _skip(value=release_key("ghcr.io/example/api", "3.0.0-rc1", "").value)
    hash_skip = _skip(
        basis="tag",
        value=release_key("ghcr.io/example/api", "3f9e1a2", "").value,
    )
    revision_skip = _skip(value=release_key("debian", "1.2.3-1", "").value)

    assert not skip_matches(rc_skip, release_key("ghcr.io/example/api", "3.0.0", ""))
    assert skip_matches(rc_skip, release_key("ghcr.io/example/api", "v3.0.0-rc1", ""))
    assert not skip_matches(hash_skip, release_key("ghcr.io/example/api", "3a7c0d9", ""))
    assert not skip_matches(revision_skip, release_key("debian", "1.2.3-2", ""))


def test_line_skip_holds_the_whole_minor_line() -> None:
    skip = _skip(scope="line")

    assert skip.label == "5.3.x"
    assert skip_matches(skip, release_key(LSIO_QBIT, "5.3.4-r0-ls430", ""))
    assert not skip_matches(skip, release_key(LSIO_QBIT, "5.4.0-r0-ls440", ""))
    assert not skip_matches(skip, release_key(LSIO_QBIT, "6.3.0-r0-ls450", ""))


def test_tag_and_digest_skips_match_exactly() -> None:
    tag_skip = _skip(basis="tag", value="mainline")
    digest_skip = _skip(basis="digest", value="sha256:aaa")

    assert skip_matches(tag_skip, release_key("nginx", "mainline", ""))
    assert not skip_matches(tag_skip, release_key("nginx", "stable", ""))
    assert skip_matches(digest_skip, release_key("nginx", "", "sha256:aaa"))
    assert not skip_matches(digest_skip, release_key("nginx", "", "sha256:bbb"))
    assert skip_label("digest", "sha256:aaa", "release") == "build sha256:aaa"


def test_skip_for_release_returns_match_or_newest_previous_skip() -> None:
    older = _skip(skip_id=1, value="5.3.0", reason="stalls on magnets")
    newer = _skip(
        skip_id=2,
        value="5.3.1",
        created_at="2026-10-30T10:00:00+00:00",
        reason="still broken",
    )
    other_service = _skip(skip_id=3, service_key="media/sonarr", value="5.3.2")
    skips = skips_by_service([older, newer, other_service])

    matched, matches = skip_for_release(
        skips, ["media/qbittorrent"], release_key(LSIO_QBIT, "5.3.0-r0-ls413", "")
    )
    assert (matched, matches) == (older, True)

    previous, matches = skip_for_release(
        skips, ["media/qbittorrent"], release_key(LSIO_QBIT, "5.3.2-r0-ls421", "")
    )
    assert (previous, matches) == (newer, False)

    none, matches = skip_for_release(
        skips, ["media/qbittorrent"], release_key(LSIO_QBIT, "", "sha256:abc")
    )
    assert (none, matches) == (None, False)
