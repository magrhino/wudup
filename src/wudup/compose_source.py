"""Source-preserving Compose YAML editing, independent of update decisions.

Owns the round-trip YAML tree, source spans, label styles, anchor/alias guards,
and comment-token containers. Callers choose replacement values and marker
policy; these helpers validate and edit their exact YAML locations. No writes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from .updater_models import ComposeTagRewriteError

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
    source = compose_path.read_bytes().decode("utf-8")
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
        return re.match(r"[ \t]*[,}\]#]", tail) is not None
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
        return _sequence_label_value(labels, key)
    raise ComposeTagRewriteError(_UNSUPPORTED_SERVICE_LABELS_YAML)


def _sequence_label_value(labels: CommentedSeq, key: str) -> str:
    for item in labels:
        if not isinstance(item, str):
            raise ComposeTagRewriteError(_UNSUPPORTED_NON_STRING_LABEL_ENTRY)
        label_key, sep, label_value = item.partition("=")
        if sep and label_key == key:
            return label_value
    return ""


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
