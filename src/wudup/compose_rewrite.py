"""Compose rewrite operations and compatibility entrypoints.

This owner chooses approved image/label changes and resolved-tag policy.
YAML mechanics live in compose_source; atomic writes live in compose_persistence.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .compose_persistence import (
    _atomic_replace_compose,
    _backup_compose,  # noqa: F401 - compatibility re-export
    _compose_source_hash,
    restore_compose_backup,  # noqa: F401 - compatibility re-export
)
from .compose_source import (
    _direct_service_config,
    _dump_compose_yaml,
    _empty_detached_service_comment_lists,
    _get_service_label_value,
    _line_start_offsets,
    _load_compose_yaml,
    _prepare_service_labels,
    _reject_yaml_anchor_or_alias_image_value,  # noqa: F401 - compatibility re-export
    _reject_yaml_anchor_or_alias_labels,  # noqa: F401 - compatibility re-export
    _reject_yaml_anchor_or_alias_service_config,
    _rewrite_service_config,
    _service_comment_token_lists,
    _service_comment_tokens,
    _service_image_scalar_span,
    _service_label_source_rewrite,
    _set_service_label_value,
    _unique_image_rewrite,
    _unique_image_span,
)
from .images import image_tag, tag_value_valid
from .naming import (
    DIGEST_PIN_MARKER_PREFIX,
    LEGACY_DIGEST_PIN_MARKER_PREFIX,
)
from .updater_models import (
    AppliedDigestPinUpdate,
    AppliedDigestUnpinUpdate,
    AppliedTagExclusion,
    AppliedTagUpdate,
    ComposeTagRewriteError,
    DigestPinLabelRewrite,
    DigestPinLabelRewriteApproval,
    DigestPinLabelRewriteApprovalRequired,
    DigestPinUpdate,
    DigestUnpinUpdate,
    ResolvedTagMarkerConflictError,
    TagExclusionUpdate,
    TagStreamLabelRewriteApproval,
    TagStreamUpdate,
    TagUpdate,
)

RESOLVED_TAG_MARKER_PREFIXES = (
    DIGEST_PIN_MARKER_PREFIX,
    LEGACY_DIGEST_PIN_MARKER_PREFIX,
)
WUD_TAG_INCLUDE_LABEL = "wud.tag.include"
_JS_REGEX_SPECIAL_RE = re.compile(r"([\\^$.*+?()[\]{}|])")


def _require_update_services(old_image: str, services: Sequence[str]) -> None:
    if not services:
        raise ComposeTagRewriteError(
            f"No compose service was mapped for {old_image}."
        )


def _digest_pin_expected_image(
    service: str,
    current_image: object,
    update: DigestPinUpdate,
) -> str:
    if current_image == update.resolved_image:
        return update.resolved_image
    if current_image == update.old_image:
        return update.old_image
    raise ComposeTagRewriteError(
        f"Service {service} image is {current_image}, expected "
        f"{update.old_image} or {update.resolved_image}."
    )


def _digest_pin_label_rewrite_or_raise(
    *,
    stack_name: str,
    service: str,
    current_image: str,
    current_label_value: str,
    update: DigestPinUpdate,
    approvals: Sequence[DigestPinLabelRewriteApproval],
) -> DigestPinLabelRewrite | None:
    label_rewrite = _digest_pin_label_rewrite(
        stack_name=stack_name,
        service=service,
        current_image=current_image,
        current_label_value=current_label_value,
        update=update,
        approvals=approvals,
    )
    if label_rewrite is None:
        return None
    if label_rewrite.reason != "approval-required":
        return label_rewrite
    raise DigestPinLabelRewriteApprovalRequired(
        service=label_rewrite.service,
        label_key=label_rewrite.label_key,
        current_label_value=label_rewrite.current_label_value,
        planned_tag=label_rewrite.planned_tag,
        proposed_label_value=label_rewrite.proposed_label_value,
        proposed_label_regex=label_rewrite.proposed_label_regex,
    )


def _validate_service_image(
    service: str,
    current_image: object,
    expected_image: str,
) -> None:
    if current_image == expected_image:
        return
    raise ComposeTagRewriteError(
        f"Service {service} image is {current_image}, expected {expected_image}."
    )


def _validate_service_resolved_tag_marker(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
    expected_tag: str,
    *,
    stack_name: str,
) -> None:
    marker_tag = _service_resolved_tag_marker(
        services,
        service,
        service_config,
    )
    if not marker_tag or marker_tag == expected_tag:
        return
    label = f"{stack_name} " if stack_name else ""
    raise ComposeTagRewriteError(
        f"{label}Service {service} resolved-tag marker is "
        f"{marker_tag}, expected {expected_tag}."
    )


def apply_compose_tag_updates(
    compose_path: Path,
    updates: Sequence[TagUpdate],
    *,
    tag_stream_updates: Sequence[TagStreamUpdate] = (),
    stack_name: str = "",
    written_hashes: list[str] | None = None,
    expected_source_hash: str | None = None,
) -> tuple[AppliedTagUpdate, ...]:
    if not updates:
        return ()

    source_hash = expected_source_hash or _compose_source_hash(compose_path)
    if tag_stream_updates:
        rendered, applied = render_compose_tag_stream_updates(
            compose_path,
            updates,
            tag_stream_updates=tag_stream_updates,
            stack_name=stack_name,
        )
        _atomic_replace_compose(
            compose_path, rendered, prefix="tag-stream", expected_source_hash=source_hash,
            written_hashes=written_hashes,
        )
        return applied

    source, _yaml, _parsed, services = _load_compose_yaml(compose_path)
    line_offsets = _line_start_offsets(source)
    spans: list[tuple[int, int, str, TagUpdate]] = []
    counts = {id(update): 0 for update in updates}
    seen_spans: set[tuple[int, int]] = set()

    for update in updates:
        _require_update_services(update.old_image, update.services)
        for service in update.services:
            _rewrite_service_config(
                services,
                service,
                direct_image_required=False,
            )
            span_start, span_end, replacement_prefix = _unique_image_rewrite(
                services,
                service,
                update.old_image,
                source,
                line_offsets,
                seen_spans,
            )
            spans.append(
                (
                    span_start,
                    span_end,
                    f"{replacement_prefix}{update.new_image}",
                    update,
                )
            )
            counts[id(update)] += 1

    rendered = source
    for start, end, replacement, _update in sorted(
        spans,
        key=lambda item: item[0],
        reverse=True,
    ):
        rendered = f"{rendered[:start]}{replacement}{rendered[end:]}"

    applied = tuple(
        AppliedTagUpdate(
            old_image=update.old_image,
            desired_tag=update.desired_tag,
            new_image=update.new_image,
            services=update.services,
            replacements=counts[id(update)],
        )
        for update in updates
    )
    if any(item.replacements < 1 for item in applied):
        return ()

    _atomic_replace_compose(
        compose_path, rendered, prefix="tag", expected_source_hash=source_hash,
        written_hashes=written_hashes,
    )
    return applied


def plan_compose_tag_stream_update(
    compose_path: Path,
    *,
    line_no: int,
    stack_name: str,
    stack_directory: str,
    service: str,
    current_image: str,
    current_tag: str,
    reported_tag: str,
    selected_tag: str,
    decision: str,
    proposed_label_regex: str,
    approvals: Sequence[TagStreamLabelRewriteApproval] = (),
) -> TagStreamUpdate:
    """Inspect one service and return its exact stream-label rewrite plan."""

    _source, _yaml, _parsed, services = _load_compose_yaml(compose_path)
    service_config = _rewrite_service_config(
        services,
        service,
        direct_image_required=True,
    )
    _validate_service_image(service, service_config.get("image"), current_image)
    _prepare_service_labels(services, service, service_config)
    current_label_value = compose_unescape_dollars(
        _get_service_label_value(service_config, WUD_TAG_INCLUDE_LABEL)
    )
    proposed_label_value = compose_escape_dollars(proposed_label_regex)

    if not current_label_value:
        approved, reason = True, "label-added"
    elif current_label_value == proposed_label_regex:
        approved, reason = True, "label-matches"
    elif current_label_value == exact_tags_regex((current_tag,)) or (
        tag_value_valid(current_label_value) and current_label_value == current_tag
    ):
        approved, reason = True, "exact-tag-normalized"
    else:
        approved = any(
            _tag_stream_label_rewrite_approval_matches(
                approval,
                line_no=line_no,
                stack_name=stack_name,
                stack_directory=stack_directory,
                compose_file=compose_path.name,
                service=service,
                current_label_value=current_label_value,
                selected_tag=selected_tag,
                proposed_label_value=proposed_label_value,
            )
            for approval in approvals
        )
        reason = "approved" if approved else "approval-required"

    return TagStreamUpdate(
        line_no=line_no,
        stack=stack_name,
        stack_directory=stack_directory,
        compose_file=compose_path.name,
        service=service,
        current_tag=current_tag,
        reported_tag=reported_tag,
        selected_tag=selected_tag,
        decision=decision,
        label_key=WUD_TAG_INCLUDE_LABEL,
        current_label_value=current_label_value,
        proposed_label_value=proposed_label_value,
        proposed_label_regex=proposed_label_regex,
        approved=approved,
        reason=reason,
    )


def render_compose_tag_stream_updates(
    compose_path: Path,
    updates: Sequence[TagUpdate],
    *,
    tag_stream_updates: Sequence[TagStreamUpdate],
    stack_name: str = "",
) -> tuple[str, tuple[AppliedTagUpdate, ...]]:
    """Render tag image and WUD stream-label changes into one document."""

    source, _yaml, _parsed, services = _load_compose_yaml(compose_path, width=4096)
    line_offsets = _line_start_offsets(source)
    counts = {id(update): 0 for update in updates}
    seen_spans: set[tuple[int, int]] = set()
    rewrites: list[tuple[int, int, str]] = []
    stream_by_service = _tag_stream_updates_by_service(
        compose_path,
        tag_stream_updates,
        stack_name=stack_name,
    )

    rewritten_services: set[str] = set()
    for update in updates:
        _require_update_services(update.old_image, update.services)
        for service in update.services:
            service_config = _rewrite_service_config(
                services,
                service,
                direct_image_required=True,
            )
            _validate_service_image(service, service_config.get("image"), update.old_image)
            image_start, image_end, replacement_prefix = _unique_image_rewrite(
                services,
                service,
                update.old_image,
                source,
                line_offsets,
                seen_spans,
            )
            rewrites.append(
                (image_start, image_end, f"{replacement_prefix}{update.new_image}")
            )
            stream_update = stream_by_service.get(service)
            if stream_update is not None and _rewrite_tag_stream_label(
                services,
                service,
                service_config,
                update,
                stream_update,
            ):
                rewritten_services.add(service)
                rewrites.extend(
                    _service_label_source_rewrite(
                        service_config,
                        stream_update.label_key,
                        stream_update.proposed_label_value,
                        source,
                        line_offsets,
                    )
                )
            counts[id(update)] += 1

    missing = sorted(set(stream_by_service) - rewritten_services)
    if missing:
        raise ComposeTagRewriteError(
            "Tag stream update did not match selected service(s): " + ", ".join(missing)
        )
    applied = tuple(
        AppliedTagUpdate(
            old_image=update.old_image,
            desired_tag=update.desired_tag,
            new_image=update.new_image,
            services=update.services,
            replacements=counts[id(update)],
        )
        for update in updates
    )
    if any(item.replacements < 1 for item in applied):
        raise ComposeTagRewriteError("Compose tag stream rewrite produced no output.")
    rendered = source
    for start, end, replacement in sorted(rewrites, reverse=True):
        rendered = f"{rendered[:start]}{replacement}{rendered[end:]}"
    return rendered, applied


def _rewrite_tag_stream_label(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
    update: TagUpdate,
    stream_update: TagStreamUpdate | None,
) -> bool:
    if stream_update is None:
        return False
    if stream_update.selected_tag != update.desired_tag:
        raise ComposeTagRewriteError(
            f"Service {service} tag stream selected {stream_update.selected_tag}, "
            f"expected {update.desired_tag}."
        )
    _prepare_service_labels(services, service, service_config)
    current_label = compose_unescape_dollars(
        _get_service_label_value(service_config, stream_update.label_key)
    )
    if current_label != stream_update.current_label_value:
        raise ComposeTagRewriteError(
            f"Service {service} {stream_update.label_key} changed since planning."
        )
    return True


def _tag_stream_updates_by_service(
    compose_path: Path,
    updates: Sequence[TagStreamUpdate],
    *,
    stack_name: str,
) -> dict[str, TagStreamUpdate]:
    compose_directory = compose_path.parent.resolve(strict=False)
    by_service: dict[str, TagStreamUpdate] = {}
    for update in updates:
        if update.stack != stack_name:
            raise ComposeTagRewriteError(
                f"Tag stream update targets stack {update.stack}, expected {stack_name}."
            )
        if Path(update.stack_directory).resolve(strict=False) != compose_directory:
            raise ComposeTagRewriteError(
                f"Tag stream update for service {update.service} targets "
                "a different Compose directory."
            )
        if update.compose_file != compose_path.name:
            raise ComposeTagRewriteError(
                f"Tag stream update for service {update.service} targets "
                "a different Compose file."
            )
        if not update.approved:
            raise ComposeTagRewriteError(
                f"Service {update.service} tag stream label rewrite is not approved."
            )
        if update.service in by_service:
            existing = by_service[update.service]
            if replace(update, line_no=existing.line_no) == existing:
                continue
            raise ComposeTagRewriteError(
                f"Service {update.service} has more than one tag stream update."
            )
        by_service[update.service] = update
    return by_service


def apply_compose_tag_exclusions(
    compose_path: Path,
    updates: Sequence[TagExclusionUpdate],
    *,
    existing_exact_tags: Mapping[str, set[str]],
) -> tuple[AppliedTagExclusion, ...]:
    """Write WUD exact-tag exclusions into service labels."""

    source_hash = _compose_source_hash(compose_path)
    rendered, applied = render_compose_tag_exclusions(
        compose_path,
        updates,
        existing_exact_tags=existing_exact_tags,
    )
    if applied:
        _atomic_replace_compose(
            compose_path, rendered, prefix="exclude", expected_source_hash=source_hash,
        )
    return applied


def apply_compose_digest_pins(
    compose_path: Path,
    updates: Sequence[DigestPinUpdate],
    *,
    label_rewrite_approvals: Sequence[DigestPinLabelRewriteApproval] = (),
    tag_stream_updates: Sequence[TagStreamUpdate] = (),
    stack_name: str = "",
    written_hashes: list[str] | None = None,
    expected_source_hash: str | None = None,
) -> tuple[AppliedDigestPinUpdate, ...]:
    """Write final digest-pinned images plus WUD watch metadata."""

    source_hash = expected_source_hash or _compose_source_hash(compose_path)
    rendered, applied = render_compose_digest_pins(
        compose_path,
        updates,
        label_rewrite_approvals=label_rewrite_approvals,
        tag_stream_updates=tag_stream_updates,
        stack_name=stack_name,
    )
    if updates and not rendered:
        raise ComposeTagRewriteError("Compose digest-pin rewrite produced no output.")
    _atomic_replace_compose(
        compose_path, rendered, prefix="digest-pin", expected_source_hash=source_hash,
        written_hashes=written_hashes,
    )
    return applied


def apply_compose_retag_updates(
    compose_path: Path,
    updates: Sequence[DigestPinUpdate],
    *,
    stack_name: str = "",
    written_hashes: list[str] | None = None,
    expected_source_hash: str | None = None,
) -> tuple[AppliedDigestPinUpdate, ...]:
    """Write retagged images plus exact WUD tag tracking metadata."""

    source_hash = expected_source_hash or _compose_source_hash(compose_path)
    rendered, applied = render_compose_retag_updates(
        compose_path,
        updates,
        stack_name=stack_name,
    )
    if updates and not rendered:
        raise ComposeTagRewriteError("Compose retag rewrite produced no output.")
    _atomic_replace_compose(
        compose_path, rendered, prefix="retag", expected_source_hash=source_hash,
        written_hashes=written_hashes,
    )
    return applied


def apply_compose_digest_unpins(
    compose_path: Path,
    updates: Sequence[DigestUnpinUpdate],
    *,
    stack_name: str = "",
    written_hashes: list[str] | None = None,
    expected_source_hash: str | None = None,
) -> tuple[AppliedDigestUnpinUpdate, ...]:
    """Rewrite digest-pinned images back to tag images plus WUD watch metadata."""

    source_hash = expected_source_hash or _compose_source_hash(compose_path)
    rendered, applied = render_compose_digest_unpins(
        compose_path,
        updates,
        stack_name=stack_name,
    )
    if updates and not rendered:
        raise ComposeTagRewriteError("Compose digest-unpin rewrite produced no output.")
    _atomic_replace_compose(
        compose_path, rendered, prefix="digest-unpin", expected_source_hash=source_hash,
        written_hashes=written_hashes,
    )
    return applied


def render_compose_digest_pins(
    compose_path: Path,
    updates: Sequence[DigestPinUpdate],
    *,
    label_rewrite_approvals: Sequence[DigestPinLabelRewriteApproval] = (),
    tag_stream_updates: Sequence[TagStreamUpdate] = (),
    stack_name: str = "",
) -> tuple[str, tuple[AppliedDigestPinUpdate, ...]]:
    """Return Compose YAML with digest-pin image and watch metadata applied."""

    return _render_compose_retag_updates(
        compose_path,
        updates,
        label_rewrite_approvals=label_rewrite_approvals,
        tag_stream_updates=tag_stream_updates,
        stack_name=stack_name,
    )


def render_compose_retag_updates(
    compose_path: Path,
    updates: Sequence[DigestPinUpdate],
    *,
    stack_name: str = "",
) -> tuple[str, tuple[AppliedDigestPinUpdate, ...]]:
    """Return Compose YAML with tag retags and WUD watch metadata applied."""

    return _render_compose_retag_updates(
        compose_path,
        updates,
        stack_name=stack_name,
    )


def _render_compose_retag_updates(
    compose_path: Path,
    updates: Sequence[DigestPinUpdate],
    *,
    label_rewrite_approvals: Sequence[DigestPinLabelRewriteApproval] = (),
    tag_stream_updates: Sequence[TagStreamUpdate] = (),
    stack_name: str = "",
) -> tuple[str, tuple[AppliedDigestPinUpdate, ...]]:

    if not updates:
        return compose_path.read_text(encoding="utf-8"), ()

    source, yaml, parsed, services = _load_compose_yaml(compose_path, width=4096)
    line_offsets = _line_start_offsets(source)
    counts = {id(update): 0 for update in updates}
    label_rewrites = {id(update): [] for update in updates}
    seen_spans: set[tuple[int, int]] = set()
    stream_by_service = _tag_stream_updates_by_service(
        compose_path,
        tag_stream_updates,
        stack_name=stack_name,
    )
    rewritten_stream_services: set[str] = set()

    for update in updates:
        _require_update_services(update.old_image, update.services)
        for service in update.services:
            service_config = _rewrite_service_config(
                services,
                service,
                direct_image_required=True,
            )
            current_image = service_config.get("image")
            expected_image = _digest_pin_expected_image(service, current_image, update)
            _unique_image_span(
                services,
                service,
                expected_image,
                source,
                line_offsets,
                seen_spans,
                selected_image=update.old_image,
            )

            _prepare_service_labels(services, service, service_config)
            current_include = compose_unescape_dollars(
                _get_service_label_value(service_config, update.label_key)
            )
            stream_update = stream_by_service.get(service)
            next_label_value, label_rewrite, stream_rewritten = (
                _retag_label_rewrite(
                    stack_name=stack_name,
                    service=service,
                    current_image=str(current_image),
                    current_label_value=current_include,
                    update=update,
                    stream_update=stream_update,
                    approvals=label_rewrite_approvals,
                )
            )
            if label_rewrite is not None:
                label_rewrites[id(update)].append(label_rewrite)
            if stream_rewritten:
                rewritten_stream_services.add(service)
            _set_service_label_value(
                service_config,
                update.label_key,
                next_label_value,
            )
            service_config["image"] = update.final_image
            _update_service_resolved_tag_marker(
                services,
                service,
                service_config,
                update.marker,
            )
            counts[id(update)] += 1

    missing_stream_services = sorted(set(stream_by_service) - rewritten_stream_services)
    if missing_stream_services:
        raise ComposeTagRewriteError(
            "Tag stream update did not match digest-pin service(s): "
            + ", ".join(missing_stream_services)
        )

    applied = tuple(
        AppliedDigestPinUpdate(
            old_image=update.old_image,
            resolved_tag=update.resolved_tag,
            resolved_image=update.resolved_image,
            planned_digest=update.planned_digest,
            final_image=update.final_image,
            watch_tag=update.watch_tag,
            marker=update.marker,
            label_key=update.label_key,
            label_value=update.label_value,
            services=update.services,
            replacements=counts[id(update)],
            label_rewrites=tuple(label_rewrites[id(update)]),
        )
        for update in updates
    )
    if any(item.replacements < 1 for item in applied):
        return "", ()
    return _dump_compose_yaml(yaml, parsed), applied


def _retag_label_rewrite(
    *,
    stack_name: str,
    service: str,
    current_image: str,
    current_label_value: str,
    update: DigestPinUpdate,
    stream_update: TagStreamUpdate | None,
    approvals: Sequence[DigestPinLabelRewriteApproval],
) -> tuple[str, DigestPinLabelRewrite | None, bool]:
    if stream_update is None:
        return (
            update.label_value,
            _digest_pin_label_rewrite_or_raise(
                stack_name=stack_name,
                service=service,
                current_image=current_image,
                current_label_value=current_label_value,
                update=update,
                approvals=approvals,
            ),
            False,
        )
    if (
        stream_update.current_tag != image_tag(update.old_image)
        or stream_update.selected_tag != update.watch_tag
        or stream_update.label_key != update.label_key
    ):
        raise ComposeTagRewriteError(
            f"Service {service} tag stream update does not match "
            "the digest-pin update."
        )
    if current_label_value not in {
        stream_update.current_label_value,
        stream_update.proposed_label_regex,
    }:
        raise ComposeTagRewriteError(
            f"Service {service} {stream_update.label_key} changed since planning."
        )
    return stream_update.proposed_label_value, None, True


def render_compose_digest_unpins(
    compose_path: Path,
    updates: Sequence[DigestUnpinUpdate],
    *,
    stack_name: str = "",
) -> tuple[str, tuple[AppliedDigestUnpinUpdate, ...]]:
    """Return Compose YAML with digest-pinned images rewritten to tag images."""

    if not updates:
        return compose_path.read_text(encoding="utf-8"), ()

    source, yaml, parsed, services = _load_compose_yaml(compose_path)
    line_offsets = _line_start_offsets(source)
    counts = {id(update): 0 for update in updates}
    seen_spans: set[tuple[int, int]] = set()

    for update in updates:
        _require_update_services(update.old_image, update.services)
        for service in update.services:
            service_config = _rewrite_service_config(
                services,
                service,
                direct_image_required=True,
            )
            current_image = service_config.get("image")
            _validate_service_image(service, current_image, update.old_image)
            _unique_image_span(
                services,
                service,
                update.old_image,
                source,
                line_offsets,
                seen_spans,
                selected_image=update.old_image,
            )

            _validate_service_resolved_tag_marker(
                services,
                service,
                service_config,
                update.watch_tag,
                stack_name=stack_name,
            )

            _prepare_service_labels(services, service, service_config)
            current_include = _get_service_label_value(service_config, update.label_key)
            _validate_digest_unpin_include(
                stack_name=stack_name,
                service=service,
                current_label_value=current_include,
                update=update,
            )
            _set_service_label_value(
                service_config,
                update.label_key,
                update.label_value,
            )
            _remove_service_resolved_tag_marker(
                services,
                service,
                service_config,
                update.marker,
            )
            service_config["image"] = update.tag_image
            counts[id(update)] += 1

    applied = tuple(
        AppliedDigestUnpinUpdate(
            old_image=update.old_image,
            resolved_tag=update.resolved_tag,
            tag_image=update.tag_image,
            current_digest=update.current_digest,
            target_digest=update.target_digest,
            watch_tag=update.watch_tag,
            marker=update.marker,
            label_key=update.label_key,
            label_value=update.label_value,
            services=update.services,
            replacements=counts[id(update)],
        )
        for update in updates
    )
    if any(item.replacements < 1 for item in applied):
        return "", ()
    return _dump_compose_yaml(yaml, parsed), applied


def render_compose_tag_exclusions(
    compose_path: Path,
    updates: Sequence[TagExclusionUpdate],
    *,
    existing_exact_tags: Mapping[str, set[str]],
) -> tuple[str, tuple[AppliedTagExclusion, ...]]:
    """Return Compose YAML with WUD exact-tag exclusions applied."""

    if not updates:
        return compose_path.read_text(encoding="utf-8"), ()

    source, yaml, parsed, services = _load_compose_yaml(compose_path)
    line_offsets = _line_start_offsets(source)
    service_tags: dict[str, set[str]] = {}
    service_repos: dict[str, str] = {}
    service_images: dict[str, str] = {}
    for update in updates:
        _service_image_scalar_span(
            services,
            update.service,
            update.image,
            source,
            line_offsets,
        )
        service_tags.setdefault(update.service, set()).add(update.tag)
        service_repos[update.service] = update.image_repo
        service_images[update.service] = update.image

    applied: list[AppliedTagExclusion] = []
    for service in sorted(service_tags):
        service_config = services.get(service)
        if not isinstance(service_config, CommentedMap):
            raise ComposeTagRewriteError(
                f"Service {service} is not a mapping with direct labels."
            )
        _reject_yaml_anchor_or_alias_service_config(services, service, service_config)
        _prepare_service_labels(services, service, service_config)
        existing_tags = set(existing_exact_tags.get(service, set()))
        new_tags = existing_tags | service_tags[service]
        current_value = _get_service_label_value(service_config, "wud.tag.exclude")
        current_regex = compose_unescape_dollars(current_value)
        previous_managed = exact_tags_regex(existing_tags)
        next_managed = exact_tags_regex(new_tags)
        next_regex = merge_wud_exclude_regex(
            current_regex,
            previous_managed=previous_managed,
            next_managed=next_managed,
        )
        if current_regex == next_regex:
            continue
        _set_service_label_value(
            service_config,
            "wud.tag.exclude",
            compose_escape_dollars(next_regex),
        )
        applied.append(
            AppliedTagExclusion(
                service=service,
                image_repo=service_repos[service],
                tags=tuple(sorted(new_tags)),
            )
        )

    return _dump_compose_yaml(yaml, parsed), tuple(applied)


def service_resolved_tag_marker(
    compose_path: Path,
    service: str,
    *,
    expected_image: str = "",
) -> str:
    """Return the WUD digest-pin resolved tag marker for one service, if present."""

    _source, _yaml, _parsed, services = _load_compose_yaml(compose_path)
    service_config = _direct_service_config(services, service)
    if expected_image and service_config.get("image") != expected_image:
        raise ComposeTagRewriteError(
            f"Service {service} image is {service_config.get('image')}, "
            f"expected {expected_image}."
        )
    return _service_resolved_tag_marker(services, service, service_config)


def _is_simple_exact_tag_include(value: str) -> bool:
    if len(value) < 3 or not value.startswith("^") or not value.endswith("$"):
        return False
    tag_chars: list[str] = []
    index = 1
    end = len(value) - 1
    while index < end:
        char = value[index]
        if char == "\\":
            index += 1
            if index >= end:
                return False
            escaped = value[index]
            if escaped not in "\\^$.*+?()[]{}|":
                return False
            tag_chars.append(escaped)
            index += 1
            continue
        if char in "\\^$.*+?()[]{}|":
            return False
        tag_chars.append(char)
        index += 1
    return tag_value_valid("".join(tag_chars))


def _digest_pin_label_rewrite(
    *,
    stack_name: str,
    service: str,
    current_image: str,
    current_label_value: str,
    update: DigestPinUpdate,
    approvals: Sequence[DigestPinLabelRewriteApproval],
) -> DigestPinLabelRewrite | None:
    if not current_label_value:
        return None

    proposed_label_regex = compose_unescape_dollars(update.label_value)
    if current_label_value == proposed_label_regex:
        return None

    if _is_simple_exact_tag_include(current_label_value):
        return DigestPinLabelRewrite(
            service=service,
            label_key=update.label_key,
            current_label_value=current_label_value,
            planned_tag=update.watch_tag,
            proposed_label_value=update.label_value,
            proposed_label_regex=proposed_label_regex,
            approved=False,
            reason="exact-regex-normalized",
        )

    if tag_value_valid(current_label_value) and current_label_value in {
        update.watch_tag,
        image_tag(current_image),
    }:
        return DigestPinLabelRewrite(
            service=service,
            label_key=update.label_key,
            current_label_value=current_label_value,
            planned_tag=update.watch_tag,
            proposed_label_value=update.label_value,
            proposed_label_regex=proposed_label_regex,
            approved=False,
            reason="plain-tag-normalized",
        )

    approved = any(
        _digest_pin_label_rewrite_approval_matches(
            approval,
            stack_name=stack_name,
            service=service,
            label_key=update.label_key,
            current_label_value=current_label_value,
            planned_tag=update.watch_tag,
            proposed_label_value=update.label_value,
        )
        for approval in approvals
    )
    return DigestPinLabelRewrite(
        service=service,
        label_key=update.label_key,
        current_label_value=current_label_value,
        planned_tag=update.watch_tag,
        proposed_label_value=update.label_value,
        proposed_label_regex=proposed_label_regex,
        approved=approved,
        reason="approved" if approved else "approval-required",
    )


def _validate_digest_unpin_include(
    *,
    stack_name: str,
    service: str,
    current_label_value: str,
    update: DigestUnpinUpdate,
) -> None:
    if not current_label_value:
        return
    current_regex = compose_unescape_dollars(current_label_value)
    expected_regex = exact_tags_regex((update.watch_tag,))
    if current_regex == expected_regex:
        return
    if tag_value_valid(current_regex) and current_regex == update.watch_tag:
        return
    label = f"{stack_name} " if stack_name else ""
    raise ComposeTagRewriteError(
        f'{label}Service {service} {update.label_key} is "{current_regex}", '
        f'expected "{expected_regex}" for digest unpin.'
    )


def _service_resolved_tag_marker(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
) -> str:
    values: set[str] = set()
    for token in _service_comment_tokens(services, service, service_config):
        values.update(_comment_token_resolved_tag_markers(token))
    if not values:
        return ""
    if len(values) > 1:
        raise ResolvedTagMarkerConflictError(
            service=service,
            tags=tuple(sorted(values)),
        )
    tag = next(iter(values))
    if not tag_value_valid(tag):
        raise ComposeTagRewriteError(
            f"Service {service} resolved-tag marker has invalid tag {tag}."
        )
    return tag


def _update_service_resolved_tag_marker(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
    marker: str,
) -> None:
    _clear_service_resolved_tag_markers(services, service, service_config)
    if marker:
        service_config.yaml_set_comment_before_after_key("image", before=marker)


def _remove_service_resolved_tag_marker(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
    marker: str,
) -> None:
    for token_list in _service_comment_token_lists(services, service, service_config):
        kept = []
        for token in token_list.tokens:
            if _comment_token_matches_marker(token, marker):
                continue
            kept.append(token)
        token_list.replace(kept)
    _empty_detached_service_comment_lists(services, service, service_config)
    remaining_marker = _service_resolved_tag_marker(services, service, service_config)
    if remaining_marker:
        raise ComposeTagRewriteError(
            f"Service {service} resolved-tag marker is attached ambiguously."
        )


def _clear_service_resolved_tag_markers(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
) -> None:
    for token_list in _service_comment_token_lists(services, service, service_config):
        token_list.replace(
            [
                token
                for token in token_list.tokens
                if _clear_comment_token_resolved_tag_markers(token)
            ]
        )
    _empty_detached_service_comment_lists(services, service, service_config)
    if _service_resolved_tag_marker(services, service, service_config):
        raise ComposeTagRewriteError(
            f"Service {service} resolved-tag marker is attached ambiguously."
        )


def _comment_token_resolved_tag_markers(token: object) -> set[str]:
    values: set[str] = set()
    for line in str(getattr(token, "value", "")).splitlines():
        marker_value = _comment_line_resolved_tag_marker(line)
        if marker_value:
            values.add(marker_value)
    return values


def _clear_comment_token_resolved_tag_markers(token: object) -> bool:
    value = str(getattr(token, "value", ""))
    token.value = "".join(
        line
        for line in value.splitlines(keepends=True)
        if _comment_line_resolved_tag_marker(line) is None
    )
    return bool(token.value)


def _comment_line_resolved_tag_marker(line: str) -> str | None:
    text = line.strip()
    if text.startswith("#"):
        text = text[1:].strip()
    for prefix in RESOLVED_TAG_MARKER_PREFIXES:
        if text.startswith(prefix):
            return text.removeprefix(prefix).strip()
    return None


def _comment_token_matches_marker(token: object, marker: str) -> bool:
    text = str(getattr(token, "value", "")).strip()
    cleaned = text[1:].strip() if text.startswith("#") else text
    if cleaned == marker:
        return True
    for prefix in RESOLVED_TAG_MARKER_PREFIXES:
        if marker.startswith(prefix):
            marker_value = marker.removeprefix(prefix)
            return any(
                cleaned == f"{candidate_prefix}{marker_value}"
                for candidate_prefix in RESOLVED_TAG_MARKER_PREFIXES
            )
    return False


def _digest_pin_label_rewrite_approval_matches(
    approval: DigestPinLabelRewriteApproval,
    *,
    stack_name: str,
    service: str,
    label_key: str,
    current_label_value: str,
    planned_tag: str,
    proposed_label_value: str,
) -> bool:
    return (
        approval.stack == stack_name
        and approval.service == service
        and approval.label_key == label_key
        and approval.current_label_value == current_label_value
        and approval.planned_tag == planned_tag
        and approval.proposed_label_value == proposed_label_value
    )


def _tag_stream_label_rewrite_approval_matches(
    approval: TagStreamLabelRewriteApproval,
    *,
    line_no: int,
    stack_name: str,
    stack_directory: str,
    compose_file: str,
    service: str,
    current_label_value: str,
    selected_tag: str,
    proposed_label_value: str,
) -> bool:
    return (
        approval.line_no == line_no
        and approval.stack == stack_name
        and approval.stack_directory == stack_directory
        and approval.compose_file == compose_file
        and approval.service == service
        and approval.label_key == WUD_TAG_INCLUDE_LABEL
        and approval.current_label_value == current_label_value
        and approval.selected_tag == selected_tag
        and approval.proposed_label_value == proposed_label_value
    )


def render_compose_tracking_label(
    compose_path: Path,
    service: str,
    expected_image: str,
    expected_label: str,
    proposed_regex: str,
    *,
    expected_source_hash: str | None = None,
) -> str:
    """Preview a single service's WUD tracking-label change without touching its image."""

    source, yaml, parsed, services = _load_compose_yaml(compose_path, width=4096)
    if expected_source_hash is not None and hashlib.sha256(source.encode()).hexdigest() != expected_source_hash:
        raise ComposeTagRewriteError("Compose file changed before tracking repair; preview it again.")
    service_config = _rewrite_service_config(
        services, service, direct_image_required=True
    )
    if service_config.get("image") != expected_image:
        raise ComposeTagRewriteError(
            f"Service {service} image changed before tracking repair."
        )
    _prepare_service_labels(services, service, service_config)
    labels = service_config.get("labels")
    if isinstance(labels, CommentedSeq) and sum(
        isinstance(entry, str) and entry.partition("=")[0] == WUD_TAG_INCLUDE_LABEL
        for entry in labels
    ) > 1:
        raise ComposeTagRewriteError(
            f"Service {service} has duplicate WUD tag filters; remove the duplicates before repair."
        )
    current = compose_unescape_dollars(
        _get_service_label_value(service_config, WUD_TAG_INCLUDE_LABEL)
    )
    if current != expected_label:
        raise ComposeTagRewriteError(
            f"Service {service} tracking label changed before repair."
        )
    _set_service_label_value(
        service_config,
        WUD_TAG_INCLUDE_LABEL,
        compose_escape_dollars(proposed_regex),
    )
    return _dump_compose_yaml(yaml, parsed)


def apply_compose_tracking_label(
    compose_path: Path,
    service: str,
    expected_image: str,
    expected_label: str,
    proposed_regex: str,
    *,
    expected_source_hash: str | None = None,
) -> None:
    rendered = render_compose_tracking_label(
        compose_path, service, expected_image, expected_label, proposed_regex,
        expected_source_hash=expected_source_hash,
    )
    _atomic_replace_compose(
        compose_path, rendered, prefix="tracking-repair",
        expected_source_hash=expected_source_hash,
    )


def js_regex_escape(value: str) -> str:
    return _JS_REGEX_SPECIAL_RE.sub(r"\\\1", value)


def exact_tags_regex(tags: Iterable[str]) -> str:
    fragments = [js_regex_escape(tag) for tag in sorted(set(tags))]
    if not fragments:
        return ""
    if len(fragments) == 1:
        return f"^{fragments[0]}$"
    return f"^(?:{'|'.join(fragments)})$"


def compose_escape_dollars(value: str) -> str:
    return value.replace("$", "$$")


def compose_unescape_dollars(value: str) -> str:
    return value.replace("$$", "$")


def merge_wud_exclude_regex(
    current_regex: str,
    *,
    previous_managed: str,
    next_managed: str,
) -> str:
    if not next_managed:
        return current_regex
    if not current_regex or current_regex == previous_managed:
        return next_managed
    if current_regex == next_managed:
        return current_regex
    return f"(?:{current_regex})|(?:{next_managed})"


def _exact_tag_include_matches(value: str, tag: str) -> bool:
    return compose_unescape_dollars(value) == exact_tags_regex((tag,))
