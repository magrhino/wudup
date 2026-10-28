from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "docs" / "examples"
COMPOSE_EXAMPLES = (
    "docker-compose.example.yml",
    "docker-compose.webui.yml",
    "docker-compose.hardened.yml",
    "docker-compose.build.yml",
)
# The WebUI examples keep only fixed container paths and Compose defaults that
# differ from WUDup's own; every other setting comes from .env via env_file.
WEBUI_ENVIRONMENT = {
    "docker-compose.webui.yml": {
        "DOCKER_BASE",
        "WUD_OUT_FILE",
        "WUD_LOG_DIR",
        "WUD_DB_PATH",
        "WUD_WEB_PORT",
        "WUD_API_STARTUP_WAIT_SECONDS",
        "OUT_UID",
        "OUT_GID",
    },
    "docker-compose.hardened.yml": {
        "DOCKER_HOST",
        "DOCKER_BASE",
        "WUD_OUT_FILE",
        "HOME",
        "WUD_WEB_PORT",
        "WUD_API_STARTUP_WAIT_SECONDS",
        "OUT_UID",
        "OUT_GID",
    },
}


def _load(name: str) -> dict:
    return YAML(typ="safe").load((EXAMPLES / name).read_text(encoding="utf-8"))


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


class ComposeExampleEnvFileTests(unittest.TestCase):
    def test_wudup_service_loads_optional_dotenv(self) -> None:
        for name in COMPOSE_EXAMPLES:
            with self.subTest(name=name):
                wudup = _load(name)["services"]["wudup"]
                self.assertEqual(
                    wudup["env_file"],
                    [{"path": ".env", "required": False}],
                )

    def test_wud_service_never_loads_dotenv(self) -> None:
        for name in COMPOSE_EXAMPLES:
            with self.subTest(name=name):
                self.assertNotIn("env_file", _load(name)["services"]["wud"])

    def test_webui_examples_keep_only_fixed_environment(self) -> None:
        for name, expected in WEBUI_ENVIRONMENT.items():
            with self.subTest(name=name):
                environment = _load(name)["services"]["wudup"]["environment"]
                self.assertEqual(set(environment), expected)

    @unittest.skipUnless(_docker_compose_available(), "docker compose unavailable")
    def test_dotenv_reaches_wudup_and_fixed_paths_win(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wudup-compose.") as tmp:
            project = Path(tmp)
            shutil.copy(EXAMPLES / "docker-compose.webui.yml", project / "docker-compose.yml")
            (project / ".env").write_text(
                "\n".join(
                    (
                        "HOST_DOCKER_BASE=/srv/stacks",
                        "WUD_COMPOSE_IGNORE_PATHS=old,archive",
                        "WUD_OUT_FILE=/elsewhere/images.todo",
                        "WUD_DB_PATH=/elsewhere/wudup.sqlite",
                        "WUD_WEB_PORT=8080",
                        "WUD_AUTH_ADMIN_PASSWORD=example-only",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["docker", "compose", "config", "--format", "json"],
                cwd=project,
                capture_output=True,
                text=True,
                check=True,
            )
        services = json.loads(result.stdout)["services"]
        wudup = services["wudup"]["environment"]
        self.assertEqual(wudup["WUD_COMPOSE_IGNORE_PATHS"], "old,archive")
        self.assertEqual(wudup["DOCKER_BASE"], "/srv/stacks")
        self.assertEqual(wudup["WUD_OUT_FILE"], "/out/images.todo")
        self.assertEqual(wudup["WUD_DB_PATH"], "/logs/wudup.sqlite")
        self.assertEqual(wudup["WUD_WEB_PORT"], "8080")
        self.assertNotIn("WUD_COMPOSE_IGNORE_PATHS", services["wud"]["environment"])

    @unittest.skipUnless(_docker_compose_available(), "docker compose unavailable")
    def test_examples_render_without_dotenv(self) -> None:
        for name in COMPOSE_EXAMPLES:
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                prefix="wudup-compose."
            ) as tmp:
                project = Path(tmp)
                shutil.copy(EXAMPLES / name, project / "docker-compose.yml")
                result = subprocess.run(
                    ["docker", "compose", "config", "--format", "json"],
                    cwd=project,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
