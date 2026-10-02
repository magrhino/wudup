from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from tests.db_helpers import db_connection

from wudup.command import CommandRunner
from wudup.digest_verifier import (
    DigestVerifier,
    DockerManifestResolver,
    ManifestDocument,
    ManifestLookupError,
)
from wudup.docker_cli import DockerCli
from wudup.updater import (
    UpdateFromWudRunner,
)
from wudup.updater_models import (
    DigestPinUpdate,
    UpdaterOptions,
)

MANIFEST_INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_IMAGE_TYPE = "application/vnd.oci.image.manifest.v1+json"


class FailingManifestResolver:
    def fetch(self, repo: str, reference: str) -> ManifestDocument:
        raise ManifestLookupError("primary unavailable in fake Docker tests")
def manifest_index(*children: str) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "mediaType": MANIFEST_INDEX_TYPE,
        "manifests": [
            {
                "mediaType": MANIFEST_IMAGE_TYPE,
                "digest": child,
                "platform": {"os": "linux", "architecture": "amd64"},
            }
            for child in children
        ],
    }
def manifest_index_digest(digest: str, *children: str) -> dict[str, object]:
    payload = manifest_index(*children)
    payload["Descriptor"] = {"digest": digest}
    return payload
def verbose_manifest_item(
    digest: str,
    *,
    config_digest: str = "sha256:config",
    architecture: str = "amd64",
) -> dict[str, object]:
    return {
        "Ref": f"docker.io/repo/app:1.0@{digest}",
        "Descriptor": {
            "mediaType": MANIFEST_IMAGE_TYPE,
            "digest": digest,
            "platform": {"os": "linux", "architecture": architecture},
        },
        "SchemaV2Manifest": manifest_image(config_digest),
    }
def manifest_image(config_digest: str) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "mediaType": MANIFEST_IMAGE_TYPE,
        "config": {"digest": config_digest},
    }
class FakeDockerTestCase(unittest.TestCase):
    tmp_prefix = "wud-python-update."

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix=self.tmp_prefix)
        self.root = Path(self.tmp.name)
        self.repo_root = Path(__file__).resolve().parents[1]
        self.base = self.root / "base"
        self.wud_file = self.root / "images.todo"
        self.log_dir = self.root / "logs"
        self.db_path = self.root / "state" / "wudup.sqlite"
        self.fake_root = self.root / "fake"
        for path in (
            self.base,
            self.log_dir,
            self.fake_root / "images",
            self.fake_root / "manifests",
            self.fake_root / "stacks",
            self.fake_root / "containers",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.fake_root / "containers.tsv").write_text("", encoding="utf-8")
        (self.fake_root / "calls.log").write_text("", encoding="utf-8")

        self.env = os.environ.copy()
        self.env["FAKE_DOCKER_ROOT"] = str(self.fake_root)
        self.env["PATH"] = f"{self.repo_root / 'tests' / 'fakes'}:{self.env['PATH']}"
        self.env["PYTHONPATH"] = str(self.repo_root / "src")
        self.env["WUDUP_BANNER"] = "false"
        self.env["WUD_DB_PATH"] = str(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def append_compose_runtime_service(
        self,
        directory: Path,
        compose_file: Path,
        project: str,
        service: str,
    ) -> None:
        with (self.fake_root / "compose-runtime.tsv").open(
            "a",
            encoding="utf-8",
        ) as file:
            file.write(
                f"{directory}\t{compose_file}\t{project}\t{service}\tFalse\n"
            )

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
        stack_state = self.fake_root / "stacks" / stack_id
        stack_state.mkdir(parents=True, exist_ok=True)
        cids: list[str] = []

        compose_lines = ["services:\n"]
        service_rows: list[str] = []
        image_rows: list[str] = []
        for service, image, cid in services:
            compose_lines.extend([f"  {service}:\n", f"    image: {image}\n"])
            service_rows.append(f"{service}\n")
            image_rows.append(f"{image}\n")
            with (stack_state / "service-images.tsv").open("a", encoding="utf-8") as file:
                file.write(f"{service}\t{image}\n")
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
                self.append_compose_runtime_service(
                    directory,
                    directory / "docker-compose.yml",
                    directory.name,
                    service,
                )

        (directory / "docker-compose.yml").write_text("".join(compose_lines), encoding="utf-8")
        (stack_state / "services.txt").write_text("".join(service_rows), encoding="utf-8")
        (stack_state / "images.txt").write_text("".join(image_rows), encoding="utf-8")
        (stack_state / "cids.txt").write_text(
            "".join(f"{cid}\n" for cid in cids),
            encoding="utf-8",
        )
        return directory

    def make_project_sharing_stack(self, stack_id: str, project_name: str) -> Path:
        """Create a discovered stack that sets Compose project ``project_name``."""
        directory = self.make_stack(stack_id, [("db", "repo/db:latest", None)])
        compose_file = directory / "docker-compose.yml"
        compose_file.write_text(
            f"name: {project_name}\n" + compose_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return directory

    def _set_manifest_stdout(self, image: str, payload: object) -> None:
        safe = safe_name(image)
        (self.fake_root / "manifests" / f"{safe}.stdout").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def set_manifest_stdout(self, image: str, payload: object) -> None:
        self._set_manifest_stdout(image, payload)

    def set_manifest_verbose_stdout(self, image: str, payload: object) -> None:
        safe = safe_name(image)
        (self.fake_root / "manifests" / f"{safe}.verbose_stdout").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def _set_manifest_failure(self, image: str, stderr: str) -> None:
        safe = safe_name(image)
        (self.fake_root / "manifests" / f"{safe}.fail").write_text("", encoding="utf-8")
        (self.fake_root / "manifests" / f"{safe}.stderr").write_text(
            stderr,
            encoding="utf-8",
        )

    def set_manifest_failure(self, image: str, stderr: str) -> None:
        self._set_manifest_failure(image, stderr)

    def _calls(self) -> str:
        return self.calls()

    def calls(self) -> str:
        return (self.fake_root / "calls.log").read_text(encoding="utf-8")


class UpdateFromWudRunnerTestCase(FakeDockerTestCase):
    def run_python(self, *args: str, wrapper: bool = False) -> subprocess.CompletedProcess[str]:
        common = [
            "--base",
            str(self.base),
            "--file",
            str(self.wud_file),
            "--log-dir",
            str(self.log_dir),
            "--max-wait",
            "0",
            "--no-color",
            *args,
        ]
        env = dict(self.env)
        if wrapper:
            env["PYTHON_BIN"] = sys.executable
            command = [str(self.repo_root / "bin" / "docker-update-from-wud"), *common]
        else:
            command = [
                sys.executable,
                "-m",
                "wudup.cli",
                "update-from-wud",
                *common,
            ]
        try:
            return subprocess.run(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=30.0,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"run_python timed out after {e.timeout}s.\n"
                f"Command: {e.cmd}\n"
                f"Stdout: {e.stdout or ''}\n"
                f"Stderr: {e.stderr or ''}"
            ) from e
    def updater_options(
        self,
        *,
        assume_yes: bool = True,
        allow_tag_updates: bool = False,
        digest_pin_updates: bool = False,
        db_path: Path | None = None,
    ) -> UpdaterOptions:
        return UpdaterOptions(
            docker_base=self.base,
            wud_file=self.wud_file,
            log_dir=self.log_dir,
            max_wait=0,
            assume_yes=assume_yes,
            allow_tag_updates=allow_tag_updates,
            digest_pin_updates=digest_pin_updates,
            no_color=True,
            db_path=db_path,
        )
    def make_runner(
        self,
        *,
        command_runner: CommandRunner | None = None,
        digest_verifier: DigestVerifier | None = None,
        assume_yes: bool = True,
        allow_tag_updates: bool = False,
        digest_pin_updates: bool = False,
        db_path: Path | None = None,
    ) -> UpdateFromWudRunner:
        return UpdateFromWudRunner(
            self.updater_options(
                assume_yes=assume_yes,
                allow_tag_updates=allow_tag_updates,
                digest_pin_updates=digest_pin_updates,
                db_path=db_path,
            ),
            environ=self.env,
            command_runner=command_runner or CommandRunner(env=self.env),
            digest_verifier=digest_verifier,
        )
    def set_image_state(self, image: str, image_id: str, digest: str = "") -> None:
        safe = safe_name(image)
        (self.fake_root / "images" / f"{safe}.id").write_text(
            f"{image_id}\n",
            encoding="utf-8",
        )
        (self.fake_root / "images" / f"{safe}.digests").write_text(
            f"{image}@{digest}\n" if digest else "",
            encoding="utf-8",
        )
    def set_image_after_pull(self, image: str, image_id: str, digest: str = "") -> None:
        safe = safe_name(image)
        (self.fake_root / "images" / f"{safe}.after_id").write_text(
            f"{image_id}\n",
            encoding="utf-8",
        )
        (self.fake_root / "images" / f"{safe}.after_digests").write_text(
            f"{image}@{digest}\n" if digest else "",
            encoding="utf-8",
        )
    def run_direct(
        self,
        *,
        assume_yes: bool = True,
        allow_tag_updates: bool = False,
        digest_pin_updates: bool = False,
        digest_pin_plan: tuple[DigestPinUpdate, ...] = (),
        exclude_tag_lines: str = "",
        recreate_excluded_services: bool = False,
    ) -> tuple[int, str, str]:
        command_runner = CommandRunner(env=self.env)
        docker = DockerCli(runner=command_runner)
        options = UpdaterOptions(
            docker_base=self.base,
            wud_file=self.wud_file,
            log_dir=self.log_dir,
            max_wait=0,
            assume_yes=assume_yes,
            allow_tag_updates=allow_tag_updates,
            digest_pin_updates=digest_pin_updates,
            digest_pin_plan=digest_pin_plan,
            exclude_tag_lines=exclude_tag_lines,
            recreate_excluded_services=recreate_excluded_services,
            no_color=True,
            db_path=self.db_path,
        )
        runner = UpdateFromWudRunner(
            options,
            environ=self.env,
            command_runner=command_runner,
            digest_verifier=DigestVerifier(
                docker,
                primary_resolver=FailingManifestResolver(),
                fallback_resolver=DockerManifestResolver(docker),
            ),
        )
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = runner.run()
        return status, stdout.getvalue(), stderr.getvalue()
    def latest_error_report(self) -> Path:
        reports = sorted(self.log_dir.glob("update-from-wud-v2-*.errors.log"))
        self.assertTrue(reports, "expected updater error report")
        return reports[-1]
    def latest_run_log(self) -> Path:
        logs = sorted(
            path
            for path in self.log_dir.glob("update-from-wud-v2-*.log")
            if not path.name.endswith(".errors.log")
        )
        self.assertTrue(logs, "expected updater run log")
        return logs[-1]
    def db_rows(self, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with db_connection(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(query, params))
    def prepare_digest_pin_latest_update(self) -> None:
        self.wud_file.write_text(
            "repo/app:latest@sha256:child\n",
            encoding="utf-8",
        )
        self.make_stack("app", [("app", "repo/app:latest", "cid-app")])
        self.set_image_state("repo/app:latest", "sha256:old", "sha256:old-index")
        self.set_image_after_pull(
            "repo/app:latest",
            "sha256:config",
            "sha256:index",
        )
        self.set_manifest_stdout(
            "docker.io/repo/app:latest",
            manifest_index_digest("sha256:index", "sha256:child"),
        )
        self.set_manifest_stdout(
            "docker.io/repo/app@sha256:child",
            manifest_image("sha256:config"),
        )
    def write_tag_update_health_flip_hook(self) -> None:
        hook = self.fake_root / "post-up-hook"
        hook.write_text(
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            "compose_file=\"${2:?compose file is required}\"\n"
            "if grep -q 'repo/app:2.0' \"$compose_file\"; then\n"
            "  printf '/cid-app|running|unhealthy|1|0\\n' > \"${FAKE_DOCKER_ROOT:?}/containers/cid-app.summary\"\n"
            "else\n"
            "  printf '/cid-app|running|healthy|0|0\\n' > \"${FAKE_DOCKER_ROOT:?}/containers/cid-app.summary\"\n"
            "fi\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
    def write_media_provider_post_up_hook(self) -> None:
        hook = self.fake_root / "post-up-hook"
        hook.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'printf "cid-gluetun\\n" > "$FAKE_DOCKER_ROOT/stacks/media/cids-gluetun.txt"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        hook.chmod(0o755)
    def prepare_network_mode_media_stack(
        self,
        *,
        include_provider_cid: bool,
        include_extra_consumer: bool = False,
        write_provider_hook: bool = False,
    ) -> Path:
        self.wud_file.write_text(
            "ghcr.io/linuxserver/qbittorrent:5.1.4 tag=5.2.0\n",
            encoding="utf-8",
        )
        stack_dir = self.base / "media"
        stack_dir.mkdir()
        (stack_dir / ".fake-docker-id").write_text("media\n", encoding="utf-8")
        compose_lines = [
            "services:",
            "  gluetun:",
            "    image: qmcgaw/gluetun:latest",
            "  qbittorrent:",
            "    image: ghcr.io/linuxserver/qbittorrent:5.1.4",
            "    network_mode: service:gluetun",
        ]
        if include_extra_consumer:
            compose_lines.extend(
                [
                    "  mamapi:",
                    "    image: ghcr.io/example/mamapi:latest",
                    "    network_mode: service:gluetun",
                ]
            )
        compose_lines.append("")
        compose_file = stack_dir / "docker-compose.yml"
        compose_file.write_text("\n".join(compose_lines), encoding="utf-8")

        stack_state = self.fake_root / "stacks" / "media"
        stack_state.mkdir()
        cids = ["cid-qbittorrent"]
        if include_provider_cid:
            cids.insert(0, "cid-gluetun")
        if include_extra_consumer:
            cids.append("cid-mamapi")
        (stack_state / "cids.txt").write_text(
            "".join(f"{cid}\n" for cid in cids),
            encoding="utf-8",
        )
        if include_provider_cid:
            (stack_state / "cids-gluetun.txt").write_text(
                "cid-gluetun\n",
                encoding="utf-8",
            )
        (stack_state / "cids-qbittorrent.txt").write_text(
            "cid-qbittorrent\n",
            encoding="utf-8",
        )
        summary_cids = set(cids)
        if write_provider_hook:
            summary_cids.add("cid-gluetun")
        for cid in sorted(summary_cids):
            (self.fake_root / "containers" / f"{cid}.summary").write_text(
                f"/{cid}|running|healthy|0|0\n",
                encoding="utf-8",
            )
        runtime_services = ["qbittorrent"]
        if include_provider_cid:
            runtime_services.insert(0, "gluetun")
        if include_extra_consumer:
            runtime_services.append("mamapi")
        for service in runtime_services:
            self.append_compose_runtime_service(
                stack_dir,
                compose_file,
                "media",
                service,
            )
        if write_provider_hook:
            self.write_media_provider_post_up_hook()
        self.set_image_state(
            "ghcr.io/linuxserver/qbittorrent:5.1.4",
            "old-qbit",
            "sha256:old-qbit",
        )
        self.set_image_after_pull(
            "ghcr.io/linuxserver/qbittorrent:5.2.0",
            "new-qbit",
            "sha256:new-qbit",
        )
        return compose_file


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)
