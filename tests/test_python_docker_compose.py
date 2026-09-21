from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from wudup.command import CommandError, CommandRunner, display_command
from wudup.compose import (
    ComposeBindMount,
    ComposeCli,
    ComposeDiscoveryError,
    ComposeRuntimePortIssue,
    ServiceImage,
    _project_directory_for_stack,
    _service_bind_mounts_from_config_json,
    _service_runtime_port_issues_from_config_json,
)
from wudup.docker_cli import ContainerImage, DockerCli
from wudup.platforms import ImagePlatform


class CommandHelperTests(unittest.TestCase):
    def test_display_command_shell_quotes_arguments(self) -> None:
        self.assertEqual(
            display_command(["docker", "compose", "-f", "compose file.yml", "pull"]),
            "docker compose -f 'compose file.yml' pull",
        )

    @unittest.skipUnless(os.name == "posix", "PTY support is POSIX-only")
    def test_run_in_pty_exposes_stdout_and_stderr_as_ttys(self) -> None:
        output = StringIO()
        runner = CommandRunner()

        with (
            mock.patch("sys.stdout", output),
            mock.patch.dict(os.environ, {"COLUMNS": "100", "LINES": "40"}),
        ):
            result = runner.run_in_pty(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os, sys; "
                        "size = os.get_terminal_size(1); "
                        "print(f'out={os.isatty(1)} err={os.isatty(2)} "
                        "size={size.columns}x{size.lines}'); "
                        "print('stderr line', file=sys.stderr); "
                        "raise SystemExit(3)"
                    ),
                ],
                check=False,
            )

        self.assertEqual(result.returncode, 3)
        self.assertIn("out=True err=True", output.getvalue())
        self.assertIn("size=100x40", output.getvalue())
        self.assertIn("stderr line", output.getvalue())
        self.assertIn("out=True err=True", result.stdout)
        self.assertIn("size=100x40", result.stdout)
        self.assertIn("stderr line", result.stdout)
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(os.name == "posix", "PTY support is POSIX-only")
    def test_run_in_pty_falls_back_to_streaming_when_openpty_fails(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        runner = CommandRunner()

        with (
            mock.patch("wudup.command.pty.openpty", side_effect=OSError("no pty")),
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            result = runner.run_in_pty(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        "print('streamed stdout'); "
                        "print('streamed stderr', file=sys.stderr); "
                        "raise SystemExit(3)"
                    ),
                ],
                check=False,
            )

        self.assertEqual(result.returncode, 3)
        self.assertEqual(stdout.getvalue(), "streamed stdout\n")
        self.assertIn("streamed stderr\n", stderr.getvalue())
        self.assertEqual(result.stdout, "streamed stdout\n")
        self.assertEqual(result.stderr, "streamed stderr\n")


class FakeDockerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wud-python-docker.")
        self.root = Path(self.tmp.name)
        self.repo_root = Path(__file__).resolve().parents[1]
        self.base = self.root / "base"
        self.fake_root = self.root / "fake"
        for path in (
            self.base,
            self.fake_root / "images",
            self.fake_root / "stacks",
            self.fake_root / "containers",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.fake_root / "containers.tsv").write_text("", encoding="utf-8")
        (self.fake_root / "calls.log").write_text("", encoding="utf-8")

        self.env = os.environ.copy()
        self.env["FAKE_DOCKER_ROOT"] = str(self.fake_root)
        self.env["PATH"] = f"{self.repo_root / 'tests' / 'fakes'}:{self.env['PATH']}"
        self.runner = CommandRunner(env=self.env)
        self.docker = DockerCli(runner=self.runner)
        self.compose = ComposeCli(runner=self.runner)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call_commands(self) -> list[str]:
        lines = (self.fake_root / "calls.log").read_text(encoding="utf-8").splitlines()
        return [line.partition("\t")[2] for line in lines if line]

    def clear_calls(self) -> None:
        (self.fake_root / "calls.log").write_text("", encoding="utf-8")

    def make_stack(
        self,
        stack_id: str,
        services: list[tuple[str, str, str | None]],
        *,
        parent: Path | None = None,
    ) -> Path:
        directory = (parent or self.base) / stack_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".fake-docker-id").write_text(f"{stack_id}\n", encoding="utf-8")

        lines = ["services:\n"]
        cids: list[str] = []
        stack_state = self.fake_root / "stacks" / stack_id
        stack_state.mkdir(parents=True, exist_ok=True)
        for service, image, cid in services:
            lines.extend([f"  {service}:\n", f"    image: {image}\n"])
            if cid is not None:
                cids.append(cid)
                (stack_state / f"cids-{service}.txt").write_text(
                    f"{cid}\n",
                    encoding="utf-8",
                )
                (self.fake_root / "containers" / f"{cid}.summary").write_text(
                    f"/{cid}|running|healthy|0|0\n",
                    encoding="utf-8",
                )
        (directory / "docker-compose.yml").write_text("".join(lines), encoding="utf-8")
        (stack_state / "cids.txt").write_text(
            "".join(f"{cid}\n" for cid in cids),
            encoding="utf-8",
        )
        return directory

    def set_image_state(self, image: str, image_id: str, digest: str = "") -> None:
        safe = _safe_name(image)
        (self.fake_root / "images" / f"{safe}.id").write_text(
            f"{image_id}\n",
            encoding="utf-8",
        )
        digest_text = f"{image}@{digest}\n" if digest else ""
        (self.fake_root / "images" / f"{safe}.digests").write_text(
            digest_text,
            encoding="utf-8",
        )

    def set_image_after_pull(self, image: str, image_id: str, digest: str = "") -> None:
        safe = _safe_name(image)
        (self.fake_root / "images" / f"{safe}.after_id").write_text(
            f"{image_id}\n",
            encoding="utf-8",
        )
        digest_text = f"{image}@{digest}\n" if digest else ""
        (self.fake_root / "images" / f"{safe}.after_digests").write_text(
            digest_text,
            encoding="utf-8",
        )


class DockerCliTests(FakeDockerCase):
    def test_try_methods_treat_missing_docker_executable_as_unavailable(self) -> None:
        docker = DockerCli(
            runner=self.runner,
            executable=str(self.root / "missing-docker"),
        )

        self.assertEqual(docker.try_container_images(), [])
        self.assertEqual(docker.image_id("repo/web:latest"), "")
        self.assertEqual(docker.image_repo_digests("repo/web:latest"), [])
        self.assertEqual(docker.try_inspect("cid-web", "{{.Name}}"), [])
        self.assertEqual(docker.try_container_image_id("cid-web"), "")

    def test_ps_image_inspect_and_inspect_use_shell_formats(self) -> None:
        (self.fake_root / "containers.tsv").write_text(
            "web\trepo/web:latest\n",
            encoding="utf-8",
        )
        self.set_image_state("repo/web:latest", "sha256:image-id", "sha256:digest")
        (self.fake_root / "containers" / "cid-web.summary").write_text(
            "/web|running|healthy|0|0\n",
            encoding="utf-8",
        )
        (self.fake_root / "containers" / "cid-web.image-id").write_text(
            "sha256:container-image-id\n",
            encoding="utf-8",
        )

        self.assertEqual(
            self.docker.container_images(),
            [ContainerImage(name="web", image="repo/web:latest")],
        )
        self.assertEqual(self.docker.image_id("repo/web:latest"), "sha256:image-id")
        self.assertEqual(
            self.docker.image_digest("repo/web:latest"),
            "repo/web:latest@sha256:digest",
        )
        self.assertTrue(
            self.docker.image_has_digest("repo/web:latest", "sha256:digest")
        )
        self.assertEqual(
            self.docker.try_inspect("cid-web", "{{.Name}}|{{.State.Status}}"),
            ["/web|running|healthy|0|0"],
        )
        self.assertEqual(
            self.docker.try_container_image_id("cid-web"),
            "sha256:container-image-id",
        )

        self.assertIn("ps --format {{.Names}}\t{{.Image}}", self.call_commands())
        self.assertIn("image inspect repo/web:latest", self.call_commands())
        self.assertIn("inspect cid-web", self.call_commands())

    def test_ps_format_can_include_stopped_containers(self) -> None:
        (self.fake_root / "containers.tsv").write_text(
            "web\trepo/web:latest\n",
            encoding="utf-8",
        )

        self.assertEqual(
            self.docker.ps_format(all_containers=True),
            ["web\trepo/web:latest"],
        )
        self.assertIn(
            "ps --all --format {{.Names}}\t{{.Image}}",
            self.call_commands(),
        )


class ComposeCliTests(FakeDockerCase):
    def test_discover_stacks_skips_missing_docker_executable(self) -> None:
        self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        compose = ComposeCli(
            runner=self.runner,
            docker_executable=str(self.root / "missing-docker"),
        )

        with self.assertRaisesRegex(ComposeDiscoveryError, "No compose stacks found"):
            compose.discover_stacks(self.base)

    def test_discover_stacks_reads_images_services_and_service_image_map(self) -> None:
        stack = self.make_stack(
            "stack",
            [
                ("app", "repo/app:latest", "cid-app"),
                ("db", "repo/db:latest", "cid-db"),
            ],
        )
        archived = self.make_stack(
            "ignored",
            [("app", "repo/ignored:latest", "cid-ignored")],
            parent=self.base / "old",
        )
        deep = self.base / "a" / "b" / "c"
        deep.mkdir(parents=True, exist_ok=True)
        (deep / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: repo/deep:latest\n",
            encoding="utf-8",
        )
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: repo/symlinked:latest\n",
            encoding="utf-8",
        )
        symlinked = self.base / "symlinked"
        symlinked.mkdir()
        (symlinked / "docker-compose.yml").symlink_to(
            outside / "docker-compose.yml"
        )

        stacks = self.compose.discover_stacks(self.base, ignore_paths=("old",))

        by_directory = {item.directory: item for item in stacks}
        self.assertEqual(len(stacks), 1)
        self.assertIn(stack, by_directory)
        self.assertNotIn(archived, by_directory)
        self.assertEqual(by_directory[stack].file, "docker-compose.yml")
        self.assertEqual(by_directory[stack].name, "stack")
        self.assertEqual(by_directory[stack].project_name, "stack")
        self.assertEqual(
            by_directory[stack].images,
            ("repo/app:latest", "repo/db:latest"),
        )
        self.assertEqual(
            by_directory[stack].service_images,
            (
                ServiceImage(service="app", image="repo/app:latest"),
                ServiceImage(service="db", image="repo/db:latest"),
            ),
        )
        self.assertEqual(
            self.call_commands(),
            ["compose -f docker-compose.yml config --format json"],
        )

    def test_discover_stacks_falls_back_to_images_when_json_is_unavailable(
        self,
    ) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])

        with mock.patch.object(
            self.compose,
            "config_json",
            side_effect=ValueError("JSON output is unavailable"),
        ):
            stacks = self.compose.discover_stacks(self.base)

        self.assertEqual(len(stacks), 1)
        self.assertEqual(stacks[0].directory, stack)
        self.assertEqual(stacks[0].images, ("repo/app:latest",))
        self.assertEqual(stacks[0].service_images, ())
        self.assertEqual(stacks[0].service_names, ("app",))
        self.assertFalse(stacks[0].inspection_complete)
        self.assertEqual(
            self.call_commands(),
            [
                "compose -f docker-compose.yml config --images",
                "compose -f docker-compose.yml config --services",
            ],
        )

    def test_try_config_project_name_returns_empty_for_invalid_json(self) -> None:
        with mock.patch.object(
            self.compose,
            "config_json",
            return_value=mock.Mock(stdout="{"),
        ):
            project_name = self.compose.try_config_project_name(
                self.base,
                "docker-compose.yml",
            )

        self.assertEqual(project_name, "")

    def test_discover_stacks_skips_configured_single_component_ignore(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        self.make_stack(
            "ignored",
            [("app", "repo/ignored:latest", "cid-ignored")],
            parent=self.base / "old",
        )

        stacks = self.compose.discover_stacks(self.base, ignore_paths=("old",))

        self.assertEqual([item.directory for item in stacks], [stack])

    def test_discover_stacks_includes_archives_when_ignore_is_empty(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        archived = self.make_stack(
            "archived",
            [("app", "repo/archived:latest", "cid-archived")],
            parent=self.base / "old",
        )

        stacks = self.compose.discover_stacks(self.base, ignore_paths=())

        self.assertEqual({item.directory for item in stacks}, {stack, archived})

    def test_discover_stacks_skips_configured_non_default_ignore(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        self.make_stack(
            "ignored",
            [("app", "repo/ignored:latest", "cid-ignored")],
            parent=self.base / "archive",
        )

        stacks = self.compose.discover_stacks(self.base, ignore_paths=("archive",))

        self.assertEqual([item.directory for item in stacks], [stack])

    def test_discover_stacks_matches_multi_component_ignore_from_base(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        keep = self.make_stack(
            "old-stacks",
            [("app", "repo/keep:latest", "cid-keep")],
            parent=self.base / "other",
        )
        self.make_stack(
            "old-stacks",
            [("app", "repo/ignored:latest", "cid-ignored")],
            parent=self.base / "archive",
        )

        stacks = self.compose.discover_stacks(
            self.base,
            ignore_paths=("archive/old-stacks",),
        )

        self.assertEqual({item.directory for item in stacks}, {stack, keep})

    def test_discover_stacks_maps_project_base_to_stack_project_directory(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        project_base = self.root / "host-docker"
        mirrored_stack = project_base / "stack"
        mirrored_stack.mkdir(parents=True)
        (mirrored_stack / "docker-compose.yml").write_text(
            (stack / "docker-compose.yml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        stacks = self.compose.discover_stacks(self.base, project_base=project_base)

        self.assertEqual(len(stacks), 1)
        self.assertEqual(stacks[0].directory, stack)
        self.assertEqual(stacks[0].project_directory, project_base / "stack")
        self.assertIn(
            f"compose --project-directory {project_base / 'stack'} "
            "-f docker-compose.yml config --format json",
            self.call_commands(),
        )

    def test_config_json_uses_project_directory_name_as_fallback(self) -> None:
        stack = self.make_stack("source-stack", [("app", "repo/app:latest", "cid-app")])
        project_directory = self.root / "runtime-stack"
        project_directory.mkdir()
        self.env.pop("COMPOSE_PROJECT_NAME", None)

        result = self.compose.config_json(
            stack,
            "docker-compose.yml",
            project_directory=project_directory,
        )

        self.assertEqual(json.loads(result.stdout)["name"], "runtime-stack")

    def test_discover_stacks_rejects_unmounted_project_base(self) -> None:
        self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        project_base = self.root / "host-docker"

        with self.assertRaisesRegex(ComposeDiscoveryError, "not a readable compose file"):
            self.compose.discover_stacks(self.base, project_base=project_base)

    def test_project_directory_is_passed_to_stack_commands(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        project_directory = self.root / "host-docker" / "stack"

        self.compose.pull(
            stack,
            "docker-compose.yml",
            ["app"],
            project_directory=project_directory,
        )
        self.compose.ps_quiet(
            stack,
            "docker-compose.yml",
            ["app"],
            project_directory=project_directory,
        )

        self.assertIn(
            f"compose --project-directory {project_directory} "
            "-f docker-compose.yml pull app",
            self.call_commands(),
        )
        self.assertIn(
            f"compose --project-directory {project_directory} "
            "-f docker-compose.yml ps -q app",
            self.call_commands(),
        )

    def test_project_directory_mapping_rejects_stack_outside_base(self) -> None:
        with self.assertRaisesRegex(ComposeDiscoveryError, "not under DOCKER_BASE"):
            _project_directory_for_stack(
                Path("/host/other/app"),
                Path("/host/docker"),
                Path("/srv/docker"),
            )

    def test_service_image_pairs_reads_network_mode(self) -> None:
        stack = self.base / "media"
        stack.mkdir()
        (stack / ".fake-docker-id").write_text("media\n", encoding="utf-8")
        (stack / "docker-compose.yml").write_text(
            "\n".join(
                [
                    "services:",
                    "  gluetun:",
                    "    image: qmcgaw/gluetun:latest",
                    "  qbittorrent:",
                    "    image: ghcr.io/linuxserver/qbittorrent:5.1.4",
                    "    network_mode: service:gluetun",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            self.compose.service_image_pairs(stack, "docker-compose.yml"),
            (
                ServiceImage(service="gluetun", image="qmcgaw/gluetun:latest"),
                ServiceImage(
                    service="qbittorrent",
                    image="ghcr.io/linuxserver/qbittorrent:5.1.4",
                    network_mode="service:gluetun",
                ),
            ),
        )

    def test_service_image_pairs_reads_labels(self) -> None:
        stack = self.base / "media"
        stack.mkdir()
        (stack / ".fake-docker-id").write_text("media\n", encoding="utf-8")
        (stack / "docker-compose.yml").write_text(
            "\n".join(
                [
                    "services:",
                    "  app:",
                    "    image: repo/app@sha256:old",
                    "    labels:",
                    "      - wud.tag.include=^latest$$",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            self.compose.service_image_pairs(stack, "docker-compose.yml"),
            (
                ServiceImage(
                    service="app",
                    image="repo/app@sha256:old",
                    labels=(("wud.tag.include", "^latest$$"),),
                ),
            ),
        )

    def test_service_image_pairs_reads_platform(self) -> None:
        stack = self.base / "media"
        stack.mkdir()
        (stack / ".fake-docker-id").write_text("media\n", encoding="utf-8")
        (stack / "docker-compose.yml").write_text(
            "\n".join(
                [
                    "services:",
                    "  app:",
                    "    image: repo/app:1.0",
                    "    platform: linux/arm64/v8",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            self.compose.service_image_pairs(stack, "docker-compose.yml"),
            (
                ServiceImage(
                    service="app",
                    image="repo/app:1.0",
                    platform=ImagePlatform("linux", "arm64", "v8"),
                ),
            ),
        )

    def test_service_bind_mounts_from_config_json_reads_bind_sources(self) -> None:
        config_json = json.dumps(
            {
                "services": {
                    "web": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": "/host/docker/web/config",
                                "target": "/config",
                            },
                            {
                                "type": "volume",
                                "source": "named-data",
                                "target": "/data",
                            },
                        ]
                    },
                    "worker": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": "/mnt/pool/worker",
                                "target": "/work",
                            }
                        ]
                    },
                }
            }
        )

        self.assertEqual(
            _service_bind_mounts_from_config_json(config_json),
            (
                ComposeBindMount("web", "/host/docker/web/config", "/config"),
                ComposeBindMount("worker", "/mnt/pool/worker", "/work"),
            ),
        )

    def test_service_runtime_port_issues_accept_valid_values(self) -> None:
        config_json = json.dumps(
            {
                "services": {
                    "web": {
                        "expose": ["8083", "9000-9002/tcp"],
                        "ports": [
                            {"target": 8083, "published": "8083"},
                            {"target": 8443, "published": "10443-10444"},
                        ],
                    }
                }
            }
        )

        self.assertEqual(_service_runtime_port_issues_from_config_json(config_json), ())

    def test_service_runtime_port_issues_reports_smart_quote_expose(self) -> None:
        config_json = json.dumps(
            {
                "services": {
                    "calibre-web": {
                        "expose": ["\u201c8083\u201d"],
                    }
                }
            }
        )

        self.assertEqual(
            _service_runtime_port_issues_from_config_json(config_json),
            (
                ComposeRuntimePortIssue(
                    service="calibre-web",
                    field="expose",
                    value="\u201c8083\u201d",
                    reason="expected numeric port or port range from 1 to 65535",
                ),
            ),
        )

    def test_ps_quiet_scopes_to_services_when_provided(self) -> None:
        stack = self.make_stack(
            "stack",
            [
                ("app", "repo/app:latest", "cid-app"),
                ("db", "repo/db:latest", "cid-db"),
            ],
        )

        self.assertEqual(
            self.compose.ps_quiet(stack, "docker-compose.yml", ["app"]),
            ["cid-app"],
        )
        self.assertEqual(
            self.compose.ps_quiet(stack, "docker-compose.yml"),
            ["cid-app", "cid-db"],
        )

    def test_pull_and_recreate_service_scoped_order_matches_shell(self) -> None:
        stack = self.make_stack(
            "stack",
            [
                ("app", "repo/app:latest", "cid-app"),
                ("db", "repo/db:latest", "cid-db"),
            ],
        )
        self.set_image_after_pull("repo/app:latest", "new-app", "sha256:new-app")

        self.compose.pull_and_recreate(
            stack,
            "docker-compose.yml",
            mode="stop",
            services=["app"],
            use_native_wait=False,
        )

        self.assertEqual(
            self.call_commands(),
            [
                "compose -f docker-compose.yml pull app",
                "compose -f docker-compose.yml stop app",
                "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps app",
            ],
        )

    def test_pull_and_recreate_stack_level_stops_before_force_recreate(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        self.set_image_after_pull("repo/app:latest", "new-app", "sha256:new-app")

        self.compose.pull_and_recreate(
            stack,
            "docker-compose.yml",
            mode="stop",
            use_native_wait=False,
        )

        self.assertEqual(
            self.call_commands(),
            [
                "compose -f docker-compose.yml pull ",
                "compose -f docker-compose.yml config --services",
                "compose -f docker-compose.yml stop app",
                "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --force-recreate",
            ],
        )

    def test_pull_and_recreate_rejects_invalid_mode_before_pull(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])

        with self.assertRaisesRegex(ValueError, "mode must be pause, stop, or live"):
            self.compose.pull_and_recreate(
                stack,
                "docker-compose.yml",
                mode="invalid",
            )

        self.assertEqual(self.call_commands(), [])

    def test_pull_and_recreate_attempts_up_after_stack_stop_failure(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        self.set_image_after_pull("repo/app:latest", "new-app", "sha256:new-app")
        (self.fake_root / "stacks" / "stack" / "stop_fail").write_text(
            "1",
            encoding="utf-8",
        )

        with self.assertRaises(CommandError):
            self.compose.pull_and_recreate(
                stack,
                "docker-compose.yml",
                mode="stop",
                use_native_wait=False,
            )

        self.assertEqual(
            self.call_commands(),
            [
                "compose -f docker-compose.yml pull ",
                "compose -f docker-compose.yml config --services",
                "compose -f docker-compose.yml stop app",
                "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --force-recreate",
            ],
        )

    def test_pull_and_recreate_pause_mode_does_not_use_native_wait(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        self.set_image_after_pull("repo/app:latest", "new-app", "sha256:new-app")

        self.compose.pull_and_recreate(
            stack,
            "docker-compose.yml",
            mode="pause",
            services=["app"],
            use_native_wait=True,
        )

        self.assertEqual(
            self.call_commands(),
            [
                "compose -f docker-compose.yml pull app",
                "compose -f docker-compose.yml pause app",
                "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps app",
                "compose -f docker-compose.yml unpause app",
            ],
        )

    def test_pull_and_recreate_pause_failure_continues_like_shell(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        self.set_image_after_pull("repo/app:latest", "new-app", "sha256:new-app")
        (self.fake_root / "stacks" / "stack" / "pause_fail").write_text(
            "1",
            encoding="utf-8",
        )

        self.compose.pull_and_recreate(
            stack,
            "docker-compose.yml",
            mode="pause",
            services=["app"],
            use_native_wait=True,
        )

        self.assertEqual(
            self.call_commands(),
            [
                "compose -f docker-compose.yml pull app",
                "compose -f docker-compose.yml pause app",
                "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps app",
                "compose -f docker-compose.yml unpause app",
            ],
        )

    def test_up_wait_detection_and_wait_args_match_shell_order(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])
        self.assertFalse(self.compose.up_wait_supported(stack, "docker-compose.yml"))

        wait_env = dict(self.env)
        wait_env["FAKE_COMPOSE_UP_WAIT"] = "1"
        wait_compose = ComposeCli(runner=CommandRunner(env=wait_env))
        self.assertTrue(wait_compose.up_wait_supported(stack, "docker-compose.yml"))
        self.clear_calls()

        wait_compose.up(
            stack,
            "docker-compose.yml",
            ["app"],
            wait=True,
            wait_timeout=7,
        )

        self.assertEqual(
            self.call_commands(),
            [
                "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps --wait --wait-timeout 7 app"
            ],
        )

    def test_up_can_omit_no_deps_for_expanded_service_scope(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", "cid-app")])

        self.compose.up(
            stack,
            "docker-compose.yml",
            ["provider", "app"],
            no_deps=False,
        )

        self.assertEqual(
            self.call_commands(),
            [
                "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build provider app",
            ],
        )

    def test_up_can_recreate_service_without_starting_it(self) -> None:
        stack = self.make_stack("stack", [("app", "repo/app:latest", None)])

        self.compose.up(
            stack,
            "docker-compose.yml",
            ["app"],
            no_start=True,
        )

        self.assertEqual(
            self.call_commands(),
            [
                "compose -f docker-compose.yml up -d --remove-orphans --pull never --no-build --no-deps --no-start app",
            ],
        )
        self.assertEqual(
            self.compose.ps_quiet_checked(stack, "docker-compose.yml", ["app"]),
            [],
        )

def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


if __name__ == "__main__":
    unittest.main()
