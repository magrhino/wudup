from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from wudup.banner import current_tag
from wudup.cli import main
from wudup.db import init_db, open_db, utc_timestamp
from wudup.web_auth import PASSWORD_HASHER


class CliTests(unittest.TestCase):
    def test_legacy_import_package_aliases_wudup(self) -> None:
        legacy_cli = importlib.import_module("wud_updater.cli")
        legacy_self_update = importlib.import_module("wud_updater.self_update")

        self.assertIs(legacy_cli.main, main)
        self.assertEqual(
            legacy_self_update.DEFAULT_SELF_UPDATE_REPOSITORY,
            "ghcr.io/magrhino/wudup",
        )

    def test_legacy_submodule_import_works_in_fresh_process(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        src_path = str(repo_root / "src")
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            src_path
            if not existing_pythonpath
            else f"{src_path}{os.pathsep}{existing_pythonpath}"
        )

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import wud_updater.banner; print(wud_updater.banner.__name__)",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "wudup.banner")

    def _run_main(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_update_from_wud_dry_run_accepts_shell_flags(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wud-python-cli.") as tmpdir:
            root = Path(tmpdir)
            base = root / "base"
            wud_file = root / "images.todo"
            log_dir = root / "logs"
            base.mkdir()
            wud_file.write_text("", encoding="utf-8")

            status, stdout, stderr = self._run_main(
                [
                    "update-from-wud",
                    "--dry-run",
                    "--base",
                    str(base),
                    "--file",
                    str(wud_file),
                    "--log-dir",
                    str(log_dir),
                    "--mode",
                    "pause",
                    "--max-wait",
                    "0",
                    "--yes",
                    "--allow-tag-updates",
                    "--no-color",
                    "--only-lines",
                    "",
                    "--remove-lines-before-run",
                    "",
                ]
            )

        self.assertEqual(status, 0)
        self.assertIn(f"Base    : {base}", stdout)
        self.assertIn("Nothing to do; list is empty.", stdout)
        self.assertEqual(stderr, "")

    def test_update_from_wud_rejects_invalid_max_wait(self) -> None:
        status, _stdout, stderr = self._run_main(
            ["update-from-wud", "--max-wait", "not-a-number"]
        )

        self.assertEqual(status, 1)
        self.assertIn("--max-wait must be an integer number of seconds", stderr)

    def test_update_from_wud_uses_environment_defaults(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wud-python-cli.") as tmpdir:
            root = Path(tmpdir)
            base = root / "env-base"
            wud_file = root / "env-images.todo"
            log_dir = root / "env-logs"
            base.mkdir()
            wud_file.write_text("", encoding="utf-8")

            env = {
                "DOCKER_BASE": str(base),
                "WUD_OUT_FILE": str(wud_file),
                "WUD_LOG_DIR": str(log_dir),
                "WUD_UPDATE_MODE": "live",
                "WUD_MAX_WAIT": "0",
                "WUDUP_BANNER": "false",
                "WUDUP_RELEASE_CHECK": "false",
                "PATH": os.environ.get("PATH", ""),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                status, stdout, stderr = self._run_main(
                    [
                        "update-from-wud",
                        "--dry-run",
                        "--yes",
                        "--no-color",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertIn(f"Base    : {base}", stdout)
        self.assertIn(f"WUD file: {wud_file}", stdout)
        self.assertIn(f"Log file: {log_dir}", stdout)
        self.assertIn("Mode    : live", stdout)
        self.assertIn("MaxWait : 0s", stdout)
        self.assertEqual(stderr, "")

    def test_update_from_wud_rejects_invalid_environment_max_wait(self) -> None:
        env = {
            "WUD_MAX_WAIT": "not-a-number",
            "WUDUP_BANNER": "false",
            "WUDUP_RELEASE_CHECK": "false",
            "PATH": os.environ.get("PATH", ""),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            status, _stdout, stderr = self._run_main(["update-from-wud"])

        self.assertEqual(status, 1)
        self.assertIn("WUD_MAX_WAIT must be an integer number of seconds", stderr)

    def test_update_from_wud_uses_environment_log_dir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wud-python-cli.") as tmpdir:
            root = Path(tmpdir)
            base = root / "base"
            wud_file = root / "images.todo"
            log_dir = root / "env-logs"
            base.mkdir()
            wud_file.write_text("", encoding="utf-8")

            with mock.patch.dict(os.environ, {"WUD_LOG_DIR": str(log_dir)}):
                status, stdout, stderr = self._run_main(
                    [
                        "update-from-wud",
                        "--dry-run",
                        "--base",
                        str(base),
                        "--file",
                        str(wud_file),
                        "--max-wait",
                        "0",
                        "--yes",
                        "--no-color",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertIn(f"Log file: {log_dir}", stdout)
        self.assertEqual(stderr, "")

    def test_update_from_wud_prints_forced_startup_banner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wud-python-cli.") as tmpdir:
            root = Path(tmpdir)
            base = root / "base"
            wud_file = root / "images.todo"
            log_dir = root / "logs"
            base.mkdir()
            wud_file.write_text("", encoding="utf-8")

            env = {
                "WUDUP_BANNER": "true",
                "WUDUP_RELEASE_CHECK": "false",
                "PATH": os.environ.get("PATH", ""),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                status, stdout, stderr = self._run_main(
                    [
                        "update-from-wud",
                        "--dry-run",
                        "--base",
                        str(base),
                        "--file",
                        str(wud_file),
                        "--log-dir",
                        str(log_dir),
                        "--max-wait",
                        "0",
                        "--yes",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertIn(f"WUDup {current_tag()}", stdout)
        self.assertIn("Nothing to do; list is empty.", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_subcommand_accepts_container_path_options(self) -> None:
        with mock.patch(
            "wudup.cli.run_doctor_from_namespace",
            return_value=17,
        ) as run_doctor:
            status, stdout, stderr = self._run_main(
                [
                    "doctor",
                    "--base",
                    "/srv/docker",
                    "--file",
                    "/out/images.todo",
                    "--log-dir",
                    "/logs",
                    "--no-color",
                ]
            )

        self.assertEqual(status, 17)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        args = run_doctor.call_args.args[0]
        self.assertEqual(args.base, "/srv/docker")
        self.assertEqual(args.file, "/out/images.todo")
        self.assertEqual(args.log_dir, "/logs")
        self.assertTrue(args.no_color)

    def test_web_subcommand_accepts_server_and_state_options(self) -> None:
        with mock.patch("wudup.cli._run_web", return_value=19) as run_web:
            status, stdout, stderr = self._run_main(
                [
                    "web",
                    "--base",
                    "/srv/docker",
                    "--file",
                    "/out/images.todo",
                    "--log-dir",
                    "/logs",
                    "--db-path",
                    "/state/wud.sqlite",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8081",
                    "--static-dir",
                    "/app/webui",
                ]
            )

        self.assertEqual(status, 19)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        args = run_web.call_args.args[0]
        self.assertEqual(args.base, "/srv/docker")
        self.assertEqual(args.file, "/out/images.todo")
        self.assertEqual(args.log_dir, "/logs")
        self.assertEqual(args.db_path, "/state/wud.sqlite")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, "8081")
        self.assertEqual(args.static_dir, "/app/webui")

    def test_web_reset_admin_accepts_user_and_db_path(self) -> None:
        with mock.patch("wudup.cli._run_web", return_value=21) as run_web:
            status, stdout, stderr = self._run_main(
                [
                    "web",
                    "reset-admin",
                    "--user",
                    "admin",
                    "--db-path",
                    "/state/wud.sqlite",
                ]
            )

        self.assertEqual(status, 21)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        args = run_web.call_args.args[0]
        self.assertEqual(args.web_command, "reset-admin")
        self.assertEqual(args.user, "admin")
        self.assertEqual(args.db_path, "/state/wud.sqlite")

    def test_init_subcommand_accepts_configuration_options(self) -> None:
        with mock.patch("wudup.cli._run_init", return_value=23) as run_init:
            status, stdout, stderr = self._run_main(
                [
                    "init",
                    "--profile",
                    "webui",
                    "--config-file",
                    "/etc/wud/webui.env",
                    "--compose-override",
                    "/srv/wud/override.yml",
                    "--stack-root",
                    "/srv/docker",
                    "--log-dir",
                    "/srv/wud/logs",
                    "--db-path",
                    "/srv/wud/logs/wud.sqlite",
                    "--uid",
                    "1000",
                    "--gid",
                    "1000",
                    "--web-exposure",
                    "reverse-proxy",
                    "--web-bind",
                    "127.0.0.1",
                    "--web-port",
                    "8081",
                    "--public-origin",
                    "https://wud.example.test",
                    "--allowed-hosts",
                    "wud.example.test,localhost",
                    "--trusted-proxies",
                    "127.0.0.1/32",
                    "--enable-web-mutations",
                    "--non-interactive",
                    "--backup-existing",
                    "--dry-run",
                    "--no-doctor",
                    "--no-color",
                ]
            )

        self.assertEqual(status, 23)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        args = run_init.call_args.args[0]
        self.assertEqual(args.profile, "webui")
        self.assertEqual(args.config_file, "/etc/wud/webui.env")
        self.assertEqual(args.compose_override, "/srv/wud/override.yml")
        self.assertEqual(args.stack_root, "/srv/docker")
        self.assertEqual(args.log_dir, "/srv/wud/logs")
        self.assertEqual(args.db_path, "/srv/wud/logs/wud.sqlite")
        self.assertEqual(args.uid, "1000")
        self.assertEqual(args.gid, "1000")
        self.assertEqual(args.web_exposure, "reverse-proxy")
        self.assertEqual(args.web_bind, "127.0.0.1")
        self.assertEqual(args.web_port, "8081")
        self.assertEqual(args.public_origin, "https://wud.example.test")
        self.assertEqual(args.allowed_hosts, "wud.example.test,localhost")
        self.assertEqual(args.trusted_proxies, "127.0.0.1/32")
        self.assertTrue(args.enable_web_mutations)
        self.assertTrue(args.non_interactive)
        self.assertTrue(args.backup_existing)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.no_doctor)
        self.assertTrue(args.no_color)

    def test_web_reset_admin_uses_configured_db_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wud-python-cli.") as tmpdir:
            root = Path(tmpdir)
            db_path = root / "state" / "wud.sqlite"
            now = utc_timestamp()
            with open_db(db_path) as conn:
                init_db(conn)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO web_users (
                            username,
                            password_hash,
                            role,
                            created_at,
                            password_updated_at
                        )
                        VALUES ('admin', ?, 'admin', ?, ?)
                        """,
                        (
                            PASSWORD_HASHER.hash("correct horse battery staple"),
                            now,
                            now,
                        ),
                    )
            env = {"HOME": tmpdir, "PATH": os.environ.get("PATH", "")}
            with mock.patch.dict(os.environ, env, clear=True):
                status, stdout, stderr = self._run_main(
                    [
                        "web",
                        "reset-admin",
                        "--user",
                        "admin",
                        "--db-path",
                        str(db_path),
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertRegex(
            stdout.strip(),
            r"^http://127\.0\.0\.1:7417/#/reset-admin\?claim=.+&user=admin$",
        )

    def test_web_reset_admin_missing_db_fails_without_recovery_link(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wud-python-cli.") as tmpdir:
            db_path = Path(tmpdir) / "missing" / "wud.sqlite"
            env = {"HOME": tmpdir, "PATH": os.environ.get("PATH", "")}
            with mock.patch.dict(os.environ, env, clear=True):
                status, stdout, stderr = self._run_main(
                    [
                        "web",
                        "reset-admin",
                        "--user",
                        "admin",
                        "--db-path",
                        str(db_path),
                    ]
                )

        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertIn("database file does not exist", stderr)
        self.assertNotIn("/#/reset-admin", stderr)

    def test_keyboard_interrupt_returns_130_without_traceback(self) -> None:
        with mock.patch(
            "wudup.cli._run_doctor",
            side_effect=KeyboardInterrupt,
        ):
            status, stdout, stderr = self._run_main(["doctor"])

        self.assertEqual(status, 130)
        self.assertEqual(stdout, "")
        self.assertIn("Interrupted.", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_removed_host_subcommands_are_rejected_by_parser(self) -> None:
        for command in ("updates", "truenas-status-export"):
            stderr = StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main([command])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("invalid choice", stderr.getvalue())

    def test_missing_subcommand_is_rejected_by_parser(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main([])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
