from __future__ import annotations

import argparse
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from ruamel.yaml import YAML

from wudup.init_config import (
    InitConfigError,
    InitPrompter,
    _add_wud_health_dependency,
    answers_from_namespace,
    generate_files,
    run_init,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class InitConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wud-init.")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_webui_loopback_env_defaults_to_read_only(self) -> None:
        config_file = self.root / "webui.env"
        answers = answers_from_namespace(
            self._args(
                profile="webui",
                config_file=str(config_file),
                stack_root=str(self.root / "docker"),
                no_doctor=True,
            ),
            environ=self._env(),
        )

        run_init(answers)

        content = config_file.read_text(encoding="utf-8")
        self.assertIn(f"HOST_DOCKER_BASE={self.root / 'docker'}", content)
        self.assertIn("WEBUI_HTTP_BIND=127.0.0.1", content)
        self.assertIn("WUD_WEB_PORT=7417", content)
        self.assertIn("WUD_WEB_MUTATIONS_ENABLED=false", content)
        self.assertIn("WUD_WEB_PUBLIC_ORIGIN=", content)
        self.assertIn("WUD_WEB_ALLOWED_HOSTS=", content)
        self.assertIn("WUD_API_BASE_URL=http://wud:3000", content)
        self.assertIn("WUD_API_STARTUP_WAIT_SECONDS=5", content)
        self.assertNotIn("WUD_PENDING_SOURCE", content)
        self.assertNotIn("WUDUP_LEGACY_SCRIPTS", content)
        self.assertNotIn("WUDUP_TRIGGER_TOKEN=", content)
        self.assertNotIn("WUDUP_TRIGGER_TOKEN_FILE=", content)

    def test_webui_lan_requires_public_origin_in_non_interactive_mode(self) -> None:
        with self.assertRaisesRegex(InitConfigError, "--public-origin"):
            answers_from_namespace(
                self._args(
                    profile="webui",
                    stack_root=str(self.root / "docker"),
                    web_exposure="lan",
                ),
                environ=self._env(),
            )

        with self.assertRaisesRegex(InitConfigError, "--public-origin"):
            answers_from_namespace(
                self._args(
                    profile="webui",
                    stack_root=str(self.root / "docker"),
                    web_exposure="lan",
                    allowed_hosts="   ",
                ),
                environ=self._env(),
            )

    def test_webui_lan_interactive_reprompts_for_public_origin(self) -> None:
        replies = iter(["", "http://wud.lan:7417"])
        stream = StringIO()

        answers = answers_from_namespace(
            self._args(
                profile="webui",
                config_file=str(self.root / "webui.env"),
                stack_root=str(self.root / "docker"),
                log_dir=str(self.root / "logs"),
                uid="1000",
                gid="1000",
                web_exposure="lan",
                non_interactive=False,
                no_doctor=True,
            ),
            environ=self._env(),
            prompter=InitPrompter(
                input_func=lambda _prompt: next(replies),
                stream=stream,
            ),
        )

        self.assertEqual(answers.public_origin, "http://wud.lan:7417")
        self.assertEqual(answers.allowed_hosts, "")
        self.assertIn("Browser-visible WebUI origin is required.", stream.getvalue())

    def test_webui_lan_env_can_enable_mutations_explicitly(self) -> None:
        answers = answers_from_namespace(
            self._args(
                profile="webui",
                stack_root=str(self.root / "docker"),
                web_exposure="lan",
                public_origin="http://wud.lan:7417",
                enable_web_mutations=True,
                no_doctor=True,
            ),
            environ=self._env(),
        )

        content = generate_files(answers)[0].content

        self.assertIn("WEBUI_HTTP_BIND=0.0.0.0", content)
        self.assertIn("WUD_WEB_PUBLIC_ORIGIN=http://wud.lan:7417", content)
        self.assertIn("WUD_WEB_ALLOWED_HOSTS=", content)
        self.assertIn("WUD_WEB_MUTATIONS_ENABLED=true", content)

    def test_webui_reverse_proxy_uses_public_origin_without_allowed_hosts(self) -> None:
        answers = answers_from_namespace(
            self._args(
                profile="webui",
                stack_root=str(self.root / "docker"),
                web_exposure="reverse-proxy",
                public_origin="https://wud.example.test",
                no_doctor=True,
            ),
            environ=self._env(),
        )

        content = generate_files(answers)[0].content

        self.assertIn("WEBUI_HTTP_BIND=127.0.0.1", content)
        self.assertIn("WUD_WEB_PUBLIC_ORIGIN=https://wud.example.test", content)
        self.assertIn("WUD_WEB_ALLOWED_HOSTS=", content)

    def test_webui_lan_preserves_explicit_allowed_host_aliases(self) -> None:
        answers = answers_from_namespace(
            self._args(
                profile="webui",
                stack_root=str(self.root / "docker"),
                web_exposure="lan",
                public_origin="http://wud.lan:7417",
                allowed_hosts="updates.lan,192.168.1.20",
                no_doctor=True,
            ),
            environ=self._env(),
        )

        content = generate_files(answers)[0].content

        self.assertIn("WUD_WEB_PUBLIC_ORIGIN=http://wud.lan:7417", content)
        self.assertIn("WUD_WEB_ALLOWED_HOSTS=updates.lan,192.168.1.20", content)

    def test_interactive_helper_preserves_prompt_order_and_resolved_values(self) -> None:
        questions: list[str] = []
        replies = iter(
            (
                "helper",
                str(self.root / "docker"),
                str(self.root / "helper.env"),
                "",
                str(self.root / "helper.override.yml"),
                str(self.root / "logs"),
                "1200",
                "1300",
            )
        )

        def answer(_prompt: str) -> str:
            return next(replies)

        class RecordingPrompter(InitPrompter):
            def choice(
                self,
                question: str,
                choices: Sequence[str],
                default: str,
            ) -> str:
                questions.append(question)
                return super().choice(question, choices, default)

            def text(self, question: str, default: str = "") -> str:
                questions.append(question)
                return super().text(question, default)

            def yes_no(self, question: str, default: bool = False) -> bool:
                questions.append(question)
                return super().yes_no(question, default)

        answers = answers_from_namespace(
            self._args(
                profile=None,
                config_file=None,
                compose_override=None,
                stack_root=None,
                log_dir=None,
                uid=None,
                gid=None,
                non_interactive=False,
                no_doctor=True,
            ),
            environ=self._env(),
            prompter=RecordingPrompter(input_func=answer),
        )

        self.assertEqual(
            questions,
            [
                "Deployment profile",
                "Compose stack root",
                "Config file",
                "Write a Compose override file",
                "Compose override file",
                "Log/state directory",
                "Shared file UID",
                "Shared file GID",
            ],
        )
        self.assertEqual(answers.profile, "helper")
        self.assertEqual(answers.stack_root, self.root / "docker")
        self.assertEqual(answers.config_file, self.root / "helper.env")
        self.assertEqual(answers.compose_override, self.root / "helper.override.yml")
        self.assertEqual(answers.log_dir, self.root / "logs")
        self.assertEqual(answers.db_path, self.root / "logs" / "wudup.sqlite")
        self.assertEqual((answers.uid, answers.gid), ("1200", "1300"))

    def test_uid_gid_can_come_from_environment_or_cli_override(self) -> None:
        env_answers = answers_from_namespace(
            self._args(
                profile="helper",
                stack_root=str(self.root / "docker"),
                no_doctor=True,
            ),
            environ={**self._env(), "OUT_UID": "1234", "OUT_GID": "5678"},
        )
        cli_answers = answers_from_namespace(
            self._args(
                profile="helper",
                stack_root=str(self.root / "docker"),
                uid="2222",
                gid="3333",
                no_doctor=True,
            ),
            environ={**self._env(), "OUT_UID": "1234", "OUT_GID": "5678"},
        )

        self.assertIn("OUT_UID=1234", generate_files(env_answers)[0].content)
        self.assertIn("OUT_GID=5678", generate_files(env_answers)[0].content)
        self.assertIn("OUT_UID=2222", generate_files(cli_answers)[0].content)
        self.assertIn("OUT_GID=3333", generate_files(cli_answers)[0].content)

    def test_existing_file_refuses_without_backup(self) -> None:
        config_file = self.root / "env"
        config_file.write_text("existing\n", encoding="utf-8")
        answers = answers_from_namespace(
            self._args(
                profile="helper",
                no_compose_override=True,
                config_file=str(config_file),
                stack_root=str(self.root / "docker"),
                no_doctor=True,
            ),
            environ=self._env(),
        )

        with self.assertRaisesRegex(InitConfigError, "Refusing to overwrite"):
            run_init(answers)

        self.assertEqual(config_file.read_text(encoding="utf-8"), "existing\n")

    def test_existing_file_can_be_backed_up(self) -> None:
        config_file = self.root / "env"
        config_file.write_text("existing\n", encoding="utf-8")
        answers = answers_from_namespace(
            self._args(
                profile="helper",
                no_compose_override=True,
                config_file=str(config_file),
                stack_root=str(self.root / "docker"),
                backup_existing=True,
                no_doctor=True,
            ),
            environ=self._env(),
        )

        result = run_init(answers)

        self.assertEqual(len(result.backups), 1)
        self.assertEqual(result.backups[0].read_text(encoding="utf-8"), "existing\n")
        self.assertIn("HOST_DOCKER_BASE=", config_file.read_text(encoding="utf-8"))

    def test_existing_later_file_refuses_before_writing_any_file(self) -> None:
        config_file = self.root / "helper.env"
        override_file = self.root / "override.yml"
        override_file.write_text("existing\n", encoding="utf-8")
        answers = answers_from_namespace(
            self._args(
                profile="helper",
                config_file=str(config_file),
                compose_override=str(override_file),
                stack_root=str(self.root / "docker"),
                no_doctor=True,
            ),
            environ=self._env(),
        )

        with self.assertRaisesRegex(InitConfigError, "Refusing to overwrite"):
            run_init(answers)

        self.assertFalse(config_file.exists())
        self.assertEqual(override_file.read_text(encoding="utf-8"), "existing\n")

    def test_existing_directory_refuses_even_with_backup(self) -> None:
        config_file = self.root / "env"
        config_file.mkdir()
        answers = answers_from_namespace(
            self._args(
                profile="helper",
                no_compose_override=True,
                config_file=str(config_file),
                stack_root=str(self.root / "docker"),
                backup_existing=True,
                no_doctor=True,
            ),
            environ=self._env(),
        )

        with self.assertRaisesRegex(InitConfigError, "non-regular"):
            run_init(answers)

        self.assertTrue(config_file.is_dir())
        self.assertEqual(list(config_file.iterdir()), [])

    def test_dry_run_writes_nothing(self) -> None:
        config_file = self.root / "env"
        answers = answers_from_namespace(
            self._args(
                profile="helper",
                no_compose_override=True,
                config_file=str(config_file),
                stack_root=str(self.root / "docker"),
                dry_run=True,
            ),
            environ=self._env(),
        )

        result = run_init(answers)

        self.assertEqual(result.backups, ())
        self.assertFalse(config_file.exists())
        self.assertIsNone(result.doctor_status)

    def test_removed_host_profile_is_rejected(self) -> None:
        args = self._args(profile="host")
        env = self._env()

        with self.assertRaisesRegex(InitConfigError, "profile must be one of"):
            answers_from_namespace(args, environ=env)

    def test_non_interactive_requires_profile_and_stack_root(self) -> None:
        with self.assertRaisesRegex(InitConfigError, "--profile"):
            answers_from_namespace(self._args(profile=None), environ=self._env())

        with self.assertRaisesRegex(InitConfigError, "--stack-root"):
            answers_from_namespace(
                self._args(profile="helper", stack_root=None),
                environ=self._env(),
            )

    def test_helper_compose_override_yaml_contains_expected_service_fields(self) -> None:
        override_file = self.root / "override.yml"
        answers = answers_from_namespace(
            self._args(
                profile="helper",
                compose_override=str(override_file),
                stack_root=str(self.root / "docker"),
                no_doctor=True,
            ),
            environ=self._env(),
        )

        run_init(answers)

        parsed = YAML(typ="safe").load(override_file.read_text(encoding="utf-8"))
        service = parsed["services"]["wudup"]
        self.assertEqual(service["environment"]["WUD_OUT_FILE"], "/out/images.todo")
        self.assertEqual(
            service["environment"]["WUD_DB_PATH"],
            "/logs/wudup.sqlite",
        )
        self.assertIn("${WEBUI_LOG_DIR:-./logs}:/logs", service["volumes"])
        self.assertFalse(any("managed-wud" in volume for volume in service["volumes"]))

    def test_hardened_compose_override_uses_image_defaults_for_log_and_db(self) -> None:
        override_file = self.root / "override.yml"
        answers = answers_from_namespace(
            self._args(
                profile="hardened",
                compose_override=str(override_file),
                stack_root=str(self.root / "docker"),
                no_doctor=True,
            ),
            environ=self._env(),
        )

        run_init(answers)

        parsed = YAML(typ="safe").load(override_file.read_text(encoding="utf-8"))
        environment = parsed["services"]["wudup"]["environment"]
        self.assertEqual(
            environment["WUD_OUT_FILE"],
            "${WUD_OUT_FILE:-/out/images.todo}",
        )
        self.assertNotIn("WUD_LOG_DIR", environment)
        self.assertNotIn("WUD_DB_PATH", environment)
        self.assertNotIn("WUDUP_USE_SUDO", environment)
        self.assertEqual(
            environment["WUD_API_BASE_URL"],
            "${WUD_API_BASE_URL:-http://wud:3000}",
        )
        self.assertEqual(
            environment["WUD_API_STARTUP_WAIT_SECONDS"],
            "${WUD_API_STARTUP_WAIT_SECONDS:-5}",
        )
        self.assertNotIn("WUD_PENDING_SOURCE", environment)
        self.assertNotIn("WUDUP_LEGACY_SCRIPTS", environment)
        self.assertNotIn("WUDUP_TRIGGER_TOKEN", environment)
        self.assertNotIn("WUDUP_TRIGGER_TOKEN_FILE", environment)
        self.assertEqual(
            parsed["services"]["wudup"]["depends_on"],
            {"wud": {"condition": "service_healthy"}},
        )
        self.assertIn(
            "${WEBUI_LOG_DIR:-./logs}:/logs",
            parsed["services"]["wudup"]["volumes"],
        )

    def test_wud_health_dependency_preserves_existing_depends_on(self) -> None:
        service: dict[str, object] = {
            "depends_on": {
                "socket-proxy-wudup": {"condition": "service_started"},
            }
        }

        _add_wud_health_dependency(service)

        self.assertEqual(
            service["depends_on"],
            {
                "socket-proxy-wudup": {"condition": "service_started"},
                "wud": {"condition": "service_healthy"},
            },
        )

    def test_wud_health_dependency_converts_list_depends_on(self) -> None:
        service: dict[str, object] = {
            "depends_on": ["socket-proxy", "database"],
        }

        _add_wud_health_dependency(service)

        self.assertEqual(
            service["depends_on"],
            {
                "socket-proxy": {"condition": "service_started"},
                "database": {"condition": "service_started"},
                "wud": {"condition": "service_healthy"},
            },
        )

    def test_wud_health_dependency_rejects_invalid_depends_on(self) -> None:
        service: dict[str, object] = {
            "depends_on": "database",
        }

        with self.assertRaisesRegex(InitConfigError, "mapping or a list"):
            _add_wud_health_dependency(service)

    def test_webui_compose_override_yaml_inherits_image_healthcheck(self) -> None:
        override_file = self.root / "override.yml"
        answers = answers_from_namespace(
            self._args(
                profile="webui",
                compose_override=str(override_file),
                stack_root=str(self.root / "docker"),
                no_doctor=True,
            ),
            environ=self._env(),
        )

        run_init(answers)

        parsed = YAML(typ="safe").load(override_file.read_text(encoding="utf-8"))
        service = parsed["services"]["wudup"]
        self.assertNotIn("command", service)
        self.assertNotIn("WUD_WEB_HOST", service["environment"])
        self.assertEqual(
            service["environment"]["WUD_API_BASE_URL"],
            "${WUD_API_BASE_URL:-http://wud:3000}",
        )
        self.assertEqual(
            service["environment"]["WUD_API_STARTUP_WAIT_SECONDS"],
            "${WUD_API_STARTUP_WAIT_SECONDS:-5}",
        )
        self.assertNotIn("WUD_PENDING_SOURCE", service["environment"])
        self.assertNotIn("WUDUP_LEGACY_SCRIPTS", service["environment"])
        self.assertNotIn("WUDUP_TRIGGER_TOKEN", service["environment"])
        self.assertNotIn("WUDUP_TRIGGER_TOKEN_FILE", service["environment"])
        self.assertEqual(
            service["depends_on"],
            {"wud": {"condition": "service_healthy"}},
        )
        self.assertEqual(
            service["ports"],
            [
                "${WEBUI_HTTP_BIND:-127.0.0.1}:${WUD_WEB_PORT:-7417}:${WUD_WEB_PORT:-7417}"
            ],
        )
        self.assertNotIn("healthcheck", service)

    def test_webui_examples_use_api_source_on_internal_app_network(self) -> None:
        yaml = YAML(typ="safe")
        for name in ("docker-compose.webui.yml", "docker-compose.hardened.yml"):
            with self.subTest(name=name):
                compose = yaml.load(
                    (REPO_ROOT / "docs" / "examples" / name).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertTrue(compose["networks"]["wudup-app"]["internal"])
                for service_name in ("wud", "wudup"):
                    self.assertIn(
                        "wudup-app",
                        compose["services"][service_name]["networks"],
                    )
                self.assertEqual(
                    compose["services"]["wudup"]["environment"][
                        "WUD_API_BASE_URL"
                    ],
                    "${WUD_API_BASE_URL:-http://wud:3000}",
                )
                self.assertNotIn(
                    "WUD_PENDING_SOURCE",
                    compose["services"]["wudup"]["environment"],
                )

    def test_container_doctor_runs_only_after_interactive_confirmation(self) -> None:
        args = self._args(
            profile="helper",
            config_file=str(self.root / "helper.env"),
            no_compose_override=True,
            stack_root=str(self.root / "docker"),
            log_dir=str(self.root / "logs"),
            uid="1000",
            gid="1000",
            non_interactive=False,
        )
        answers = answers_from_namespace(args, environ=self._env())
        completed = mock.Mock(returncode=0)

        with (
            mock.patch("builtins.input", return_value="yes"),
            mock.patch("wudup.init_config.subprocess.run", return_value=completed)
            as run,
            redirect_stdout(StringIO()),
        ):
            result = run_init(answers)

        self.assertEqual(result.doctor_status, 0)
        self.assertEqual(
            run.call_args.args[0],
            [
                "docker",
                "compose",
                "--env-file",
                str(self.root / "helper.env"),
                "-f",
                "docs/examples/docker-compose.example.yml",
                "run",
                "--rm",
                "wudup",
                "doctor",
            ],
        )

    def test_container_doctor_guidance_printed_when_not_run(self) -> None:
        for non_interactive, answer in ((True, None), (False, "no")):
            with self.subTest(non_interactive=non_interactive):
                config_file = self.root / f"helper-{non_interactive}.env"
                answers = answers_from_namespace(
                    self._args(
                        profile="helper",
                        config_file=str(config_file),
                        no_compose_override=True,
                        stack_root=str(self.root / "docker"),
                        log_dir=str(self.root / "logs"),
                        uid="1000",
                        gid="1000",
                        non_interactive=non_interactive,
                    ),
                    environ=self._env(),
                )
                stdout = StringIO()
                with (
                    mock.patch("builtins.input", return_value=answer or ""),
                    mock.patch("wudup.init_config.subprocess.run") as run,
                    redirect_stdout(stdout),
                ):
                    result = run_init(answers)

                self.assertIsNone(result.doctor_status)
                run.assert_not_called()
                self.assertIn(
                    "Container doctor was not run automatically. Run:",
                    stdout.getvalue(),
                )
                self.assertIn(str(config_file), stdout.getvalue())

    def _env(self) -> dict[str, str]:
        return {"HOME": str(self.root), "PATH": ""}

    def _args(self, **overrides: object) -> argparse.Namespace:
        values = {
            "profile": "helper",
            "config_file": None,
            "compose_override": None,
            "no_compose_override": False,
            "stack_root": str(self.root / "docker"),
            "log_dir": None,
            "db_path": None,
            "uid": None,
            "gid": None,
            "web_exposure": None,
            "web_bind": None,
            "web_port": None,
            "public_origin": None,
            "allowed_hosts": None,
            "trusted_proxies": None,
            "enable_web_mutations": False,
            "non_interactive": True,
            "backup_existing": False,
            "dry_run": False,
            "no_doctor": False,
            "no_color": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)


if __name__ == "__main__":
    unittest.main()
