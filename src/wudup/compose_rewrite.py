"""Compose YAML rewrite helpers for updater-managed image and label changes."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

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
_UNSUPPORTED_NON_STRING_LABEL_ENTRY = (
    "Service labels use unsupported non-string list entries."
)
_SERVICE_LABELS_SOURCE_LOCATION_UNAVAILABLE = (
    "Service labels source location is unavailable."
)
_UNSUPPORTED_SERVICE_LABELS_YAML = "Service labels use unsupported YAML syntax."


class _CommentTokenList:
    __slots__ = ("_replace", "tokens")

    def __init__(
        self,
        tokens: list[object],
        replace: Callable[[list[object]], None],
    ) -> None:
        self.tokens = tokens
        self._replace = replace

    def replace(self, tokens: list[object]) -> None:
        self._replace(tokens)


def _load_compose_yaml(
    compose_path: Path,
    *,
    width: int | None = None,
) -> tuple[str, YAML, CommentedMap, CommentedMap]:
    source = compose_path.read_text(encoding="utf-8")
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    if width is not None:
        yaml.width = width
    try:
        parsed = yaml.load(source)
    except YAMLError as exc:
        raise ComposeTagRewriteError(
            f"Compose file YAML could not be parsed: {exc}"
        ) from exc
    if not isinstance(parsed, CommentedMap):
        raise ComposeTagRewriteError("Compose file is not a YAML mapping.")
    services = parsed.get("services")
    if not isinstance(services, CommentedMap):
        raise ComposeTagRewriteError("Compose file has no services mapping.")
    return source, yaml, parsed, services


def _dump_compose_yaml(yaml: YAML, parsed: CommentedMap) -> str:
    output = StringIO()
    yaml.dump(parsed, output)
    return output.getvalue()


def _require_update_services(old_image: str, services: Sequence[str]) -> None:
    if not services:
        raise ComposeTagRewriteError(
            f"No compose service was mapped for {old_image}."
        )


def _rewrite_service_config(
    services: CommentedMap,
    service: str,
    *,
    direct_image_required: bool,
) -> CommentedMap:
    service_config = _direct_service_config(services, service)
    _reject_yaml_anchor_or_alias_service_config(
        services,
        service,
        service_config,
    )
    has_direct_image = _commented_map_has_direct_key(service_config, "image")
    if not has_direct_image and (
        direct_image_required or service_config.get("image") is not None
    ):
        raise ComposeTagRewriteError(
            f"Service {service} image is inherited and needs manual review."
        )
    _reject_yaml_anchor_or_alias_image_value(services, service, service_config)
    return service_config


def _unique_image_rewrite(
    services: CommentedMap,
    service: str,
    old_image: str,
    source: str,
    line_offsets: Sequence[int],
    seen_spans: set[tuple[int, int]],
) -> tuple[int, int, str]:
    start, end, replacement_prefix = _service_image_scalar_rewrite(
        services,
        service,
        old_image,
        source,
        line_offsets,
    )
    _reject_duplicate_image_span(seen_spans, (start, end), service, old_image)
    return start, end, replacement_prefix


def _unique_image_span(
    services: CommentedMap,
    service: str,
    expected_image: str,
    source: str,
    line_offsets: Sequence[int],
    seen_spans: set[tuple[int, int]],
    *,
    selected_image: str,
) -> tuple[int, int]:
    span = _service_image_scalar_span(
        services,
        service,
        expected_image,
        source,
        line_offsets,
    )
    _reject_duplicate_image_span(seen_spans, span, service, selected_image)
    return span


def _reject_duplicate_image_span(
    seen_spans: set[tuple[int, int]],
    span: tuple[int, int],
    service: str,
    image: str,
) -> None:
    if span in seen_spans:
        raise ComposeTagRewriteError(
            f"Service {service} image for {image} was selected more than once."
        )
    seen_spans.add(span)


def _prepare_service_labels(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
) -> None:
    _materialize_inherited_service_labels(service_config, service)
    labels = service_config.get("labels")
    if labels is not None:
        _reject_yaml_anchor_or_alias_labels(services, service, labels)


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
) -> tuple[AppliedTagUpdate, ...]:
    if not updates:
        return ()

    if tag_stream_updates:
        rendered, applied = render_compose_tag_stream_updates(
            compose_path,
            updates,
            tag_stream_updates=tag_stream_updates,
            stack_name=stack_name,
        )
        _atomic_replace_compose(compose_path, rendered, prefix="tag-stream")
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

    _atomic_replace_compose(compose_path, rendered, prefix="tag")
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


def _service_label_source_rewrite(
    service_config: CommentedMap,
    key: str,
    value: str,
    source: str,
    line_offsets: Sequence[int],
) -> tuple[tuple[int, int, str], ...]:
    labels = service_config.get("labels")
    if labels is None:
        if _commented_map_has_direct_key(service_config, "labels"):
            return (
                _empty_service_labels_rewrite(
                    service_config,
                    key,
                    value,
                    source,
                    line_offsets,
                ),
            )
        return (
            _new_service_labels_rewrite(
                service_config,
                key,
                value,
                source,
                line_offsets,
            ),
        )

    flow_start = _service_labels_flow_start(labels, source, line_offsets)
    if isinstance(labels, CommentedMap):
        return _mapping_label_source_rewrites(
            service_config,
            labels,
            key,
            value,
            source,
            line_offsets,
            flow_start,
        )
    if isinstance(labels, CommentedSeq):
        return _sequence_label_source_rewrites(
            service_config,
            labels,
            key,
            value,
            source,
            line_offsets,
            flow_start,
        )

    raise ComposeTagRewriteError(_UNSUPPORTED_SERVICE_LABELS_YAML)


def _service_labels_flow_start(
    labels: object,
    source: str,
    line_offsets: Sequence[int],
) -> int | None:
    if not isinstance(labels, (CommentedMap, CommentedSeq)):
        return None
    try:
        collection_start = line_offsets[labels.lc.line] + labels.lc.col
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise ComposeTagRewriteError(
            _SERVICE_LABELS_SOURCE_LOCATION_UNAVAILABLE
        ) from exc
    if collection_start < len(source) and source[collection_start] in "[{":
        return collection_start
    return None


def _mapping_label_source_rewrites(
    service_config: CommentedMap,
    labels: CommentedMap,
    key: str,
    value: str,
    source: str,
    line_offsets: Sequence[int],
    flow_start: int | None,
) -> tuple[tuple[int, int, str], ...]:
    flow = flow_start is not None
    if key in labels:
        if not _commented_map_has_direct_key(labels, key):
            raise ComposeTagRewriteError(
                f"Label {key} is inherited and needs manual review."
            )
        try:
            line_no, col = labels.lc.value(key)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ComposeTagRewriteError(
                f"Label {key} source location is unavailable."
            ) from exc
        current = labels[key]
        if not isinstance(current, str):
            raise ComposeTagRewriteError(f"Label {key} is not a string value.")
        return (
            _yaml_scalar_source_rewrite(
                source,
                line_offsets,
                line_no,
                col,
                current,
                value,
                flow=flow,
            ),
        )
    entry = f"{key}: {_render_yaml_scalar_like('', value, flow=flow)}"
    if flow_start is not None:
        return _flow_label_addition_rewrites(
            labels,
            entry,
            source,
            line_offsets,
            flow_start,
        )
    return (
        _append_block_label_rewrite(
            service_config,
            labels,
            entry,
            source,
            line_offsets,
        ),
    )


def _sequence_label_source_rewrites(
    service_config: CommentedMap,
    labels: CommentedSeq,
    key: str,
    value: str,
    source: str,
    line_offsets: Sequence[int],
    flow_start: int | None,
) -> tuple[tuple[int, int, str], ...]:
    replacement = f"{key}={value}"
    flow = flow_start is not None
    for index, item in enumerate(labels):
        if not isinstance(item, str):
            raise ComposeTagRewriteError(_UNSUPPORTED_NON_STRING_LABEL_ENTRY)
        label_key, sep, _label_value = item.partition("=")
        if sep and label_key == key:
            try:
                line_no, col = labels.lc.item(index)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise ComposeTagRewriteError(
                    f"Label {key} source location is unavailable."
                ) from exc
            return (
                _yaml_scalar_source_rewrite(
                    source,
                    line_offsets,
                    line_no,
                    col,
                    item,
                    replacement,
                    flow=flow,
                ),
            )
    inserted_replacement = _render_yaml_scalar_like("", replacement, flow=flow)
    if flow_start is not None:
        return _flow_label_addition_rewrites(
            labels,
            inserted_replacement,
            source,
            line_offsets,
            flow_start,
        )
    return (
        _append_block_label_rewrite(
            service_config,
            labels,
            f"- {inserted_replacement}",
            source,
            line_offsets,
        ),
    )


def _new_service_labels_rewrite(
    service_config: CommentedMap,
    key: str,
    value: str,
    source: str,
    line_offsets: Sequence[int],
) -> tuple[int, int, str]:
    try:
        image_line_no, _image_col = service_config.lc.value("image")
        _key_line_no, key_col = service_config.lc.key("image")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ComposeTagRewriteError(
            "Service image source location is unavailable."
        ) from exc
    _line_start, line_end, _body = _source_line(
        source, line_offsets, image_line_no
    )
    insertion = line_end + 1 if line_end < len(source) else line_end
    indent = " " * key_col
    label = _render_yaml_scalar_like("", f"{key}={value}")
    replacement = _source_lines_insertion(
        source,
        insertion,
        (
            f"{indent}labels:",
            f"{indent}  - {label}",
        ),
    )
    return insertion, insertion, replacement


def _empty_service_labels_rewrite(
    service_config: CommentedMap,
    key: str,
    value: str,
    source: str,
    line_offsets: Sequence[int],
) -> tuple[int, int, str]:
    try:
        line_no, key_col = service_config.lc.key("labels")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ComposeTagRewriteError(
            _SERVICE_LABELS_SOURCE_LOCATION_UNAVAILABLE
        ) from exc
    line_start, _line_end, body = _source_line(source, line_offsets, line_no)
    pattern = re.compile(
        r"^([ \t]*(?:[\"']labels[\"']|labels)[ \t]*:[ \t]*)"
        r"(?:(?:null|Null|NULL|~)[ \t]*)?(#.*)?$"
    )
    match = pattern.fullmatch(body)
    if match is None:
        raise ComposeTagRewriteError(
            "Service labels use unsupported empty YAML syntax."
        )
    comment = match.group(2) or ""
    label = _render_yaml_scalar_like("", f"{key}={value}")
    replacement = f"{comment}\n{' ' * (key_col + 2)}- {label}"
    return line_start + len(match.group(1)), line_start + len(body), replacement


def _flow_label_addition_rewrites(
    labels: CommentedMap | CommentedSeq,
    entry: str,
    source: str,
    line_offsets: Sequence[int],
    start: int,
) -> tuple[tuple[int, int, str], ...]:
    end, last_significant = _flow_collection_bounds(source, start)
    rewrites: list[tuple[int, int, str]] = []
    needs_comma = last_significant != start and source[last_significant] != ","

    if "\n" not in source[start:end]:
        if needs_comma and last_significant + 1 == end - 1:
            prefix = ", "
        else:
            if needs_comma:
                rewrites.append((last_significant + 1, last_significant + 1, ","))
            prefix = "" if last_significant == start else " "
        rewrites.append((end - 1, end - 1, f"{prefix}{entry},"))
        return tuple(rewrites)

    if needs_comma:
        rewrites.append((last_significant + 1, last_significant + 1, ","))

    closing_line_start = source.rfind("\n", start, end - 1) + 1
    closing_prefix = source[closing_line_start : end - 1]
    if closing_prefix.strip():
        raise ComposeTagRewriteError(
            "Service labels use unsupported multiline flow-style YAML syntax."
        )
    entry_indent = _multiline_flow_label_indent(
        labels,
        source,
        line_offsets,
        start,
        closing_prefix,
    )
    rewrites.append(
        (closing_line_start, closing_line_start, f"{entry_indent}{entry},\n")
    )
    return tuple(rewrites)


def _flow_collection_bounds(source: str, start: int) -> tuple[int, int]:
    pairs = {"[": "]", "{": "}"}
    stack: list[str] = []
    quote = ""
    escaped = False
    comment = False
    last_significant = start
    index = start
    while index < len(source):
        char = source[index]
        end: int | None = None
        if comment:
            comment = char != "\n"
        elif quote:
            quote, escaped, index, last_significant = _quoted_flow_character(
                source,
                index,
                quote,
                escaped,
            )
        else:
            quote, comment, last_significant, end = _unquoted_flow_character(
                source,
                start,
                index,
                pairs,
                stack,
                last_significant,
            )
        if end is not None:
            return end, last_significant
        index += 1
    raise ComposeTagRewriteError("Service labels use an unterminated flow collection.")


def _quoted_flow_character(
    source: str,
    index: int,
    quote: str,
    escaped: bool,
) -> tuple[str, bool, int, int]:
    char = source[index]
    if quote == "'" and char == "'" and source[index + 1 : index + 2] == "'":
        return quote, escaped, index + 1, index + 1
    if quote == '"' and escaped:
        return quote, False, index, index
    if quote == '"' and char == "\\":
        return quote, True, index, index
    if char == quote:
        quote = ""
    return quote, escaped, index, index


def _unquoted_flow_character(
    source: str,
    start: int,
    index: int,
    pairs: Mapping[str, str],
    stack: list[str],
    last_significant: int,
) -> tuple[str, bool, int, int | None]:
    char = source[index]
    if char == "#" and (index == start or source[index - 1].isspace()):
        return "", True, last_significant, None
    if char in "'\"":
        return char, False, index, None
    if char in pairs:
        stack.append(char)
        return "", False, index, None
    if char in "]}":
        if not stack or pairs[stack[-1]] != char:
            raise ComposeTagRewriteError(
                "Service labels use a malformed flow collection."
            )
        stack.pop()
        if not stack:
            return "", False, last_significant, index + 1
        return "", False, index, None
    if not char.isspace():
        last_significant = index
    return "", False, last_significant, None


def _multiline_flow_label_indent(
    labels: CommentedMap | CommentedSeq,
    source: str,
    line_offsets: Sequence[int],
    collection_start: int,
    closing_prefix: str,
) -> str:
    locations: list[tuple[int, int]] = []
    try:
        if isinstance(labels, CommentedMap):
            locations = [labels.lc.key(item_key) for item_key in labels]
        else:
            locations = [labels.lc.item(index) for index in range(len(labels))]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ComposeTagRewriteError(
            _SERVICE_LABELS_SOURCE_LOCATION_UNAVAILABLE
        ) from exc
    opening_line_no = source.count("\n", 0, collection_start)
    for line_no, col in locations:
        if line_no > opening_line_no:
            _line_start, _line_end, body = _source_line(
                source, line_offsets, line_no
            )
            if not body[:col].strip():
                return body[:col]
    return f"{closing_prefix}  "


def _append_block_label_rewrite(
    service_config: CommentedMap,
    labels: CommentedMap | CommentedSeq,
    entry: str,
    source: str,
    line_offsets: Sequence[int],
) -> tuple[int, int, str]:
    try:
        labels_line_no, labels_col = service_config.lc.key("labels")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ComposeTagRewriteError(
            _SERVICE_LABELS_SOURCE_LOCATION_UNAVAILABLE
        ) from exc
    insertion = _yaml_block_end_offset(
        source,
        line_offsets,
        labels_line_no,
        labels_col,
    )
    replacement = _source_lines_insertion(
        source,
        insertion,
        (
            (
                f"{_block_label_entry_indent(labels, source, line_offsets, labels_col)}"
                f"{entry}"
            ),
        ),
    )
    return insertion, insertion, replacement


def _block_label_entry_indent(
    labels: CommentedMap | CommentedSeq,
    source: str,
    line_offsets: Sequence[int],
    labels_col: int,
) -> str:
    if isinstance(labels, CommentedMap) and labels:
        first_key = next(iter(labels))
        try:
            _line_no, col = labels.lc.key(first_key)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ComposeTagRewriteError(
                _SERVICE_LABELS_SOURCE_LOCATION_UNAVAILABLE
            ) from exc
        return " " * col
    if isinstance(labels, CommentedSeq) and labels:
        try:
            line_no, _col = labels.lc.item(0)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ComposeTagRewriteError(
                _SERVICE_LABELS_SOURCE_LOCATION_UNAVAILABLE
            ) from exc
        _line_start, _line_end, body = _source_line(source, line_offsets, line_no)
        match = re.match(r"^([ \t]*)-", body)
        if match is None:
            raise ComposeTagRewriteError(
                "Service labels use unsupported YAML syntax for automatic rewrite."
            )
        return match.group(1)
    return " " * (labels_col + 2)


def _yaml_scalar_source_rewrite(
    source: str,
    line_offsets: Sequence[int],
    line_no: int,
    col: int,
    expected: str,
    replacement: str,
    *,
    flow: bool = False,
) -> tuple[int, int, str]:
    line_start, _line_end, body = _source_line(source, line_offsets, line_no)
    if col < 0 or col >= len(body):
        raise ComposeTagRewriteError("Label source location is invalid.")
    representations = (
        expected,
        f"'{expected.replace(chr(39), chr(39) * 2)}'",
        _render_yaml_scalar_like('"', expected),
    )
    token = next(
        (
            candidate
            for candidate in representations
            if body.startswith(candidate, col)
            and _yaml_scalar_boundary_matches(
                body[col + len(candidate) :],
                flow=flow,
            )
        ),
        "",
    )
    if not token:
        raise ComposeTagRewriteError(
            "Label uses unsupported YAML syntax for automatic rewrite."
        )
    rendered = _render_yaml_scalar_like(token, replacement, flow=flow)
    return line_start + col, line_start + col + len(token), rendered


def _yaml_scalar_boundary_matches(tail: str, *, flow: bool) -> bool:
    if flow:
        return re.match(r"[ \t]*(?:[,}\]]|#)", tail) is not None
    return re.fullmatch(r"[ \t]*(?:#.*)?", tail) is not None


def _render_yaml_scalar_like(token: str, value: str, *, flow: bool = False) -> str:
    if "\n" in value or "\r" in value:
        raise ComposeTagRewriteError("Label value cannot contain a line break.")
    if token.startswith("'"):
        return f"'{value.replace(chr(39), chr(39) * 2)}'"
    if token.startswith('"'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    unsafe_initial = bool(value) and value[0] in "-?:,[]{}#&*!|>'\"%@`"
    unsafe_content = any(marker in value for marker in (" #", "\t#", ": ", ":\t"))
    unsafe_flow = flow and re.search(r"[\[\]{},]", value) is not None
    if unsafe_initial or unsafe_content or unsafe_flow:
        return f"'{value.replace(chr(39), chr(39) * 2)}'"
    return value


def _yaml_block_end_offset(
    source: str,
    line_offsets: Sequence[int],
    key_line_no: int,
    key_col: int,
) -> int:
    for line_no in range(key_line_no + 1, len(line_offsets)):
        line_start, _line_end, body = _source_line(source, line_offsets, line_no)
        if body.strip() and _line_indent_width(body) <= key_col:
            return line_start
    return len(source)


def _source_lines_insertion(
    source: str,
    offset: int,
    lines: Sequence[str],
) -> str:
    leading = "" if offset == 0 or source[offset - 1] == "\n" else "\n"
    trailing = "\n" if offset < len(source) or source.endswith("\n") else ""
    return leading + "\n".join(lines) + trailing


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

    rendered, applied = render_compose_tag_exclusions(
        compose_path,
        updates,
        existing_exact_tags=existing_exact_tags,
    )
    if applied:
        _atomic_replace_compose(compose_path, rendered, prefix="exclude")
    return applied


def apply_compose_digest_pins(
    compose_path: Path,
    updates: Sequence[DigestPinUpdate],
    *,
    label_rewrite_approvals: Sequence[DigestPinLabelRewriteApproval] = (),
    tag_stream_updates: Sequence[TagStreamUpdate] = (),
    stack_name: str = "",
) -> tuple[AppliedDigestPinUpdate, ...]:
    """Write final digest-pinned images plus WUD watch metadata."""

    rendered, applied = render_compose_digest_pins(
        compose_path,
        updates,
        label_rewrite_approvals=label_rewrite_approvals,
        tag_stream_updates=tag_stream_updates,
        stack_name=stack_name,
    )
    if updates and not rendered:
        raise ComposeTagRewriteError("Compose digest-pin rewrite produced no output.")
    _atomic_replace_compose(compose_path, rendered, prefix="digest-pin")
    return applied


def apply_compose_retag_updates(
    compose_path: Path,
    updates: Sequence[DigestPinUpdate],
    *,
    stack_name: str = "",
) -> tuple[AppliedDigestPinUpdate, ...]:
    """Write retagged images plus exact WUD tag tracking metadata."""

    rendered, applied = render_compose_retag_updates(
        compose_path,
        updates,
        stack_name=stack_name,
    )
    if updates and not rendered:
        raise ComposeTagRewriteError("Compose retag rewrite produced no output.")
    _atomic_replace_compose(compose_path, rendered, prefix="retag")
    return applied


def apply_compose_digest_unpins(
    compose_path: Path,
    updates: Sequence[DigestUnpinUpdate],
    *,
    stack_name: str = "",
) -> tuple[AppliedDigestUnpinUpdate, ...]:
    """Rewrite digest-pinned images back to tag images plus WUD watch metadata."""

    rendered, applied = render_compose_digest_unpins(
        compose_path,
        updates,
        stack_name=stack_name,
    )
    if updates and not rendered:
        raise ComposeTagRewriteError("Compose digest-unpin rewrite produced no output.")
    _atomic_replace_compose(compose_path, rendered, prefix="digest-unpin")
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


def _atomic_replace_compose(
    compose_path: Path, rendered: str, *, prefix: str,
    expected_source_hash: str | None = None,
) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{compose_path.name}.{prefix}.",
        dir=str(compose_path.parent),
    )
    tmp_path: Path | None = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as tmp:
            tmp.write(rendered)
        st = compose_path.stat()
        os.chown(tmp_path, st.st_uid, st.st_gid)
        os.chmod(tmp_path, st.st_mode & 0o7777)
        if expected_source_hash is not None and hashlib.sha256(compose_path.read_bytes()).hexdigest() != expected_source_hash:
            raise ComposeTagRewriteError("Compose file changed before tracking repair; preview it again.")
        os.replace(tmp_path, compose_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _materialize_inherited_service_labels(
    service_config: CommentedMap,
    service: str,
) -> None:
    if _commented_map_has_direct_key(service_config, "labels"):
        return

    labels = service_config.get("labels")
    if labels is None:
        return
    if isinstance(labels, CommentedMap):
        service_config["labels"] = _copy_label_map(labels)
        return
    if isinstance(labels, CommentedSeq):
        service_config["labels"] = _copy_label_sequence(labels)
        return
    raise ComposeTagRewriteError(
        f"Service {service} labels use unsupported YAML syntax."
    )


def _commented_map_has_direct_key(mapping: CommentedMap, key: str) -> bool:
    try:
        direct_items = mapping.non_merged_items()
    except AttributeError:
        return key in mapping
    return any(item_key == key for item_key, _item_value in direct_items)


def _copy_label_map(labels: CommentedMap) -> CommentedMap:
    copied = CommentedMap()
    for key, value in labels.items():
        copied[key] = value
    return copied


def _copy_label_sequence(labels: CommentedSeq) -> CommentedSeq:
    copied = CommentedSeq()
    for item in labels:
        copied.append(item)
    return copied


def _line_start_offsets(source: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", source):
        offsets.append(match.end())
    return offsets


def _service_image_scalar_span(
    services: CommentedMap,
    service: str,
    old_image: str,
    source: str,
    line_offsets: Sequence[int],
) -> tuple[int, int]:
    start, end, _replacement_prefix = _service_image_scalar_rewrite(
        services,
        service,
        old_image,
        source,
        line_offsets,
    )
    return start, end


def _service_image_scalar_rewrite(
    services: CommentedMap,
    service: str,
    old_image: str,
    source: str,
    line_offsets: Sequence[int],
) -> tuple[int, int, str]:
    service_config = services.get(service)
    if not isinstance(service_config, CommentedMap):
        raise ComposeTagRewriteError(
            f"Service {service} is not a mapping with a direct image field."
        )
    image_value = service_config.get("image")
    if not isinstance(image_value, str):
        raise ComposeTagRewriteError(
            f"Service {service} image is not a direct string scalar."
        )
    if "$" in image_value:
        raise ComposeTagRewriteError(
            f"Service {service} image uses interpolation and needs manual review."
        )
    if image_value != old_image:
        raise ComposeTagRewriteError(
            f"Service {service} image is {image_value}, expected {old_image}."
        )

    try:
        line_no, _col = service_config.lc.value("image")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ComposeTagRewriteError(
            f"Service {service} image source location is unavailable."
        ) from exc

    if line_no < 0 or line_no >= len(line_offsets):
        raise ComposeTagRewriteError(
            f"Service {service} image source location is invalid."
        )

    line_start, _line_end, body = _source_line(source, line_offsets, line_no)
    pattern = re.compile(
        r"^([ \t]*(?:[\"']image[\"']|image)[ \t]*:[ \t]*)"
        r"([\"']?)"
        + re.escape(old_image)
        + r"\2"
        r"([ \t]*(?:#.*)?)$"
    )
    match = pattern.fullmatch(body)
    if match is None:
        try:
            key_line_no, _key_col = service_config.lc.key("image")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ComposeTagRewriteError(
                f"Service {service} image source location is unavailable."
            ) from exc
        multiline_span = _multiline_plain_image_scalar_rewrite(
            source,
            line_offsets,
            key_line_no,
            line_no,
            old_image,
        )
        if multiline_span is None:
            raise ComposeTagRewriteError(
                f"Service {service} image uses unsupported YAML syntax for automatic "
                "rewrite."
            )
        return multiline_span

    start = line_start + len(match.group(1)) + len(match.group(2))
    end = start + len(old_image)
    return start, end, ""


def _multiline_plain_image_scalar_rewrite(
    source: str,
    line_offsets: Sequence[int],
    key_line_no: int,
    value_line_no: int,
    old_image: str,
) -> tuple[int, int, str] | None:
    if key_line_no < 0 or key_line_no >= len(line_offsets):
        return None
    if value_line_no != key_line_no + 1:
        return None

    key_line_start, _key_line_end, key_body = _source_line(
        source,
        line_offsets,
        key_line_no,
    )
    key_match = re.fullmatch(
        r"^([ \t]*)(?:[\"']image[\"']|image)[ \t]*:([ \t]*)$",
        key_body,
    )
    if key_match is None:
        return None

    value_line_start, _value_line_end, value_body = _source_line(
        source,
        line_offsets,
        value_line_no,
    )
    value_match = re.fullmatch(
        r"^([ \t]+)" + re.escape(old_image) + r"([ \t]*)$",
        value_body,
    )
    if value_match is None:
        return None
    if len(value_match.group(1)) <= len(key_match.group(1)):
        return None

    next_line_no = value_line_no + 1
    if next_line_no < len(line_offsets):
        _next_start, _next_end, next_body = _source_line(
            source,
            line_offsets,
            next_line_no,
        )
        next_stripped = next_body.strip()
        if next_stripped and _line_indent_width(next_body) > len(key_match.group(1)):
            return None

    trailing_space = key_match.group(2)
    start = key_line_start + len(key_body)
    replacement_prefix = "" if trailing_space else " "
    value_start = value_line_start + len(value_match.group(1))
    end = value_start + len(old_image)
    return start, end, replacement_prefix


def _source_line(
    source: str,
    line_offsets: Sequence[int],
    line_no: int,
) -> tuple[int, int, str]:
    line_start = line_offsets[line_no]
    line_end = source.find("\n", line_start)
    if line_end == -1:
        line_end = len(source)
    line = source[line_start:line_end]
    body = line.removesuffix("\r")
    return line_start, line_end, body


def _line_indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _direct_service_config(services: CommentedMap, service: str) -> CommentedMap:
    service_config = services.get(service)
    if not isinstance(service_config, CommentedMap):
        raise ComposeTagRewriteError(
            f"Service {service} is not a mapping with a direct image field."
        )
    return service_config


def _reject_yaml_anchor_or_alias_image_value(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
) -> None:
    image_value = service_config.get("image")
    anchor = getattr(image_value, "anchor", None)
    if getattr(anchor, "value", None):
        raise ComposeTagRewriteError(
            f"Service {service} image uses YAML anchors or aliases and needs manual review."
        )
    for other_service, other_config in services.items():
        if other_service == service or not isinstance(other_config, CommentedMap):
            continue
        other_image = other_config.get("image")
        if other_image is image_value and getattr(image_value, "anchor", None) is not None:
            raise ComposeTagRewriteError(
                f"Service {service} image uses YAML anchors or aliases and needs manual review."
            )


def _get_service_label_value(service_config: CommentedMap, key: str) -> str:
    labels = service_config.get("labels")
    if labels is None:
        return ""
    if isinstance(labels, CommentedMap):
        value = labels.get(key)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ComposeTagRewriteError(f"Label {key} is not a string value.")
        return value
    if isinstance(labels, CommentedSeq):
        for item in labels:
            if not isinstance(item, str):
                raise ComposeTagRewriteError(_UNSUPPORTED_NON_STRING_LABEL_ENTRY)
            label_key, sep, label_value = item.partition("=")
            if sep and label_key == key:
                return label_value
        return ""
    raise ComposeTagRewriteError(_UNSUPPORTED_SERVICE_LABELS_YAML)


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


def _service_comment_tokens(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
) -> tuple[object, ...]:
    tokens: list[object] = []
    seen: set[int] = set()
    for token_list in _service_comment_token_lists(services, service, service_config):
        for token in token_list.tokens:
            token_id = id(token)
            if token_id in seen:
                continue
            tokens.append(token)
            seen.add(token_id)
    return tuple(tokens)


def _service_comment_token_lists(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
) -> tuple[_CommentTokenList, ...]:
    token_lists: list[_CommentTokenList] = []
    _append_comment_slots(token_lists, getattr(service_config.ca, "comment", None))
    _append_comment_slots(token_lists, services.ca.items.get(service))
    for key in service_config:
        _append_comment_slots(token_lists, service_config.ca.items.get(key))
    _append_comment_token_list(
        token_lists,
        getattr(service_config.ca, "end", None),
    )
    return tuple(token_lists)


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


def _append_comment_slots(
    token_lists: list[_CommentTokenList],
    slots: list[object] | None,
) -> None:
    if not slots:
        return
    for index, slot in enumerate(slots):
        if isinstance(slot, list):
            _append_comment_token_list(
                token_lists,
                slot,
                lambda kept, slots=slots, index=index, slot=slot: (
                    _replace_comment_slot_list(slots, index, slot, kept)
                ),
            )
        elif _is_comment_token(slot):
            _append_standalone_comment_token(token_lists, slots, index, slot)


def _append_comment_token_list(
    token_lists: list[_CommentTokenList],
    tokens: list[object] | None,
    replace: Callable[[list[object]], None] | None = None,
) -> None:
    if not tokens or not any(_is_comment_token(token) for token in tokens):
        return
    if replace is None:
        def replace_comment_tokens(kept: list[object]) -> None:
            tokens[:] = kept

        replace = replace_comment_tokens
    token_lists.append(_CommentTokenList(tokens, replace))


def _append_standalone_comment_token(
    token_lists: list[_CommentTokenList],
    slots: list[object],
    index: int,
    token: object,
) -> None:
    token_lists.append(
        _CommentTokenList(
            [token],
            lambda kept, slots=slots, index=index: slots.__setitem__(
                index,
                kept[0] if kept else None,
            ),
        )
    )


def _replace_comment_slot_list(
    slots: list[object],
    index: int,
    original: list[object],
    kept: list[object],
) -> None:
    original[:] = kept
    slots[index] = original if kept else None


def _is_comment_token(value: object) -> bool:
    return isinstance(getattr(value, "value", None), str)


def _empty_detached_service_comment_lists(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
) -> None:
    comment = getattr(service_config.ca, "comment", None)
    if comment and len(comment) > 1 and comment[1] == []:
        comment[1] = None
    item = services.ca.items.get(service)
    if item and len(item) > 3 and item[3] == []:
        item[3] = None


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


def _reject_yaml_anchor_or_alias_service_config(
    services: CommentedMap,
    service: str,
    service_config: CommentedMap,
) -> None:
    anchor = getattr(service_config, "anchor", None)
    if getattr(anchor, "value", None):
        raise ComposeTagRewriteError(
            f"Service {service} uses YAML anchors or aliases and needs manual review."
        )
    for other_service, other_config in services.items():
        if other_service == service or not isinstance(other_config, CommentedMap):
            continue
        if other_config is service_config:
            raise ComposeTagRewriteError(
                f"Service {service} uses YAML anchors or aliases and needs manual review."
            )


def _reject_yaml_anchor_or_alias_labels(
    services: CommentedMap,
    service: str,
    labels: object,
) -> None:
    anchor = getattr(labels, "anchor", None)
    if getattr(anchor, "value", None):
        raise ComposeTagRewriteError(
            f"Service {service} labels use YAML anchors or aliases and need manual review."
        )
    for other_service, other_config in services.items():
        if other_service == service or not isinstance(other_config, CommentedMap):
            continue
        if other_config.get("labels") is labels:
            raise ComposeTagRewriteError(
                f"Service {service} labels use YAML anchors or aliases and need manual review."
            )


def _set_service_label_value(
    service_config: CommentedMap,
    key: str,
    value: str,
) -> None:
    labels = service_config.get("labels")
    if labels is None:
        labels = CommentedSeq()
        service_config["labels"] = labels
    if isinstance(labels, CommentedMap):
        labels[key] = value
        return
    if isinstance(labels, CommentedSeq):
        replacement = f"{key}={value}"
        for index, item in enumerate(labels):
            if not isinstance(item, str):
                raise ComposeTagRewriteError(_UNSUPPORTED_NON_STRING_LABEL_ENTRY)
            label_key, sep, _label_value = item.partition("=")
            if sep and label_key == key:
                labels[index] = replacement
                return
        labels.append(replacement)
        return
    raise ComposeTagRewriteError(_UNSUPPORTED_SERVICE_LABELS_YAML)


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


def _backup_compose(compose_path: Path) -> Path:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{compose_path.name}.backup.",
        dir=str(compose_path.parent),
    )
    os.close(fd)
    backup = Path(tmp_name)
    try:
        shutil.copy2(compose_path, backup)
    except Exception:
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        raise
    return backup


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
