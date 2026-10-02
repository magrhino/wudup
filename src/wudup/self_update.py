"""Self-update detection helpers for the ``updates`` wrappers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .banner import (
    current_tag,
    fetch_latest_release_tag,
    release_check_enabled,
    release_update_available,
)
from .container_identity import container_identity_candidates
from .images import image_repo_ref, repo_key, tag_value_valid
from .naming import IMAGE_REPOSITORY, LEGACY_IMAGE_REPOSITORY

DEFAULT_SELF_UPDATE_IMAGE = f"{IMAGE_REPOSITORY}:latest"
DEFAULT_SELF_UPDATE_REPOSITORY = IMAGE_REPOSITORY
SELF_UPDATE_REPOS = frozenset(
    {
        "magrhino/wudup",
        "wudup",
        "magrhino/wud-updater",
        "wud-updater",
        LEGACY_IMAGE_REPOSITORY.removeprefix("ghcr.io/"),
    }
)
LEGACY_SELF_UPDATE_REPOS = frozenset(
    {
        "magrhino/wud-updater",
        "wud-updater",
        LEGACY_IMAGE_REPOSITORY.removeprefix("ghcr.io/"),
    }
)
_SEMVER_IMAGE_TAG_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+].*)?$")
_BARE_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EDGE_IMAGE_TAG_RE = re.compile(r"^edge(?:-[0-9a-f]{7,40})?(?:-trivy)?$")


@dataclass(frozen=True)
class ReleaseSelfUpdate:
    local_tag: str
    latest_tag: str
    target: str


class SelfUpdateInspectionError(RuntimeError):
    """Raised when the running image variant cannot be determined safely."""


def is_self_update_target(value: str) -> bool:
    return repo_key(value).casefold() in SELF_UPDATE_REPOS


def github_release_self_update(
    environ: Mapping[str, str] | None = None,
    *,
    timeout: float = 1.0,
) -> ReleaseSelfUpdate | None:
    env = os.environ if environ is None else environ
    if not release_check_enabled(env):
        return None

    local_tag = current_tag()
    latest_tag = fetch_latest_release_tag(timeout=timeout)
    if not release_update_available(local_tag, latest_tag):
        return None
    latest_tag = latest_tag or ""

    current_image = current_container_image(env)
    if is_edge_image(current_image):
        return None
    if not self_update_image_variant_known(current_image):
        raise SelfUpdateInspectionError(
            "Could not inspect the running WUDup container image; "
            "self-update cannot preserve the image variant."
        )
    return ReleaseSelfUpdate(
        local_tag=local_tag,
        latest_tag=latest_tag,
        target=release_self_update_target(
            current_image,
            local_tag,
            latest_tag,
        ),
    )


def is_edge_image(current_image: str) -> bool:
    """Return whether the running image is an unreleased ``edge`` build of main."""

    return bool(_EDGE_IMAGE_TAG_RE.fullmatch(_image_reference_tag(current_image)))


def self_update_image_variant_known(current_image: str) -> bool:
    """Return whether an inspected image reference preserves its variant tag."""

    if not current_image or _BARE_IMAGE_ID_RE.fullmatch(current_image):
        return False
    return "@sha256:" not in current_image or bool(_image_reference_tag(current_image))


def release_self_update_target(
    current_image: str,
    local_tag: str,
    latest_tag: str,
) -> str:
    if current_image == "":
        if _is_release_image_tag(local_tag) and tag_value_valid(local_tag):
            return f"{DEFAULT_SELF_UPDATE_REPOSITORY}:{local_tag} tag={latest_tag}"
        return DEFAULT_SELF_UPDATE_IMAGE

    current_image = _canonical_self_update_image(current_image)
    current_tag = _image_reference_tag(current_image)
    desired_tag = _desired_release_image_tag(current_tag, latest_tag)
    if _is_release_image_tag(current_tag) and _normalize_tag(current_tag) != desired_tag:
        return f"{current_image} tag={desired_tag}"
    return current_image


def current_container_image(
    environ: Mapping[str, str] | None = None,
    *,
    timeout: float = 5.0,
) -> str:
    env = dict(os.environ if environ is None else environ)
    candidates = container_identity_candidates(env)
    if not candidates:
        return ""

    for candidate in candidates:
        try:
            result = subprocess.run(
                ["docker", "container", "inspect", candidate],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            continue

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return ""
        if not isinstance(payload, list) or not payload:
            return ""
        container = payload[0]
        if not isinstance(container, dict):
            return ""
        return _inspected_container_image(container)
    return ""


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["github-target"]:
        print("Usage: python -m wudup.self_update github-target", file=sys.stderr)
        return 2

    try:
        update = github_release_self_update()
    except SelfUpdateInspectionError as exc:
        print(exc, file=sys.stderr)
        return 1
    if update is not None:
        print(update.target)
    return 0


def _inspected_container_image(container: Mapping[str, object]) -> str:
    config = container.get("Config")
    if isinstance(config, dict):
        image = config.get("Image")
        if isinstance(image, str) and image:
            return image
    image = container.get("Image")
    if isinstance(image, str) and image:
        return image
    return ""


def _image_reference_tag(image: str) -> str:
    without_digest = image.split("@sha256:", 1)[0]
    last = without_digest.rsplit("/", 1)[-1]
    if ":" not in last:
        return ""
    return last.rsplit(":", 1)[1]


def _canonical_self_update_image(image: str) -> str:
    if repo_key(image).casefold() not in LEGACY_SELF_UPDATE_REPOS:
        return image
    repo = image_repo_ref(image)
    suffix = image[len(repo) :]
    return f"{IMAGE_REPOSITORY}{suffix}"


def _is_release_image_tag(tag: str) -> bool:
    return bool(_SEMVER_IMAGE_TAG_RE.fullmatch(tag))


def _desired_release_image_tag(current_tag: str, latest_tag: str) -> str:
    return f"{_normalize_tag(latest_tag)}{_release_variant_suffix(current_tag)}"


def _release_variant_suffix(tag: str) -> str:
    base = tag.split("+", 1)[0]
    match = re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+(?P<variant>-.*)?", base)
    if match is None:
        return ""
    return match.group("variant") or ""


def _normalize_tag(tag: str) -> str:
    return tag if tag.startswith("v") else f"v{tag}"


if __name__ == "__main__":
    raise SystemExit(main())
