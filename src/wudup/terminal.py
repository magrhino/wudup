"""Terminal rendering helpers with optional Rich support."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

try:  # pragma: no cover - exercised by Rich-specific tests when installed.
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover - plain fallback is tested directly.
    box = None
    Console = None
    Panel = None
    Table = None
    Text = None

RICH_AVAILABLE = Console is not None
_BANNER_BOX_HORIZONTAL_OVERHEAD = 4
_STYLE_INFO = "bold cyan"
_STYLE_WARNING = "bold yellow"
_STYLE_ERROR = "bold red"
_STYLE_SUCCESS = "bold green"
_STYLE_DIM = "dim"
_STYLE_WHITE = "white"
_STYLE_BLUE = "blue"


class TerminalRenderer:
    """Render terminal output with Rich when appropriate, otherwise plain text."""

    def __init__(
        self,
        *,
        no_color: bool = False,
        environ: Mapping[str, str] | None = None,
        stream: TextIO | None = None,
        force_rich: bool = False,
        width: int | None = None,
    ) -> None:
        self.environ = dict(environ or {})
        self.stream = sys.stdout if stream is None else stream
        self.no_color = no_color or "NO_COLOR" in self.environ
        self.force_rich = force_rich
        self.width = width
        self._consoles: dict[int, Any] = {}

    def rich_enabled(self, stream: TextIO | None = None) -> bool:
        target = self.stream if stream is None else stream
        return RICH_AVAILABLE and not self.no_color and (
            self.force_rich or _stream_is_tty(target)
        )

    def line(
        self,
        message: str = "",
        *,
        style: str | None = None,
        stream: TextIO | None = None,
    ) -> None:
        target = self.stream if stream is None else stream
        if self.rich_enabled(target):
            self._console(target).print(message, style=style)
        else:
            print(message, file=target)

    def blank(self, *, stream: TextIO | None = None) -> None:
        self.line("", stream=stream)

    def log_line(
        self,
        *,
        timestamp: str,
        level: str,
        message: str,
        stream: TextIO | None = None,
    ) -> None:
        target = self.stream if stream is None else stream
        if not self.rich_enabled(target) or Text is None:
            print(f"[{timestamp}] {message}", file=target)
            return

        level_style = _LEVEL_STYLES.get(level, _STYLE_INFO)
        text = Text()
        text.append(f"[{timestamp}]", style=_STYLE_DIM)
        text.append(" ")
        text.append(f"{level:<5}", style=level_style)
        text.append(" ")
        text.append(message)
        self._console(target).print(text)

    def status(
        self,
        message: str,
        *,
        kind: str = "info",
        plain: str | None = None,
        stream: TextIO | None = None,
    ) -> None:
        target = self.stream if stream is None else stream
        if not self.rich_enabled(target):
            print(plain or message, file=target)
            return

        style = _KIND_STYLES.get(kind, _STYLE_INFO)
        prefix = _KIND_PREFIXES.get(kind, "")
        self._console(target).print(f"{prefix}{message}", style=style)

    def panel(
        self,
        title: str,
        body: str | Any,
        *,
        body_style: str | None = None,
        stream: TextIO | None = None,
    ) -> None:
        target = self.stream if stream is None else stream
        if not self.rich_enabled(target) or Panel is None:
            print(str(body), file=target)
            return
        panel_options = {
            "title": f" {title} ",
            "title_align": "left",
            "border_style": _STYLE_INFO,
        }
        if body_style is not None:
            panel_options["style"] = body_style
        self._console(target).print(
            Panel(
                body,
                **panel_options,
            )
        )

    def startup_banner(
        self,
        *,
        art: str,
        local_tag: str,
        release_status: tuple[str, str] | None = None,
        stream: TextIO | None = None,
    ) -> None:
        target = self.stream if stream is None else stream
        if self.rich_enabled(target) and Panel is not None and Text is not None:
            console = self._console(target)
            include_art = _banner_art_fits(
                art,
                _banner_content_width(console.width),
            )
            body = Text()
            if include_art:
                body.append(art.rstrip("\n"), style=_STYLE_INFO)
                body.append("\n\n")
            body.append(f"WUDup {local_tag}", style=_STYLE_SUCCESS)
            if release_status is not None:
                message, kind = release_status
                body.append("\n")
                body.append(message, style=_KIND_STYLES.get(kind, _STYLE_WHITE))
            console.print(
                Panel(
                    body,
                    title=f" WUDup {local_tag} ",
                    title_align="left",
                    border_style=_STYLE_INFO,
                )
            )
            return

        for line in _plain_banner_lines(
            art=art,
            local_tag=local_tag,
            release_status=release_status,
            terminal_width=_terminal_width(target, self.width),
        ):
            print(line, file=target)

    def updater_targets(self, rows: Sequence[tuple[int, str, str, str]]) -> None:
        if not self.rich_enabled():
            return

        table = self._table(title="Targets")
        table.add_column("Line", justify="right", style=_STYLE_DIM, no_wrap=True)
        table.add_column("Target", style=_STYLE_WHITE)
        table.add_column("Digest", style=_STYLE_DIM)
        table.add_column("Tag", style=_STYLE_BLUE)
        for line_no, target, digest, tag in rows:
            table.add_row(str(line_no), target, digest, tag)
        self._console(self.stream).print(table)

    def updater_stack_plan(
        self,
        rows: Sequence[tuple[str, str, Sequence[str]]],
    ) -> None:
        if not self.rich_enabled():
            return

        for name, services, lines in rows:
            if Text is None:
                return
            body = Text()
            body.append("Services: ", style=_STYLE_DIM)
            body.append(services or "stack-level fallback")
            for line in lines:
                body.append("\n")
                body.append(line, style=_STYLE_WHITE)
            self.panel(f"Stack: {name}", body)

    def stack_summary(
        self,
        name: str,
        lines: Sequence[tuple[str, str]],
    ) -> None:
        if not self.rich_enabled():
            return
        if Text is None:
            return
        body = Text()
        for index, (line, kind) in enumerate(lines):
            if index:
                body.append("\n")
            body.append(line, style=_KIND_STYLES.get(kind, _STYLE_WHITE))
        self.panel(f"Stack: {name}", body)

    def _table(self, *, title: str | None = None) -> Any:
        if Table is None:
            raise RuntimeError("Rich table requested when Rich is unavailable")
        table = Table(
            title=title,
            box=box.SIMPLE_HEAVY if box is not None else None,
            header_style=_STYLE_INFO,
            border_style=_STYLE_INFO,
        )
        return table

    def _console(self, stream: TextIO) -> Any:
        key = id(stream)
        if key not in self._consoles:
            if Console is None:
                raise RuntimeError("Rich console requested when Rich is unavailable")
            self._consoles[key] = Console(
                file=stream,
                force_terminal=True if self.force_rich else None,
                color_system="standard",
                width=self.width,
            )
        return self._consoles[key]


def _stream_is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _terminal_width(_stream: TextIO, explicit_width: int | None) -> int:
    if explicit_width is not None:
        return explicit_width
    return shutil.get_terminal_size(fallback=(80, 24)).columns


def _banner_content_width(terminal_width: int) -> int:
    return max(0, terminal_width - _BANNER_BOX_HORIZONTAL_OVERHEAD)


def _banner_art_fits(art: str, content_width: int) -> bool:
    art_lines = art.rstrip("\n").splitlines()
    art_width = max((len(line) for line in art_lines), default=0)
    return art_width <= content_width


def _plain_banner_lines(
    *,
    art: str,
    local_tag: str,
    release_status: tuple[str, str] | None,
    terminal_width: int,
) -> list[str]:
    content: list[str] = []
    if _banner_art_fits(art, _banner_content_width(terminal_width)):
        content.extend(art.rstrip("\n").splitlines())
        content.append("")
    content.append(f"WUDup {local_tag}")
    if release_status is not None:
        content.append(release_status[0])
    width = max((len(line) for line in content), default=0)
    horizontal = f"+-{'-' * width}-+"
    lines = [horizontal]
    for line in content:
        lines.append(f"| {line.ljust(width)} |")
    lines.append(horizontal)
    return lines


_LEVEL_STYLES = {
    "INFO": _STYLE_INFO,
    "WARN": _STYLE_WARNING,
    "ERROR": _STYLE_ERROR,
}
_KIND_STYLES = {
    "info": _STYLE_INFO,
    "success": _STYLE_SUCCESS,
    "warning": _STYLE_WARNING,
    "error": _STYLE_ERROR,
    "path": _STYLE_DIM,
}
_KIND_PREFIXES = {
    "success": "✓ ",
    "warning": "! ",
    "error": "✗ ",
}
