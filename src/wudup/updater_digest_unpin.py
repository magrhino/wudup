"""Shared digest-unpin planning helpers for WebUI plans and apply jobs."""

from __future__ import annotations

from collections.abc import Sequence

from .compose_rewrite import (
    DIGEST_PIN_MARKER_PREFIX,
    WUD_TAG_INCLUDE_LABEL,
    compose_escape_dollars,
)
from .digest_provenance import digest_from_image
from .images import image_with_tag, normalize_digest
from .tag_streams import retag_tag_include_regex
from .updater_models import DigestUnpinUpdate


def digest_unpin_update_from_values(
    *,
    old_image: str,
    resolved_tag: str,
    target_digest: str,
    services: Sequence[str],
) -> DigestUnpinUpdate:
    digest = normalize_digest(target_digest)
    watch_tag = resolved_tag
    return DigestUnpinUpdate(
        old_image=old_image,
        resolved_tag=resolved_tag,
        tag_image=image_with_tag(old_image, resolved_tag),
        current_digest=digest_from_image(old_image),
        target_digest=digest,
        watch_tag=watch_tag,
        marker=f"{DIGEST_PIN_MARKER_PREFIX}{watch_tag}",
        label_key=WUD_TAG_INCLUDE_LABEL,
        # Unpinned images show their tag again, so track its release line.
        label_value=compose_escape_dollars(retag_tag_include_regex(watch_tag)),
        services=tuple(sorted(services)),
    )
