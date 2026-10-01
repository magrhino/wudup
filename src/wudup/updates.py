"""Python implementation of the host ``bin/updates`` wrapper."""

from __future__ import annotations

import argparse
import errno
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .banner import print_startup_banner
from .command import _os_error_returncode
from .config import COMPOSE_IGNORE_PATHS_ENV, DIGEST_PIN_UPDATES_ENV
from .images import image_with_tag, tag_value_valid
from .line_specs import LineSpecError, parse_line_spec
from .naming import CONFIG_DIR_NAME, LEGACY_CONFIG_DIR_NAME, env_value
from .self_update import (
    ReleaseSelfUpdate,
    SelfUpdateInspectionError,
    github_release_self_update,
    self_update_display_numbers,
    self_update_enabled,
)
from .terminal import TerminalRenderer
from .truenas import (
    DEFAULT_TRUENAS_STATUS_TIMEOUT,
    TrueNasCallResult,
    _refresh_truenas_status,
    _truenas_active_alerts,
    _truenas_unreachable_message,
    _truenas_update_error_reason,
    _truenas_update_status,
    _truenas_update_version,
)
from .wud_file import WudTarget, parse_wud_text

DEFAULT_UPDATE_MODE = "stop"
DEFAULT_MAX_WAIT = "180"
DEFAULT_LOCK_TIMEOUT = "30"
SELF_UPDATE_SKIPPED_MESSAGE = "⏸️  Skipped WUDup self-update."
_SECONDS_RE = re.compile(r"^[0-9]+$")
_SHELL_SPACE_RE = re.compile(r"[ \t\n\r\v\f]")
_LEGACY_SHA_SUFFIX_RE = re.compile(r"^(.*\S)\s+sha256=\S+$")
_DOCKER_CLI_ENV_NAMES = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
    "DOCKER_API_VERSION",
)


class UpdatesError(RuntimeError):
    """Raised for a user-facing updates wrapper failure."""


@dataclass(frozen=True)
class TodoEntry:
    line_no: int
    raw: str
    first: str
    desired_tag: str

    @property
    def display_raw(self) -> str:
        match = _LEGACY_SHA_SUFFIX_RE.fullmatch(self.raw)
        if match is None:
            return self.raw
        return match.group(1)


@dataclass(frozen=True)
class UpdatesOptions:
    docker_base: str
    wud_file: str
    log_dir: str
    updater: str
    update_mode: str
    max_wait: str
    dry_run: bool = False
    auto_run: bool = False
    allow_tag_updates: bool = False
    out_uid: str = ""
    out_gid: str = ""
    lock_timeout: str = ""
    use_sudo: bool = False
    no_color: bool = False
    self_update: bool = True
    truenas_status_check: bool = False
    truenas_status_timeout: str = DEFAULT_TRUENAS_STATUS_TIMEOUT


@dataclass(frozen=True)
class SelfUpdatePreflightResult:
    status: int
    continue_updates: bool = True


@dataclass
class UpdatesFileLock:
    path: str
    timeout_seconds: str
    environ: Mapping[str, str]
    use_sudo: bool = False
    sleep: Callable[[float], None] = time.sleep
    lock_dir: Path = field(init=False)
    held: bool = field(default=False, init=False)
    via_sudo: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.lock_dir = Path(f"{self.path}.lock")

    def acquire(self) -> None:
        timeout = _parse_lock_timeout(self.timeout_seconds or DEFAULT_LOCK_TIMEOUT)
        waited = 0
        lock_parent = self.lock_dir.parent
        self.via_sudo = self.use_sudo and not os.access(lock_parent, os.W_OK)
        if not self.use_sudo and not os.access(lock_parent, os.W_OK):
            raise UpdatesError(
                "Cannot create WUD file lock without sudo: "
                f"{self.lock_dir} (parent is not writable: {lock_parent})"
            )

        while True:
            if self.via_sudo:
                if not self.lock_dir.exists() and self._sudo_mkdir():
                    self.held = True
                    return
            else:
                try:
                    os.mkdir(self.lock_dir)
                    self.held = True
                    return
                except OSError as exc:
                    if isinstance(exc, FileExistsError) or exc.errno == errno.EEXIST:
                        pass
                    elif self.use_sudo and self._sudo_mkdir():
                        self.via_sudo = True
                        self.held = True
                        return
                    else:
                        raise UpdatesError(
                            "Cannot create WUD file lock without sudo: "
                            f"{self.lock_dir}: {_format_os_error(exc)}"
                        ) from exc

            if waited >= timeout:
                raise UpdatesError(
                    f"Timed out waiting for WUD file lock: {self.lock_dir}"
                )
            self.sleep(1)
            waited += 1

    def release(self) -> None:
        if not self.held:
            return
        if self.via_sudo:
            subprocess.run(
                ["sudo", "rmdir", str(self.lock_dir)],
                env=dict(self.environ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.rmdir(self.lock_dir)
            except OSError:
                pass
        self.held = False

    def _sudo_mkdir(self) -> bool:
        result = subprocess.run(
            ["sudo", "mkdir", str(self.lock_dir)],
            env=dict(self.environ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

@dataclass(frozen=True)
class UpdateSelectionState:
    selected_line_spec: str = ""
    remove_line_spec: str = ""
    allow_tag_updates: bool = False
    tag_override_specs: Sequence[str] = ()
    exclude_tag_line_spec: str = ""
    recreate_excluded_services: bool = False


class UpdatesRunner:
    def __init__(
        self,
        options: UpdatesOptions,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.options = options
        self.environ = dict(os.environ if environ is None else environ)
        self.todo_entries: list[TodoEntry] = []
        self.renderer = TerminalRenderer(
            no_color=options.no_color,
            environ=self.environ,
        )
        self.lock = UpdatesFileLock(
            options.wud_file,
            options.lock_timeout or DEFAULT_LOCK_TIMEOUT,
            self.environ,
            use_sudo=options.use_sudo,
        )

    def run(self) -> int:
        try:
            return self._run()
        except UpdatesError as exc:
            print(exc, file=sys.stderr)
            return 1
        finally:
            self.lock.release()

    def _run(self) -> int:
        self.todo_entries = self._snapshot_todo_entries()
        self_update_status = self._run_self_update_preflight()
        if self_update_status is not None:
            if self_update_status.status != 0 or not self_update_status.continue_updates:
                return self_update_status.status
            self.todo_entries = self._snapshot_todo_entries()

        if self.renderer.rich_enabled():
            self.renderer.docker_updates(
                [
                    (display_no, entry.display_raw)
                    for display_no, entry in enumerate(self.todo_entries, start=1)
                ]
            )
        else:
            print("=== 📦 Docker Updates ===")
            if self.todo_entries:
                _display_todo_entries(self.todo_entries, env=self.environ)
            else:
                print("✅ No pending Docker updates!")

        if self.options.truenas_status_check:
            snapshot = _refresh_truenas_status(self.options, self.environ)
            print()
            self._print_system_update_status(snapshot.update)
            print()
            self._print_alert_status(snapshot.alerts)

        if not self.todo_entries:
            return 0

        if not os.access(self.options.updater, os.X_OK):
            print()
            if Path(self.options.updater).is_file():
                print(
                    "ℹ️  Updater script found but not executable: "
                    f"{self.options.updater}"
                )
                print(
                    "    Make it executable with: "
                    f"chmod +x \"{self.options.updater}\""
                )
            else:
                print(f"ℹ️  Updater script not found: {self.options.updater}")
            return 0

        if self.options.dry_run:
            print()
            print("👀 Dry-run mode: not running updates.")
            return 0

        print()
        if self.options.auto_run:
            selection = UpdateSelectionState(allow_tag_updates=self.options.allow_tag_updates)
            return self._run_updater(selection)

        selection = self._choose_update_lines()
        if selection is None:
            return 0

        self._lock_updater_handoff(selection)
        status = self._run_updater(selection)
        self.lock.release()
        return status

    def _run_self_update_preflight(self) -> SelfUpdatePreflightResult | None:
        if not self.options.self_update:
            return None
        if not os.access(self.options.updater, os.X_OK):
            return None

        display_numbers = self_update_display_numbers(self.todo_entries)
        if display_numbers:
            status = self._run_wud_file_self_update(display_numbers)
            if status is None:
                return None
            return SelfUpdatePreflightResult(status=status)

        try:
            release_update = github_release_self_update(self.environ)
        except SelfUpdateInspectionError as exc:
            raise UpdatesError(str(exc)) from exc
        if release_update is None:
            return None
        status = self._run_github_release_self_update(release_update)
        if status is None:
            return None
        return SelfUpdatePreflightResult(status=status, continue_updates=False)

    def _run_wud_file_self_update(self, display_numbers: Sequence[int]) -> int | None:
        lines = self._display_numbers_to_file_line_spec(display_numbers)
        allow_tag_updates = self.options.allow_tag_updates
        count = len(display_numbers)
        entry_label = "entry" if count == 1 else "entries"
        if not self._confirm_self_update(
            f"🔁 WUDup self-update detected in the WUD file "
            f"({count} {entry_label}).",
        ):
            return None

        if any(self.todo_entries[display - 1].desired_tag for display in display_numbers):
            allow_tag_updates = True

        selection = UpdateSelectionState(
            selected_line_spec=lines,
            allow_tag_updates=allow_tag_updates,
        )
        try:
            self._lock_updater_handoff(selection)
            return self._run_updater(selection)
        finally:
            self.lock.release()

    def _run_github_release_self_update(
        self,
        release_update: ReleaseSelfUpdate,
    ) -> int | None:
        if not self._confirm_self_update(
            "🔁 WUDup release update available: "
            f"{release_update.local_tag} -> {release_update.latest_tag}."
        ):
            return None

        if _self_update_desired_tag(release_update.target):
            return self._run_github_release_tag_update(release_update.target)
        return self._pull_self_update_image(release_update.target)

    def _run_github_release_tag_update(self, target: str) -> int:
        with tempfile.TemporaryDirectory(prefix="wud-self-update.") as tmpdir:
            todo_file = Path(tmpdir) / "images.todo"
            todo_file.write_text(f"{target}\n", encoding="utf-8")
            selection = UpdateSelectionState(allow_tag_updates=True)
            return self._run_updater(selection, wud_file=str(todo_file))

    def _pull_self_update_image(self, target: str) -> int:
        image = _self_update_pull_image(target)
        if image == "":
            raise UpdatesError("Could not determine WUDup image to pull.")

        docker_env = _docker_cli_env(self.environ) if self.options.use_sudo else []
        if self.options.use_sudo and docker_env:
            print(
                "🚀 Pulling WUDup image via: sudo env "
                f"{' '.join(docker_env)} docker pull {image}"
            )
            command = ["sudo", "env", *docker_env, "docker", "pull", image]
        elif self.options.use_sudo:
            print(f"🚀 Pulling WUDup image via: sudo docker pull {image}")
            command = ["sudo", "docker", "pull", image]
        else:
            print(f"🚀 Pulling WUDup image via: docker pull {image}")
            command = ["docker", "pull", image]

        try:
            result = subprocess.run(command, env=self.environ, check=False)
            returncode = result.returncode
        except OSError as exc:
            print(exc, file=sys.stderr)
            returncode = _os_error_returncode(exc)

        print()
        if returncode == 0:
            print(f"✅ WUDup image pull completed (exit {returncode}).")
            print("Please restart the wudup container before running updates again.")
        else:
            print(f"❌ WUDup image pull failed (exit {returncode}).")
        return returncode

    def _confirm_self_update(self, message: str) -> bool:
        print()
        print(message)
        if self.options.dry_run:
            print("👀 Dry-run mode: not running WUDup self-update.")
            return False
        if self.options.auto_run:
            return True

        while True:
            if self.renderer.rich_enabled():
                reply = self.renderer.prompt_choice(
                    "Update WUDup before other Docker updates?",
                    "[Y] yes   [n] no",
                )
            else:
                reply = _prompt_or_none(
                    "Update WUDup before other Docker updates? (Y/n) "
                )
            if reply is None:
                print(SELF_UPDATE_SKIPPED_MESSAGE)
                return False
            choice = reply.strip().casefold()
            if choice in {"y", "yes"}:
                return True
            if choice == "":
                if sys.stdin.isatty():
                    return True
                print(SELF_UPDATE_SKIPPED_MESSAGE)
                return False
            if choice in {"n", "no"}:
                print(SELF_UPDATE_SKIPPED_MESSAGE)
                return False
            print("Invalid choice. Enter y or n.")

    def _snapshot_todo_entries(self) -> list[TodoEntry]:
        wud_file = Path(self.options.wud_file)
        try:
            file_stat = wud_file.stat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            if not self.options.use_sudo:
                raise UpdatesError(
                    "Cannot stat WUD file without sudo: "
                    f"{wud_file}: {_format_os_error(exc)}"
                ) from exc
        else:
            if file_stat.st_size <= 0:
                return []

        if not self.options.use_sudo and not os.access(wud_file, os.R_OK):
            raise UpdatesError(
                "Cannot read WUD file without sudo: "
                f"{wud_file} (file is not readable)"
            )

        if not self.options.use_sudo and not os.access(wud_file.parent, os.W_OK):
            raise UpdatesError(
                "Cannot create WUD file lock without sudo: "
                f"{wud_file}.lock (parent is not writable: {wud_file.parent})"
            )

        self.lock.acquire()
        try:
            return self._read_todo_entries()
        finally:
            self.lock.release()

    def _read_todo_entries(self) -> list[TodoEntry]:
        wud_file = Path(self.options.wud_file)
        try:
            text = wud_file.read_text(encoding="utf-8")
        except OSError as exc:
            if not self.options.use_sudo:
                raise UpdatesError(
                    "Cannot read WUD file without sudo: "
                    f"{wud_file}: {_format_os_error(exc)}"
                ) from exc
            return _read_todo_entries_with_sudo(self.options.wud_file, self.environ)
        return _parse_todo_entries(text)

    def _print_system_update_status(self, result: TrueNasCallResult) -> None:
        if not result.ok:
            line = _truenas_unreachable_message("system update check", result.reason)
            self.renderer.truenas_panel(
                "TrueNAS System",
                [(line, "info")],
                plain_header="=== 🖥️ TrueNAS System Update ===",
            )
            return

        status = _truenas_update_status(result.data)
        if status == "UNAVAILABLE":
            line = "✅ System up to date"
            kind = "success"
        elif status == "AVAILABLE":
            version = _truenas_update_version(result.data)
            suffix = f" ({version})" if version else ""
            line = f"⚠️  System update available!{suffix}"
            kind = "warning"
        elif status == "ERROR":
            reason = _truenas_update_error_reason(result.data) or "<no response>"
            line = f"❓ TrueNAS update status error: {reason}"
            kind = "error"
        else:
            line = f"❓ Unknown status: {status or '<no response>'}"
            kind = "error"

        self.renderer.truenas_panel(
            "TrueNAS System",
            [(line, kind)],
            plain_header="=== 🖥️ TrueNAS System Update ===",
        )

    def _print_alert_status(self, result: TrueNasCallResult) -> None:
        if not result.ok:
            line = _truenas_unreachable_message("alert check", result.reason)
            self.renderer.truenas_panel(
                "TrueNAS Alerts",
                [(line, "info")],
                plain_header="=== 🚨 TrueNAS Alerts ===",
            )
            return

        alerts = _truenas_active_alerts(result.data)
        if alerts is None:
            line = _truenas_unreachable_message(
                "alert check",
                "invalid alert response",
            )
            self.renderer.truenas_panel(
                "TrueNAS Alerts",
                [(line, "info")],
                plain_header="=== 🚨 TrueNAS Alerts ===",
            )
            return
        if alerts:
            if self.renderer.rich_enabled():
                self.renderer.truenas_panel(
                    "TrueNAS Alerts",
                    [
                        (f"{index}. {alert}", "warning")
                        for index, alert in enumerate(alerts, start=1)
                    ],
                    plain_header="=== 🚨 TrueNAS Alerts ===",
                )
            else:
                print("=== 🚨 TrueNAS Alerts ===")
                _print_numbered_lines("\n".join(alerts), self.environ)
        else:
            self.renderer.truenas_panel(
                "TrueNAS Alerts",
                [("✅ No active alerts", "success")],
                plain_header="=== 🚨 TrueNAS Alerts ===",
            )

    def _choose_update_lines(self) -> UpdateSelectionState | None:
        todo_count = len(self.todo_entries)
        selected_display: list[int] = []
        unselected_display: list[int] = []

        while True:
            choice = self.renderer.prompt_choice(
                "Run Docker updates?",
                "[a=all, s=select, x=exclude, n=skip]",
            )
            if choice in ("", "n", "N", "no", "NO"):
                print("⏸️  Skipped running updates.")
                return None
            if choice in ("a", "A", "all", "ALL", "y", "Y", "yes", "YES"):
                selected_display = list(range(1, todo_count + 1))
                return self._prompt_tag_updates(selected_display, "", "")

            if choice in ("s", "S", "select", "SELECT"):
                selected_display = self._read_display_selection(
                    "Enter update numbers/ranges to select: ",
                    todo_count,
                )
                if not selected_display:
                    return None
                unselected_display = _complement_display_numbers(
                    selected_display,
                    todo_count,
                )
                break

            if choice in ("x", "X", "exclude", "EXCLUDE"):
                unselected_display = self._read_display_selection(
                    "Enter update numbers/ranges to exclude: ",
                    todo_count,
                )
                if not unselected_display:
                    return None
                selected_display = _complement_display_numbers(
                    unselected_display,
                    todo_count,
                )
                break

            print("Invalid choice. Enter a, s, x, or n.")

        if not selected_display:
            print("⏸️  No updates selected; skipped running updates.")
            return None

        selected_line_spec = self._display_numbers_to_file_line_spec(
            selected_display
        )
        print(f"Selected {len(selected_display)} of {todo_count} pending update(s).")
        selection = self._prompt_tag_updates(selected_display, selected_line_spec, "")

        remove_line_spec = ""
        if unselected_display:
            remove_reply = _prompt(
                "Remove unselected entries from the WUD file before running? (y/N) "
            )
            if remove_reply in ("y", "Y", "yes", "YES", "Yes"):
                remove_line_spec = self._display_numbers_to_file_line_spec(
                    unselected_display
                )

        if remove_line_spec == "":
            return selection
        return UpdateSelectionState(
            selected_line_spec=selection.selected_line_spec,
            remove_line_spec=remove_line_spec,
            allow_tag_updates=selection.allow_tag_updates,
            tag_override_specs=selection.tag_override_specs,
            exclude_tag_line_spec=selection.exclude_tag_line_spec,
            recreate_excluded_services=selection.recreate_excluded_services,
        )

    def _prompt_tag_updates(
        self,
        display_numbers: Sequence[int],
        selected_line_spec: str,
        remove_line_spec: str,
    ) -> UpdateSelectionState:
        tag_entries = [
            (display, self.todo_entries[display - 1])
            for display in display_numbers
            if self.todo_entries[display - 1].desired_tag
        ]

        allow_tag_updates = self.options.allow_tag_updates
        tag_override_specs: list[str] = []
        exclude_tag_line_spec = ""
        recreate_excluded_services = False

        def _build_state() -> UpdateSelectionState:
            return UpdateSelectionState(
                selected_line_spec=selected_line_spec,
                remove_line_spec=remove_line_spec,
                allow_tag_updates=allow_tag_updates,
                tag_override_specs=tuple(tag_override_specs),
                exclude_tag_line_spec=exclude_tag_line_spec,
                recreate_excluded_services=recreate_excluded_services,
            )

        if not tag_entries:
            return _build_state()

        print("Selected tag update(s):")
        for display, entry in tag_entries:
            desired_tag = entry.desired_tag
            print(
                f"  {display}. {entry.first} -> "
                f"{image_with_tag(entry.first, desired_tag)}"
            )

        change_tags = False
        if not allow_tag_updates:
            while True:
                if self.renderer.rich_enabled():
                    reply = self.renderer.prompt_choice(
                        "Apply selected tag update entries?",
                        "[y] yes   [n] no   [c] change   [e] exclude",
                    )
                else:
                    reply = _prompt(
                        "Apply selected tag update entries? "
                        "[y]es/[n]o/[c]hange/[e]xclude (default n): "
                    )
                choice = reply.strip().casefold()
                if choice in {"y", "yes"}:
                    allow_tag_updates = True
                    break
                if choice in {"", "n", "no"}:
                    return _build_state()
                if choice in {"c", "change"}:
                    allow_tag_updates = True
                    change_tags = True
                    break
                if choice in {"e", "exclude"}:
                    line_spec = self._select_tag_exclusion_line_spec(tag_entries)
                    if line_spec:
                        exclude_tag_line_spec = line_spec
                        recreate_excluded_services = (
                            self._confirm_recreate_exclusions()
                        )
                    return _build_state()
                print("Invalid choice. Enter y, n, c, or e.")

        if not change_tags:
            return _build_state()

        for display, entry in tag_entries:
            current_tag = entry.desired_tag
            while True:
                reply = _prompt(
                    f"Override tag for update {display} [{current_tag}]: "
                ).strip()
                if reply == "":
                    break
                if tag_value_valid(reply):
                    if reply != current_tag:
                        tag_override_specs.append(f"{entry.line_no}={reply}")
                    break
                print("Invalid tag. Use a Docker tag value like 5.2.0.")

        return _build_state()

    def _select_tag_exclusion_line_spec(
        self,
        tag_entries: Sequence[tuple[int, TodoEntry]],
    ) -> str:
        if len(tag_entries) == 1:
            return str(tag_entries[0][1].line_no)

        tag_display_numbers = {display for display, _entry in tag_entries}
        while True:
            selected_display = self._read_tag_exclusion_selection()
            if not selected_display:
                return ""
            if all(display in tag_display_numbers for display in selected_display):
                selected_set = set(selected_display)
                return ",".join(
                    _unique_in_order(
                        str(entry.line_no)
                        for display, entry in tag_entries
                        if display in selected_set
                    )
                )
            print(
                "Invalid tag selection. "
                "Use listed tag update numbers/ranges like 1,3-5."
            )

    def _confirm_recreate_exclusions(self) -> bool:
        while True:
            if self.renderer.rich_enabled():
                reply = self.renderer.prompt_choice(
                    "Recreate affected services so WUD sees exclusions now?",
                    "[Y] yes   [n] no",
                )
            else:
                reply = _prompt(
                    "Recreate affected services so WUD sees exclusions now? (Y/n) "
                )
            choice = reply.strip().casefold()
            if choice in {"", "y", "yes"}:
                return True
            if choice in {"n", "no"}:
                return False
            print("Invalid choice. Enter y or n.")

    def _read_display_selection(self, prompt: str, todo_count: int) -> list[int]:
        while True:
            reply = _prompt(prompt)
            if _SHELL_SPACE_RE.sub("", reply) == "":
                print("⏸️  No selection; skipped running updates.")
                return []
            try:
                return _parse_display_spec(reply, todo_count)
            except ValueError:
                print("Invalid selection. Use numbers/ranges like 1,3-5.")

    def _read_tag_exclusion_selection(self) -> list[int]:
        while True:
            reply = _prompt("Enter tag update numbers/ranges to exclude: ")
            if _SHELL_SPACE_RE.sub("", reply) == "":
                print("⏸️  No tag exclusions selected.")
                return []
            try:
                return _parse_display_spec(reply, len(self.todo_entries))
            except ValueError:
                print(
                    "Invalid tag selection. "
                    "Use listed tag update numbers/ranges like 1,3-5."
                )

    def _display_numbers_to_file_line_spec(self, display_numbers: Iterable[int]) -> str:
        line_numbers: list[str] = []
        for display in display_numbers:
            line_numbers.append(str(self.todo_entries[display - 1].line_no))
        return ",".join(_unique_in_order(line_numbers))

    def _lock_updater_handoff(self, selection: UpdateSelectionState) -> None:
        if (
            selection.selected_line_spec == ""
            and selection.remove_line_spec == ""
            and selection.exclude_tag_line_spec == ""
            and not selection.tag_override_specs
        ):
            return
        self.lock.acquire()
        if not self._selected_lines_match_snapshot(selection):
            self.lock.release()
            raise UpdatesError(
                "WUD file changed while selecting updates; please rerun updates."
            )

    def _selected_lines_match_snapshot(self, selection: UpdateSelectionState) -> bool:
        selected_items = [
            *selection.selected_line_spec.split(","),
            *selection.remove_line_spec.split(","),
            *selection.exclude_tag_line_spec.split(","),
            *(
                override.partition("=")[0]
                for override in selection.tag_override_specs
            ),
        ]
        wanted = {
            int(item)
            for item in selected_items
            if item
        }
        expected = [
            (entry.line_no, entry.raw)
            for entry in self.todo_entries
            if entry.line_no in wanted
        ]
        current_entries = self._read_todo_entries()
        current = [
            (entry.line_no, entry.raw)
            for entry in current_entries
            if entry.line_no in wanted
        ]
        return expected == current

    def _run_updater(self, selection: UpdateSelectionState, *, wud_file: str | None = None) -> int:
        target_wud_file = wud_file or self.options.wud_file
        updater_args = [
            "--base",
            self.options.docker_base,
            "--file",
            target_wud_file,
            "--log-dir",
            self.options.log_dir,
            "--mode",
            self.options.update_mode,
            "--max-wait",
            self.options.max_wait,
        ]
        if selection.selected_line_spec:
            updater_args.extend(["--only-lines", selection.selected_line_spec])
        if selection.remove_line_spec:
            updater_args.extend(["--remove-lines-before-run", selection.remove_line_spec])
        if selection.allow_tag_updates:
            updater_args.append("--allow-tag-updates")
        for override in selection.tag_override_specs:
            updater_args.extend(["--tag-override", override])
        if selection.exclude_tag_line_spec:
            updater_args.extend(["--exclude-tag-lines", selection.exclude_tag_line_spec])
        if selection.recreate_excluded_services:
            updater_args.append("--recreate-excluded-services")
        if self.options.no_color:
            updater_args.append("--no-color")
        updater_args.append("--yes")

        updater_env: list[str] = []
        if self.options.out_uid or self.options.out_gid:
            if self.options.out_uid:
                updater_env.append(f"OUT_UID={self.options.out_uid}")
            if self.options.out_gid:
                updater_env.append(f"OUT_GID={self.options.out_gid}")
        if self.options.use_sudo and self.environ.get("WUD_DB_PATH"):
            updater_env.append(f"WUD_DB_PATH={self.environ['WUD_DB_PATH']}")
        if self.options.use_sudo and self.environ.get("HOST_DOCKER_BASE"):
            updater_env.append(f"HOST_DOCKER_BASE={self.environ['HOST_DOCKER_BASE']}")
        if self.options.use_sudo and COMPOSE_IGNORE_PATHS_ENV in self.environ:
            updater_env.append(
                f"{COMPOSE_IGNORE_PATHS_ENV}={self.environ[COMPOSE_IGNORE_PATHS_ENV]}"
            )
        if self.options.use_sudo and DIGEST_PIN_UPDATES_ENV in self.environ:
            updater_env.append(
                f"{DIGEST_PIN_UPDATES_ENV}={self.environ[DIGEST_PIN_UPDATES_ENV]}"
            )
        if self.options.lock_timeout:
            updater_env.append(f"WUD_LOCK_TIMEOUT={self.options.lock_timeout}")
        if self.lock.held:
            updater_env.append("WUD_LOCK_HELD_BY_PARENT=1")

        updater_environ = dict(self.environ)
        for assignment in updater_env:
            key, value = assignment.split("=", 1)
            updater_environ[key] = value

        if self.options.use_sudo and updater_env:
            print(
                "🚀 Running Docker updates via: sudo env "
                f"{' '.join(updater_env)} \"{self.options.updater}\" "
                f"{' '.join(updater_args)}"
            )
            command = ["sudo", "env", *updater_env, self.options.updater, *updater_args]
            command_env = self.environ
        elif self.options.use_sudo:
            print(
                "🚀 Running Docker updates via: sudo "
                f"\"{self.options.updater}\" {' '.join(updater_args)}"
            )
            command = ["sudo", self.options.updater, *updater_args]
            command_env = self.environ
        elif updater_env:
            print(
                "🚀 Running Docker updates via: env "
                f"{' '.join(updater_env)} \"{self.options.updater}\" "
                f"{' '.join(updater_args)}"
            )
            command = [self.options.updater, *updater_args]
            command_env = updater_environ
        else:
            print(
                "🚀 Running Docker updates via: "
                f"\"{self.options.updater}\" {' '.join(updater_args)}"
            )
            command = [self.options.updater, *updater_args]
            command_env = updater_environ

        try:
            result = subprocess.run(command, env=command_env, check=False)
            returncode = result.returncode
        except OSError as exc:
            print(exc, file=sys.stderr)
            returncode = _os_error_returncode(exc)

        print()
        if returncode == 0:
            print(f"✅ Update script completed (exit {returncode}).")
        else:
            print(f"❌ Update script failed (exit {returncode}).")
        return returncode


def run_updates_from_namespace(
    args: argparse.Namespace,
    *,
    repo_root: str | Path,
    environ: Mapping[str, str] | None = None,
    show_banner: bool = False,
) -> int:
    env = load_configured_environ(
        environ,
        config_file=getattr(args, "config_file", None),
    )
    if show_banner:
        print_startup_banner(
            environ=env,
            no_color=bool(getattr(args, "no_color", False)),
        )
    try:
        options = options_from_namespace(args, repo_root=repo_root, environ=env)
    except UpdatesError as exc:
        print(exc, file=sys.stderr)
        return 1
    return UpdatesRunner(options, environ=env).run()



def options_from_namespace(
    args: argparse.Namespace,
    *,
    repo_root: str | Path,
    environ: Mapping[str, str],
) -> UpdatesOptions:
    home = environ.get("HOME") or str(Path.home())
    docker_base = _arg_or_default(
        getattr(args, "base", None),
        environ.get("DOCKER_BASE") or f"{home}/docker",
    )

    env_wud_file = environ.get("WUD_OUT_FILE") or ""
    if getattr(args, "file", None) is not None:
        wud_file = str(args.file)
    elif env_wud_file:
        wud_file = env_wud_file
    else:
        wud_file = f"{docker_base}/wud/out/images.todo"

    log_dir = _arg_or_default(
        getattr(args, "log_dir", None),
        environ.get("WUD_LOG_DIR") or "./logs",
    )
    update_mode = _arg_or_default(
        getattr(args, "mode", None),
        environ.get("WUD_UPDATE_MODE") or DEFAULT_UPDATE_MODE,
    )
    max_wait = _arg_or_default(
        getattr(args, "max_wait", None),
        environ.get("WUD_MAX_WAIT") or DEFAULT_MAX_WAIT,
    )
    out_gid = environ.get("OUT_GID") or environ.get("OUT_GUID") or ""

    return UpdatesOptions(
        docker_base=docker_base,
        wud_file=wud_file,
        log_dir=log_dir,
        updater=env_value(environ, "WUDUP_UPDATER", "WUD_UPDATER")
        or str(Path(repo_root) / "bin" / "docker-update-from-wud"),
        update_mode=update_mode,
        max_wait=max_wait,
        dry_run=bool(getattr(args, "dry_run", False)),
        auto_run=bool(getattr(args, "yes", False)),
        allow_tag_updates=bool(getattr(args, "allow_tag_updates", False)),
        out_uid=environ.get("OUT_UID") or "",
        out_gid=out_gid,
        lock_timeout=environ.get("WUD_LOCK_TIMEOUT") or "",
        use_sudo=_resolve_use_sudo(
            env_value(environ, "WUDUP_USE_SUDO", "WUD_UPDATER_USE_SUDO"),
            no_updater_sudo=bool(getattr(args, "no_updater_sudo", False)),
        ),
        no_color=bool(getattr(args, "no_color", False)),
        self_update=self_update_enabled(
            environ,
            cli_value=getattr(args, "self_update", None),
        ),
        truenas_status_check=_resolve_bool_env(
            environ.get("TRUENAS_STATUS_CHECK"),
            "TRUENAS_STATUS_CHECK",
            default=False,
        ),
        truenas_status_timeout=(
            environ.get("TRUENAS_STATUS_TIMEOUT") or DEFAULT_TRUENAS_STATUS_TIMEOUT
        ),
    )


def load_configured_environ(
    environ: Mapping[str, str] | None = None,
    config_file: str | None = None,
) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    explicit_config_file = config_file is not None
    if config_file:
        env["WUDUP_CONFIG"] = config_file
        env["WUD_UPDATER_CONFIG"] = config_file
    if _config_already_sourced(env, explicit_config_file=explicit_config_file):
        return env
    config_file = _configured_env_file(env)
    if not config_file.is_file() or not os.access(config_file, os.R_OK):
        return env

    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                'set -a; . "$1"; env; exit 0',
                "wudup-config",
                str(config_file),
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return env

    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _config_already_sourced(
    env: Mapping[str, str],
    *,
    explicit_config_file: bool,
) -> bool:
    return (
        env.get("WUDUP_CONFIG_SOURCED") == "1"
        or env.get("WUD_UPDATER_CONFIG_SOURCED") == "1"
    ) and not explicit_config_file


def _configured_env_file(env: Mapping[str, str]) -> Path:
    home = env.get("HOME") or str(Path.home())
    configured = env_value(env, "WUDUP_CONFIG", "WUD_UPDATER_CONFIG")
    default_config_file = Path(home) / ".config" / CONFIG_DIR_NAME / "env"
    legacy_config_file = Path(home) / ".config" / LEGACY_CONFIG_DIR_NAME / "env"
    if configured:
        return Path(configured)
    if legacy_config_file.is_file() and not default_config_file.is_file():
        return legacy_config_file
    return default_config_file


def _parse_todo_entries(text: str) -> list[TodoEntry]:
    parsed = parse_wud_text(text)
    return [_todo_entry_from_target(target) for target in parsed.targets]


def _self_update_pull_image(target: str) -> str:
    entry = _todo_entry_from_raw(1, target)
    if entry is None:
        return ""

    if entry.desired_tag:
        return image_with_tag(entry.first, entry.desired_tag)
    return entry.first


def _self_update_desired_tag(target: str) -> str:
    entry = _todo_entry_from_raw(1, target)
    return "" if entry is None else entry.desired_tag


def _todo_entry_from_target(target: WudTarget) -> TodoEntry:
    return TodoEntry(
        line_no=target.line_no,
        raw=target.raw,
        first=target.first,
        desired_tag=target.desired_tag,
    )


def _todo_entry_from_raw(line_no: int, raw: str) -> TodoEntry | None:
    parsed = parse_wud_text(raw)
    if not parsed.targets:
        return None
    target = parsed.targets[0]
    return TodoEntry(
        line_no=line_no,
        raw=raw,
        first=target.first,
        desired_tag=target.desired_tag,
    )


def _docker_cli_env(environ: Mapping[str, str]) -> list[str]:
    result: list[str] = []
    for name in _DOCKER_CLI_ENV_NAMES:
        value = environ.get(name)
        if value:
            result.append(f"{name}={value}")
    return result


def _read_todo_entries_with_sudo(
    wud_file: str,
    environ: Mapping[str, str],
) -> list[TodoEntry]:
    awk_script = (
        "{ trimmed = $0; sub(/^[[:space:]]+/, \"\", trimmed); "
        'if (trimmed != "" && trimmed !~ /^#/) { print NR "\\t" $0 } }'
    )
    try:
        result = subprocess.run(
            ["sudo", "awk", awk_script, wud_file],
            env=dict(environ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise UpdatesError(
            f"Unable to read WUD file with sudo: {wud_file}: {_format_os_error(exc)}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise UpdatesError(f"Unable to read WUD file with sudo: {wud_file}{suffix}")

    entries: list[TodoEntry] = []
    for line in result.stdout.splitlines():
        line_no, sep, raw = line.partition("\t")
        if not sep:
            continue
        try:
            entry = _todo_entry_from_raw(int(line_no, 10), raw)
            if entry is not None:
                entries.append(entry)
        except ValueError:
            continue
    return entries


def _display_todo_entries(
    entries: Sequence[TodoEntry],
    *,
    env: Mapping[str, str],
) -> None:
    display = "".join(
        f"{display_no}\t{entry.display_raw}\n"
        for display_no, entry in enumerate(entries, start=1)
    )
    try:
        result = subprocess.run(
            ["column", "-t"],
            input=display,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        print(display, end="")
        return
    print(result.stdout, end="")


def _parse_display_spec(spec: str, max_value: int) -> list[int]:
    cleaned = _SHELL_SPACE_RE.sub("", spec)
    if cleaned == "" or any(part == "" for part in cleaned.split(",")):
        raise ValueError("empty selection")
    try:
        return parse_line_spec(cleaned, max_value, "selection")
    except LineSpecError as exc:
        raise ValueError(str(exc)) from exc


def _complement_display_numbers(selected: Sequence[int], max_value: int) -> list[int]:
    selected_set = set(selected)
    return [value for value in range(1, max_value + 1) if value not in selected_set]


def _unique_in_order(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _prompt(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _prompt_or_none(prompt: str) -> str | None:
    try:
        return input(prompt)
    except EOFError:
        return None


def _parse_lock_timeout(value: str) -> int:
    return _parse_seconds(value, "WUD_LOCK_TIMEOUT")


def _parse_seconds(value: str, label: str) -> int:
    if _SECONDS_RE.fullmatch(str(value)) is None:
        raise UpdatesError(f"{label} must be an integer number of seconds")
    return int(str(value), 10)


def _resolve_use_sudo(
    value: str | None,
    *,
    no_updater_sudo: bool,
) -> bool:
    if no_updater_sudo:
        return False
    if value is None or value == "":
        return False

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise UpdatesError(
        "WUDUP_USE_SUDO must be one of true, false, 1, 0, yes, no, on, or off"
    )


def _resolve_bool_env(value: str | None, label: str, *, default: bool) -> bool:
    if value is None or value == "":
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise UpdatesError(
        f"{label} must be one of true, false, 1, 0, yes, no, on, or off"
    )


def _format_os_error(exc: OSError) -> str:
    return exc.strerror or str(exc)




def _print_numbered_lines(value: str, environ: Mapping[str, str]) -> None:
    text = "\n".join(value.splitlines()) + "\n"
    try:
        result = subprocess.run(
            ["nl", "-ba"],
            input=text,
            env=dict(environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        for index, line in enumerate(value.splitlines(), start=1):
            print(f"{index}\t{line}")
        return
    print(result.stdout, end="")


def _arg_or_default(value: object, default: str) -> str:
    if value is None:
        return default
    return str(value)
