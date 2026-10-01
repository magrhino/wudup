from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from wudup.command import (
    CommandError,
    CommandResult,
    CommandRunner,
)


class CommandRunnerTests(unittest.TestCase):
    def test_command_result_properties(self) -> None:
        result = CommandResult(
            args=("echo", "hello"),
            cwd=None,
            returncode=0,
            stdout="hello\nworld\n",
            stderr="error1\nerror2",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout_lines, ["hello", "world"])
        self.assertEqual(result.stderr_lines, ["error1", "error2"])
        self.assertEqual(result.display, "echo hello")

    def test_command_error_formatting(self) -> None:
        result = CommandResult(
            args=("false",),
            cwd=None,
            returncode=1,
        )
        error = CommandError(result)
        self.assertEqual(str(error), "Command failed with exit code 1: false")
        self.assertIs(error.result, result)

    @mock.patch("wudup.command.subprocess.run")
    def test_capture_success(self, run_mock: mock.Mock) -> None:
        run_mock.return_value = mock.Mock(
            returncode=0,
            stdout="captured out",
            stderr="captured err",
        )
        runner = CommandRunner()

        result = runner.capture(["ls", "-l"])

        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "captured out")
        self.assertEqual(result.stderr, "captured err")
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[0], ("ls", "-l"))

    @mock.patch("wudup.command.subprocess.run")
    def test_capture_failure_raises(self, run_mock: mock.Mock) -> None:
        run_mock.return_value = mock.Mock(
            returncode=2,
            stdout="",
            stderr="bad usage",
        )
        runner = CommandRunner()

        with self.assertRaises(CommandError) as cm:
            runner.capture(["ls", "--bad"])

        self.assertEqual(cm.exception.result.returncode, 2)
        self.assertEqual(cm.exception.result.stderr, "bad usage")

    @mock.patch("wudup.command.subprocess.run")
    def test_capture_failure_no_check(self, run_mock: mock.Mock) -> None:
        run_mock.return_value = mock.Mock(
            returncode=2,
            stdout="",
            stderr="bad usage",
        )
        runner = CommandRunner()

        result = runner.capture(["ls", "--bad"], check=False)

        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 2)

    @mock.patch("wudup.command.subprocess.run")
    def test_capture_os_error_handling(self, run_mock: mock.Mock) -> None:
        run_mock.side_effect = FileNotFoundError("No such file or directory")
        runner = CommandRunner()

        result = runner.capture(["nonexistent"], check=False)

        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stderr, "No such file or directory")

    @mock.patch("wudup.command.subprocess.run")
    def test_capture_permission_error_handling(self, run_mock: mock.Mock) -> None:
        run_mock.side_effect = PermissionError("Permission denied")
        runner = CommandRunner()

        result = runner.capture(["/root/secret"], check=False)

        self.assertEqual(result.returncode, 126)

    @mock.patch("wudup.command.subprocess.run")
    def test_capture_timeout_returns_timeout_result(self, run_mock: mock.Mock) -> None:
        run_mock.side_effect = subprocess.TimeoutExpired(
            cmd=("slow",),
            timeout=2,
            output="partial out",
            stderr="partial err",
        )
        runner = CommandRunner()

        result = runner.capture(["slow"], check=False, timeout_seconds=2)

        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "partial out")
        self.assertEqual(result.stderr, "partial err")
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 2)

    @mock.patch("wudup.command.subprocess.run")
    def test_capture_timeout_decodes_bytes_output_and_stderr(
        self,
        run_mock: mock.Mock,
    ) -> None:
        run_mock.side_effect = subprocess.TimeoutExpired(
            cmd=("slow",),
            timeout=2,
            output=b"partial out bytes",
            stderr=b"partial err bytes",
        )
        runner = CommandRunner()

        result = runner.capture(["slow"], check=False, timeout_seconds=2)

        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "partial out bytes")
        self.assertEqual(result.stderr, "partial err bytes")

    @mock.patch("wudup.command.subprocess.run")
    def test_capture_timeout_handles_none_output_and_stderr(
        self,
        run_mock: mock.Mock,
    ) -> None:
        run_mock.side_effect = subprocess.TimeoutExpired(
            cmd=("slow",),
            timeout=2,
            output=None,
            stderr=None,
        )
        runner = CommandRunner()

        result = runner.capture(["slow"], check=False, timeout_seconds=2)

        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "command timed out")

    @mock.patch("wudup.command.subprocess.run")
    def test_capture_lines(self, run_mock: mock.Mock) -> None:
        run_mock.return_value = mock.Mock(
            returncode=0,
            stdout="line1\nline2\n",
            stderr="",
        )
        runner = CommandRunner()

        lines = runner.capture_lines(["echo"])

        self.assertEqual(lines, ["line1", "line2"])

    @mock.patch("wudup.command.subprocess.run")
    def test_run_success(self, run_mock: mock.Mock) -> None:
        run_mock.return_value = mock.Mock(returncode=0)
        runner = CommandRunner()

        result = runner.run(["echo", "hello"])

        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    @mock.patch("wudup.command.subprocess.run")
    def test_run_os_error_handling(self, run_mock: mock.Mock) -> None:
        run_mock.side_effect = FileNotFoundError("Missing")
        runner = CommandRunner()

        with self.assertRaises(CommandError) as cm:
            runner.run(["missing"])

        self.assertEqual(cm.exception.result.returncode, 127)
        self.assertIsInstance(cm.exception.__cause__, FileNotFoundError)

    @mock.patch("wudup.command.subprocess.run")
    def test_capture_check_chains_os_error(self, run_mock: mock.Mock) -> None:
        run_mock.side_effect = FileNotFoundError("Missing")
        runner = CommandRunner()

        with self.assertRaises(CommandError) as cm:
            runner.capture(["missing"], check=True)

        self.assertEqual(cm.exception.result.returncode, 127)
        self.assertIsInstance(cm.exception.__cause__, FileNotFoundError)

    @mock.patch("wudup.command.os.read")
    @mock.patch("wudup.command.os.close")
    @mock.patch("wudup.command.subprocess.Popen")
    def test_run_in_pty_success(
        self,
        popen_mock: mock.Mock,
        close_mock: mock.Mock,
        read_mock: mock.Mock,
    ) -> None:
        import wudup.command

        if getattr(wudup.command, "pty", None) is None:
            self.skipTest("pty not available on this platform")

        with mock.patch("wudup.command.pty.openpty") as openpty_mock:
            openpty_mock.return_value = (10, 11)
            read_mock.side_effect = [b"pty output", b""]
            process_mock = mock.Mock()
            process_mock.wait.return_value = 0
            popen_mock.return_value = process_mock

            with mock.patch("wudup.command._copy_terminal_size"), mock.patch("sys.stdout"):
                runner = CommandRunner()
                result = runner.run_in_pty(["some-command"])

        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "pty output")
        close_mock.assert_has_calls([mock.call(11), mock.call(10)], any_order=True)
        popen_mock.assert_called_once()
        self.assertEqual(popen_mock.call_args.kwargs["stdout"], 11)

    def test_run_in_pty_fallback_on_oserror(self) -> None:
        import wudup.command

        if getattr(wudup.command, "pty", None) is None:
            self.skipTest("pty not available on this platform")

        with mock.patch("wudup.command.pty.openpty") as openpty_mock:
            openpty_mock.side_effect = OSError("no pty")
            runner = CommandRunner()

            with mock.patch.object(runner, "run_streaming") as streaming_mock:
                streaming_mock.return_value = CommandResult(args=("foo",), cwd=None, returncode=0)
                result = runner.run_in_pty(["foo"])

        self.assertTrue(result.ok)
        streaming_mock.assert_called_once()

    @mock.patch("wudup.command.subprocess.Popen")
    def test_run_streaming_success(self, popen_mock: mock.Mock) -> None:
        process_mock = mock.Mock()
        process_mock.stdout = ["out1\n", "out2\n"]
        process_mock.stderr = ["err1\n"]
        process_mock.wait.return_value = 0
        popen_mock.return_value = process_mock

        runner = CommandRunner()

        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            result = runner.run_streaming(["streaming_command"])

        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "out1\nout2\n")
        self.assertEqual(result.stderr, "err1\n")

    @mock.patch("wudup.command.subprocess.Popen")
    def test_run_streaming_oserror(self, popen_mock: mock.Mock) -> None:
        popen_mock.side_effect = FileNotFoundError("Missing streaming")
        runner = CommandRunner()

        result = runner.run_streaming(["missing"], check=False)

        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 127)

    @mock.patch("wudup.command.subprocess.run")
    def test_cwd_and_env_passing(self, run_mock: mock.Mock) -> None:
        run_mock.return_value = mock.Mock(returncode=0)
        runner = CommandRunner(env={"BASE": "1"})

        with mock.patch.dict(os.environ, {"OS_VAR": "2"}, clear=True):
            runner.capture(
                ["pwd"],
                cwd=Path("/srv/wud"),
                env={"CALL_VAR": "3"},
            )

        run_mock.assert_called_once()
        kwargs = run_mock.call_args.kwargs
        self.assertEqual(kwargs["cwd"], "/srv/wud")
        self.assertEqual(kwargs["env"]["OS_VAR"], "2")
        self.assertEqual(kwargs["env"]["BASE"], "1")
        self.assertEqual(kwargs["env"]["CALL_VAR"], "3")


if __name__ == "__main__":
    unittest.main()
