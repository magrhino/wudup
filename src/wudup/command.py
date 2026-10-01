"""Subprocess helpers for the Python updater."""

from __future__ import annotations

import codecs
import errno
import os
import shlex
import shutil
import struct
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

try:  # pragma: no cover - platform availability is covered by fallback tests.
    import fcntl
    import pty
    import termios
except ImportError:  # pragma: no cover - Windows fallback.
    fcntl = None
    pty = None
    termios = None


CommandArg = str | os.PathLike[str]
STREAM_TAIL_LINES = 200
MAX_WINSIZE_VALUE = 65535


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    cwd: Path | None
    returncode: int
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def stdout_lines(self) -> list[str]:
        return self.stdout.splitlines()

    @property
    def stderr_lines(self) -> list[str]:
        return self.stderr.splitlines()

    @property
    def display(self) -> str:
        return display_command(self.args)


class CommandError(RuntimeError):
    """Raised when a checked subprocess exits unsuccessfully."""

    def __init__(self, result: CommandResult) -> None:
        super().__init__(
            f"Command failed with exit code {result.returncode}: {result.display}"
        )
        self.result = result


class CommandRunner:
    """Small subprocess wrapper with explicit argv lists and optional base env."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = dict(env) if env is not None else None

    def capture(
        self,
        args: Sequence[CommandArg],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Run a command and capture stdout/stderr as text."""

        argv = normalize_args(args)
        cwd_path = Path(cwd) if cwd is not None else None
        os_error: OSError | None = None
        try:
            # Security audit: argv stays a tuple and shell=False is the subprocess default.
            completed = subprocess.run(  # nosemgrep
                argv,
                cwd=str(cwd_path) if cwd_path is not None else None,
                env=self._merged_env(env),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
            result = CommandResult(
                args=argv,
                cwd=cwd_path,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                args=argv,
                cwd=cwd_path,
                returncode=124,
                stdout=_decode_timeout_output(exc.stdout),
                stderr=_decode_timeout_output(exc.stderr) or "command timed out",
            )
        except OSError as exc:
            os_error = exc
            result = _result_from_os_error(argv, cwd_path, exc)
        if check and not result.ok:
            raise CommandError(result) from os_error
        return result

    def capture_lines(
        self,
        args: Sequence[CommandArg],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> list[str]:
        """Run a command and return stdout split into lines."""

        return self.capture(
            args,
            cwd=cwd,
            env=env,
            check=check,
            timeout_seconds=timeout_seconds,
        ).stdout_lines

    def run(
        self,
        args: Sequence[CommandArg],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Run a command without capturing output."""

        argv = normalize_args(args)
        cwd_path = Path(cwd) if cwd is not None else None
        os_error: OSError | None = None
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd_path) if cwd_path is not None else None,
                env=self._merged_env(env),
                stdin=subprocess.DEVNULL,
                check=False,
            )
            result = CommandResult(
                args=argv,
                cwd=cwd_path,
                returncode=completed.returncode,
            )
        except OSError as exc:
            os_error = exc
            result = _result_from_os_error(argv, cwd_path, exc)
        if check and not result.ok:
            raise CommandError(result) from os_error
        return result

    def run_in_pty(
        self,
        args: Sequence[CommandArg],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Run a mutating command.

        Attach stdout/stderr to a real PTY so tools such as Docker Compose keep
        their native progress UI while still returning a bounded output tail.
        """

        if os.name != "posix" or pty is None:
            return self.run_streaming(args, cwd=cwd, env=env, check=check)

        argv = normalize_args(args)
        cwd_path = Path(cwd) if cwd is not None else None
        tail = _TextTail(STREAM_TAIL_LINES)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        try:
            master_fd, slave_fd = pty.openpty()
        except OSError:
            return self.run_streaming(args, cwd=cwd, env=env, check=check)

        _copy_terminal_size(slave_fd, sys.stdout)
        try:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=str(cwd_path) if cwd_path is not None else None,
                    env=self._merged_env(env),
                    stdin=subprocess.DEVNULL,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    close_fds=True,
                )
            except OSError as exc:
                result = _result_from_os_error(argv, cwd_path, exc)
                if check and not result.ok:
                    raise CommandError(result) from exc
                return result
            finally:
                os.close(slave_fd)

            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                _write_stream_bytes(sys.stdout, chunk)
                tail.feed(decoder.decode(chunk))

            returncode = process.wait()
            tail.feed(decoder.decode(b"", final=True))
            tail.close()
        finally:
            os.close(master_fd)

        result = CommandResult(
            args=argv,
            cwd=cwd_path,
            returncode=returncode,
            stdout=tail.text,
            stderr="",
            stdout_truncated=tail.truncated,
            stderr_truncated=False,
        )
        if check and not result.ok:
            raise CommandError(result)
        return result

    def run_streaming(
        self,
        args: Sequence[CommandArg],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        tail_lines: int = STREAM_TAIL_LINES,
    ) -> CommandResult:
        """Run a command, stream output live, and keep a bounded output tail."""

        argv = normalize_args(args)
        cwd_path = Path(cwd) if cwd is not None else None
        stdout_tail: deque[str] = deque(maxlen=tail_lines)
        stderr_tail: deque[str] = deque(maxlen=tail_lines)
        stdout_count = [0]
        stderr_count = [0]
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd_path) if cwd_path is not None else None,
                env=self._merged_env(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            result = _result_from_os_error(argv, cwd_path, exc)
            if check and not result.ok:
                raise CommandError(result) from exc
            return result

        threads = [
            threading.Thread(
                target=_stream_pipe,
                args=(process.stdout, sys.stdout, stdout_tail, stdout_count),
                daemon=True,
            ),
            threading.Thread(
                target=_stream_pipe,
                args=(process.stderr, sys.stderr, stderr_tail, stderr_count),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        returncode = process.wait()
        for thread in threads:
            thread.join()

        result = CommandResult(
            args=argv,
            cwd=cwd_path,
            returncode=returncode,
            stdout="".join(stdout_tail),
            stderr="".join(stderr_tail),
            stdout_truncated=stdout_count[0] > tail_lines,
            stderr_truncated=stderr_count[0] > tail_lines,
        )
        if check and not result.ok:
            raise CommandError(result)
        return result

    def _merged_env(self, env: Mapping[str, str] | None) -> dict[str, str] | None:
        if self._env is None and env is None:
            return None
        merged = os.environ.copy()
        if self._env is not None:
            merged.update(self._env)
        if env is not None:
            merged.update(env)
        return merged


def normalize_args(args: Sequence[CommandArg]) -> tuple[str, ...]:
    return tuple(os.fspath(arg) for arg in args)


def display_command(args: Sequence[CommandArg]) -> str:
    return shlex.join(normalize_args(args))


def _stream_pipe(
    source: TextIO | None,
    target: TextIO,
    tail: deque[str],
    count: list[int],
) -> None:
    if source is None:
        return
    for line in source:
        count[0] += 1
        tail.append(line)
        try:
            target.write(line)
            target.flush()
        except OSError:
            pass


class _TextTail:
    def __init__(self, max_lines: int) -> None:
        self.max_lines = max_lines
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.line_count = 0
        self.partial = ""

    def feed(self, text: str) -> None:
        if not text:
            return
        combined = self.partial + text
        parts = combined.splitlines(keepends=True)
        if combined and not combined.endswith(("\n", "\r")):
            self.partial = parts.pop() if parts else combined
        else:
            self.partial = ""
        for part in parts:
            self._append(part)
        self._flush_long_partial()

    def close(self) -> None:
        if self.partial:
            self._append(self.partial)
            self.partial = ""

    @property
    def text(self) -> str:
        return "".join(self.lines)

    @property
    def truncated(self) -> bool:
        return self.line_count > self.max_lines

    def _append(self, line: str) -> None:
        self.line_count += 1
        self.lines.append(line)

    def _flush_long_partial(self) -> None:
        while len(self.partial) > 8192:
            self._append(self.partial[:8192])
            self.partial = self.partial[8192:]


def _write_stream_bytes(target: TextIO, chunk: bytes) -> None:
    try:
        buffer = getattr(target, "buffer", None)
        if buffer is not None:
            buffer.write(chunk)
            buffer.flush()
        else:
            target.write(chunk.decode("utf-8", errors="replace"))
            target.flush()
    except OSError:
        pass


def _copy_terminal_size(slave_fd: int, stream: TextIO) -> None:
    if fcntl is None or termios is None:
        return
    try:
        size = _terminal_size_bytes(stream)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, size)
    except (OSError, ValueError):
        return


def _terminal_size_bytes(stream: TextIO) -> bytes:
    fileno = getattr(stream, "fileno", None)
    if fileno is not None:
        try:
            stream_fd = fileno()
            if stream_fd >= 0:
                size = fcntl.ioctl(stream_fd, termios.TIOCGWINSZ, b"\0" * 8)
                rows, columns, _xpixels, _ypixels = struct.unpack("HHHH", size)
                if rows > 0 and columns > 0:
                    return size
        except (OSError, ValueError):
            pass

    size = shutil.get_terminal_size(fallback=(80, 24))
    lines = min(max(size.lines, 1), MAX_WINSIZE_VALUE)
    columns = min(max(size.columns, 1), MAX_WINSIZE_VALUE)
    return struct.pack("HHHH", lines, columns, 0, 0)


def _result_from_os_error(
    args: tuple[str, ...],
    cwd: Path | None,
    exc: OSError,
) -> CommandResult:
    return CommandResult(
        args=args,
        cwd=cwd,
        returncode=_os_error_returncode(exc),
        stderr=str(exc),
    )


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _os_error_returncode(exc: OSError) -> int:
    if isinstance(exc, FileNotFoundError):
        return 127
    if isinstance(exc, PermissionError):
        return 126
    return exc.errno or 1
