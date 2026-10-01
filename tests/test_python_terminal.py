from __future__ import annotations

import unittest
from io import StringIO
from unittest import mock

from wudup import terminal
from wudup.terminal import RICH_AVAILABLE, TerminalRenderer


class FakeConsole:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.printed: list[object] = []

    def print(self, renderable: object, *args: object, **kwargs: object) -> None:
        self.printed.append(renderable)


class FakeText:
    def __init__(self) -> None:
        self.parts: list[tuple[str, str | None]] = []

    def append(self, value: str, *, style: str | None = None) -> None:
        self.parts.append((value, style))


class TerminalRendererTests(unittest.TestCase):
    def test_no_color_disables_forced_rich_output(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(
            no_color=True,
            stream=output,
            force_rich=True,
            width=80,
        )

        renderer.log_line(timestamp="12:41:08", level="WARN", message="plain output")

        text = output.getvalue()
        self.assertEqual(text, "[12:41:08] plain output\n")
        self.assertNotIn("\x1b[", text)

    @unittest.skipUnless(RICH_AVAILABLE, "Rich is not installed")
    def test_forced_rich_log_line_styles_level(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(stream=output, force_rich=True, width=80)

        renderer.log_line(
            timestamp="12:41:08",
            level="WARN",
            message="[app] stopping affected service",
        )

        text = output.getvalue()
        self.assertIn("12:41:08", text)
        self.assertIn("WARN", text)
        self.assertIn("[app] stopping affected service", text)
        self.assertIn("\x1b[", text)

    def test_panel_omits_rich_style_when_body_style_is_absent(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(stream=output, force_rich=True, width=80)

        with (
            mock.patch.object(terminal, "RICH_AVAILABLE", True),
            mock.patch.object(terminal, "Console", FakeConsole),
            mock.patch.object(terminal, "Panel") as panel_mock,
        ):
            panel_mock.return_value = object()

            renderer.panel("Stack: app", "body")

        self.assertNotIn("style", panel_mock.call_args.kwargs)

    def test_panel_preserves_explicit_rich_style(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(stream=output, force_rich=True, width=80)

        with (
            mock.patch.object(terminal, "RICH_AVAILABLE", True),
            mock.patch.object(terminal, "Console", FakeConsole),
            mock.patch.object(terminal, "Panel") as panel_mock,
        ):
            panel_mock.return_value = object()

            renderer.panel("Docker Updates", "body", body_style="bold yellow")

        self.assertEqual(panel_mock.call_args.kwargs["style"], "bold yellow")

    def test_updater_stack_plan_omits_empty_rich_panel_style(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(stream=output, force_rich=True, width=80)

        with (
            mock.patch.object(terminal, "RICH_AVAILABLE", True),
            mock.patch.object(terminal, "Console", FakeConsole),
            mock.patch.object(terminal, "Panel") as panel_mock,
            mock.patch.object(terminal, "Text", FakeText),
        ):
            panel_mock.return_value = object()

            renderer.updater_stack_plan(
                [("qbittorrent", "qbittorrent", ["line 1: repo/app:old -> new"])]
            )

        panel_mock.assert_called_once()
        self.assertNotIn("style", panel_mock.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
