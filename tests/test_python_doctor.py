from __future__ import annotations

import argparse
import concurrent.futures
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from wudup.command import CommandResult
from wudup.doctor import (
    Doctor,
    DoctorOptions,
    _check_category,
    _write_probe,
    doctor_result_from_namespace,
    run_doctor_from_namespace,
)


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wud-doctor.")
        self.root = Path(self.tmp.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.docker_base = self.root / "docker"
        self.stack_dir = self.docker_base / "app"
        self.stack_dir.mkdir(parents=True)
        self.out_dir = self.root / "out"
        self.out_dir.mkdir()
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()
        self._write_docker()
        self._write_compose()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_doctor_passes_with_container_prerequisites(self) -> None:
        status, stdout = self._run_doctor()

        self.assertEqual(status, 0, stdout)
        self.assertIn("[PASS] docker cli: Docker version 28.0.0", stdout)
        self.assertIn("[PASS] compose discovery: 1 stack(s) rendered", stdout)
        self.assertIn("Result: 0 failure(s)", stdout)
        for removed in ("TrueNAS", "WUD script sync", "packaged WUD scripts", "sudo", "updater executable"):
            self.assertNotIn(removed, stdout)

    def test_doctor_explains_missing_wud_file_without_failing(self) -> None:
        wud_file = self.out_dir / "images.todo"
        self.assertFalse(wud_file.exists())

        status, stdout = self._run_doctor()

        self.assertEqual(status, 0, stdout)
        self.assertIn(
            f"[PASS] WUD_OUT_FILE: {wud_file} does not exist yet; "
            "docker-update-from-wud needs it before it can run",
            stdout,
        )

    def test_doctor_fails_when_no_compose_stacks_are_found(self) -> None:
        for path in self.stack_dir.iterdir():
            path.unlink()

        status, stdout = self._run_doctor()

        self.assertEqual(status, 1, stdout)
        self.assertIn("[FAIL] compose discovery: no compose stacks found", stdout)
        self.assertNotIn("Ignored paths:", stdout)

    def test_doctor_reports_configured_compose_ignore_paths(self) -> None:
        status, stdout = self._run_doctor({"WUD_COMPOSE_IGNORE_PATHS": "app"})

        self.assertEqual(status, 1, stdout)
        self.assertIn("Ignored paths: app", stdout)
        self.assertNotIn("./old", stdout)

    def test_doctor_ignores_removed_host_and_script_settings(self) -> None:
        status, stdout = self._run_doctor(
            {
                "WUD_SYNC_SCRIPTS": "treu",
                "WUD_SCRIPTS_DIR": str(self.root / "missing"),
                "WUDUP_USE_SUDO": "treu",
                "TRUENAS_STATUS_CHECK": "treu",
            }
        )

        self.assertEqual(status, 0, stdout)
        self.assertNotIn("[FAIL] configuration", stdout)

    def test_doctor_reports_invalid_compose_ignore_paths_as_configuration_failure(self) -> None:
        status, stdout = self._run_doctor({"WUD_COMPOSE_IGNORE_PATHS": "/absolute"})

        self.assertEqual(status, 1, stdout)
        self.assertIn(
            "[FAIL] configuration: WUD_COMPOSE_IGNORE_PATHS entries must be relative paths",
            stdout,
        )
        self.assertIn("Result: 1 failure(s), 0 warning(s)", stdout)

    def test_check_category_maps_remaining_check_names(self) -> None:
        cases = {
            "python rich": "runtime",
            "docker cli": "docker",
            "compose discovery": "compose",
            "bind mount path safety": "compose",
            "WUD_OUT_FILE": "paths",
            "DOCKER_BASE": "paths",
            "configuration": "configuration",
            "WebUI database": "general",
        }
        for name, category in cases.items():
            with self.subTest(name=name):
                self.assertEqual(_check_category(name), category)

    def test_doctor_result_includes_structured_checks(self) -> None:
        for path in self.stack_dir.iterdir():
            path.unlink()

        result = self._run_doctor_result()

        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.failures, 1)
        compose = next(
            check for check in result.checks if check.name == "compose discovery"
        )
        self.assertEqual(compose.status, "FAIL")
        self.assertEqual(compose.code, "compose-discovery")
        self.assertEqual(compose.category, "compose")
        self.assertTrue(compose.suggestions)
        self.assertIn("docker compose", compose.suggestions[0].snippet)

    def test_doctor_fails_when_compose_config_cannot_render(self) -> None:
        status, stdout = self._run_doctor(
            {"FAKE_DOCKER_CONFIG_FAIL": "1"},
        )

        self.assertEqual(status, 1, stdout)
        self.assertIn("[FAIL] compose config", stdout)
        self.assertIn("config failed", stdout)

    def test_doctor_warns_for_helper_only_bind_mount_sources(self) -> None:
        status, stdout = self._run_doctor(
            {"FAKE_DOCKER_BIND_SOURCE": "/host/app/config"},
        )

        self.assertEqual(status, 0, stdout)
        self.assertIn("[WARN] bind mount path safety", stdout)
        self.assertIn("app: /host/app/config", stdout)

    def test_readiness_result_passes_with_accessible_docker_and_wud_file(self) -> None:
        # Use a tcp DOCKER_HOST so no Unix socket check is needed.
        options = self._make_doctor_options(docker_host="tcp://docker:2375")
        env = self._doctor_env()
        ok_result = CommandResult(
            args=("docker", "version"),
            cwd=None,
            returncode=0,
            stdout="Docker Engine 28.0.0",
        )
        runner_mock = mock.Mock()
        runner_mock.capture.return_value = ok_result

        doctor = Doctor(options, environ=env, runner=runner_mock)
        result = doctor.run_readiness_result()

        self.assertEqual(result.failures, 0)
        self.assertTrue(result.ok)

    def test_readiness_result_fails_when_docker_unavailable(self) -> None:
        options = self._make_doctor_options(docker_host="tcp://docker:2375")
        env = self._doctor_env()
        fail_result = CommandResult(
            args=("docker", "version"),
            cwd=None,
            returncode=1,
            stderr="connection refused",
        )
        runner_mock = mock.Mock()
        runner_mock.capture.return_value = fail_result

        doctor = Doctor(options, environ=env, runner=runner_mock)
        result = doctor.run_readiness_result()

        self.assertFalse(result.ok)
        self.assertGreater(result.failures, 0)

    def test_permission_probe_names_are_safe_for_concurrent_doctor_runs(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            issues = list(executor.map(lambda _: _write_probe(self.log_dir), range(64)))

        self.assertEqual([issue for issue in issues if issue], [])

    def _run_doctor(
        self,
        env_overrides: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        env = self._doctor_env(env_overrides)

        stdout = StringIO()
        args = self._doctor_args()
        with redirect_stdout(stdout):
            status = run_doctor_from_namespace(
                args,
                repo_root=self.root,
                environ=env,
            )
        return status, stdout.getvalue()

    def _run_doctor_result(
        self,
        env_overrides: dict[str, str] | None = None,
    ):
        return doctor_result_from_namespace(
            self._doctor_args(),
            repo_root=self.root,
            environ=self._doctor_env(env_overrides),
        )

    def _doctor_env(
        self,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        env = {
            "PATH": f"{self.fake_bin}:{os.environ.get('PATH', '')}",
            "DOCKER_HOST": "tcp://docker:2375",
            "DOCKER_BASE": str(self.docker_base),
            "WUD_OUT_FILE": str(self.out_dir / "images.todo"),
            "WUD_LOG_DIR": str(self.log_dir),
        }
        if env_overrides is not None:
            env.update(env_overrides)
        return env

    def _doctor_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            base=None,
            file=None,
            log_dir=None,
            no_color=True,
        )

    def _make_doctor_options(self, **overrides: object) -> DoctorOptions:
        defaults: dict[str, object] = {
            "docker_base": self.docker_base,
            "wud_file": self.out_dir / "images.todo",
            "log_dir": self.log_dir,
        }
        defaults.update(overrides)
        return DoctorOptions(**defaults)  # type: ignore[arg-type]

    def _write_docker(self) -> None:
        docker = self.fake_bin / "docker"
        docker.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  --version)
    printf 'Docker version 28.0.0\\n'
    exit 0
    ;;
  version)
    printf 'Server: Docker Engine 28.0.0\\n'
    exit 0
    ;;
  info)
    printf 'Docker Root Dir: /var/lib/docker\\n'
    exit 0
    ;;
  ps)
    printf 'CONTAINER ID   IMAGE\\n'
    exit 0
    ;;
  compose)
    if [[ "${2:-}" == "version" ]]; then
      printf 'Docker Compose version v2.30.0\\n'
      exit 0
    fi
    if [[ "${FAKE_DOCKER_CONFIG_FAIL:-}" == "1" ]]; then
      printf 'config failed\\n' >&2
      exit 22
    fi
    for arg in "$@"; do
      if [[ "$arg" == "json" ]]; then
        if [[ -n "${FAKE_DOCKER_BIND_SOURCE:-}" ]]; then
          printf '{"services":{"app":{"image":"repo/app:latest","volumes":[{"type":"bind","source":"%s","target":"/config"}]}}}\\n' "$FAKE_DOCKER_BIND_SOURCE"
        else
          printf '{"services":{"app":{"image":"repo/app:latest"}}}\\n'
        fi
        exit 0
      fi
    done
    printf 'name: app\\n'
    exit 0
    ;;
esac
printf 'unexpected docker args: %s\\n' "$*" >&2
exit 2
""",
            encoding="utf-8",
        )
        docker.chmod(0o755)

    def _write_compose(self) -> None:
        (self.stack_dir / "compose.yml").write_text(
            "services:\n  app:\n    image: repo/app:latest\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
