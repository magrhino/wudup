from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.db_helpers import db_connection

from wudup.platforms import ImagePlatform
from wudup.security_subjects import PendingSecurityOptions, pending_security_context
from wudup.web_config import load_web_settings
from wudup.web_security import security_scans_response


class WebuiDemoStateTests(unittest.TestCase):
    def test_demo_state_seed_is_idempotent(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "webui" / "scripts" / "seed_demo_state.py"
        seeded_scan_ref = (
            "postgres:16@sha256:"
            "1111111111111111111111111111111111111111111111111111111111111111"
        )
        seeded_finding_id = "CVE-2026-0001"
        with tempfile.TemporaryDirectory(prefix="wud-webui-demo.") as tmpdir:
            root = Path(tmpdir) / "local-dev"

            for _ in range(2):
                subprocess.run(
                    [sys.executable, str(script), "--root", str(root), "--quiet"],
                    cwd=repo_root,
                    check=True,
                    text=True,
                    capture_output=True,
                )

            wud_file = root / "out" / "images.todo"
            db_path = root / "logs" / "wudup.sqlite"
            docker_base = root / "docker"
            fake_docker_root = root / "fake-docker"
            logs = sorted(path.name for path in (root / "logs").glob("demo-*.log"))
            compose_files = sorted(
                path.relative_to(docker_base).as_posix()
                for path in docker_base.glob("*/docker-compose.yml")
            )

            self.assertIn("home-assistant", wud_file.read_text(encoding="utf-8"))
            self.assertIn("gethomepage/homepage", wud_file.read_text(encoding="utf-8"))
            self.assertIn(
                "n8nio/runners:2.33.5-distroless tag=2.34.4",
                wud_file.read_text(encoding="utf-8"),
            )
            containers = (fake_docker_root / "containers.tsv").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "homepage\tghcr.io/gethomepage/homepage:v0.9.12",
                containers,
            )
            self.assertIn("vaultwarden\tvaultwarden/server:1.31.0", containers)
            self.assertIn("watchtower\tcontainrrr/watchtower:1.7.1", containers)
            self.assertEqual(
                logs,
                ["demo-dry-run.log", "demo-failed.log", "demo-success.log"],
            )
            self.assertEqual(
                compose_files,
                [
                    "data/docker-compose.yml",
                    "home/docker-compose.yml",
                    "jarvis/docker-compose.yml",
                    "media/docker-compose.yml",
                ],
            )
            self.assertIn(
                "n8nio/runners:2.33.5-distroless",
                (docker_base / "jarvis" / "docker-compose.yml").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertIn(
                "lscr.io/linuxserver/radarr:5.21.1",
                (docker_base / "media" / "docker-compose.yml").read_text(
                    encoding="utf-8"
                ),
            )
            media_compose = (docker_base / "media" / "docker-compose.yml").read_text(
                encoding="utf-8"
            )
            self.assertIn("ghcr.io/magrhino/wudup:latest", media_compose)
            self.assertIn("wud.tag.include=^latest$$", media_compose)
            self.assertTrue((fake_docker_root / "calls.log").exists())
            self.assertTrue((fake_docker_root / "containers.tsv").exists())
            self.assertTrue(
                (fake_docker_root / "containers" / "homepage.summary").exists()
            )
            self.assertTrue(
                (
                    fake_docker_root
                    / "containers"
                    / "demo-wudup.summary"
                ).exists()
            )
            self.assertTrue(
                (
                    fake_docker_root
                    / "manifests"
                    / "n8nio_runners_2.34.4-distroless.stdout"
                ).exists()
            )
            self.assertTrue(
                (
                    fake_docker_root
                    / "images"
                    / "n8nio_runners_2.34.4.after_digests"
                ).exists()
            )
            self.assertIn(
                "WUD-UPDATER-RECREATE-STACK=true",
                (
                    fake_docker_root
                    / "containers"
                    / "cid-data-postgres.labels"
                ).read_text(encoding="utf-8"),
            )
            self.assertTrue(
                (
                    fake_docker_root
                    / "images"
                    / "postgres_16.after_digests"
                ).exists()
            )
            self.assertTrue(
                (
                    fake_docker_root
                    / "images"
                    / "lscr.io_linuxserver_radarr_5.22.4.after_id"
                ).exists()
            )
            result = subprocess.run(
                ["docker", "compose", "-f", "docker-compose.yml", "config", "--images"],
                cwd=docker_base / "media",
                env={
                    **os.environ,
                    "PATH": f"{repo_root / 'tests' / 'fakes'}:{os.environ['PATH']}",
                    "FAKE_DOCKER_ROOT": str(fake_docker_root),
                },
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("ghcr.io/magrhino/wudup:latest", result.stdout)
            with db_connection(db_path) as conn:
                run_count = conn.execute("SELECT COUNT(*) FROM update_runs").fetchone()
                pending_count = conn.execute(
                    "SELECT COUNT(*) FROM pending_updates"
                ).fetchone()
                event_count = conn.execute("SELECT COUNT(*) FROM update_events").fetchone()
                policy_count = conn.execute(
                    "SELECT COUNT(*) FROM service_policy"
                ).fetchone()
                snooze_count = conn.execute("SELECT COUNT(*) FROM snoozes").fetchone()
                dependency_snooze_count = conn.execute(
                    "SELECT COUNT(*) FROM dependency_snoozes"
                ).fetchone()
                tag_exclusion_count = conn.execute(
                    "SELECT COUNT(*) FROM tag_exclusion_rules"
                ).fetchone()
                active_exclusions = conn.execute(
                    """
                    SELECT image_repo, status
                    FROM tag_exclusion_rules
                    WHERE status = 'active'
                    """
                ).fetchall()
                versions = conn.execute(
                    """
                    SELECT status, dry_run, log_file
                    FROM update_runs
                    ORDER BY id
                    """
                ).fetchall()
                audit_modes = conn.execute(
                    """
                    SELECT mode
                    FROM update_runs
                    WHERE mode LIKE 'web-%'
                    ORDER BY id
                    """
                ).fetchall()
                retag_candidates = conn.execute(
                    """
                    SELECT image, digest_resolved_tag, digest_watch_tag, digest_final_image
                    FROM known_images
                    WHERE service_key = 'media/wudup'
                    """
                ).fetchall()
                security_scan = conn.execute(
                    """
                    SELECT requested_ref, reported_digest, platform, state, verdict,
                           findings_json
                    FROM security_scan_cache
                    WHERE requested_ref = ?
                    """,
                    (seeded_scan_ref,),
                ).fetchone()
            static_dir = root / "static"
            static_dir.mkdir()
            settings = load_web_settings(
                {
                    "DOCKER_BASE": str(docker_base),
                    "HOST_DOCKER_BASE": str(docker_base),
                    "WUD_OUT_FILE": str(wud_file),
                    "WUD_LOG_DIR": str(root / "logs"),
                    "WUD_DB_PATH": str(db_path),
                    "WUD_WEB_STATIC_DIR": str(static_dir),
                    "WUD_SECURITY_SCANNING_ENABLED": "true",
                    "WUD_PENDING_SOURCE": "file",
                }
            )
            security_scans = security_scans_response(settings)
            scan_context = pending_security_context(
                settings,
                options=PendingSecurityOptions(
                    include_compose=False,
                    include_wud_metadata=False,
                ),
            )
            finding_scan = next(
                (
                    item
                    for item in security_scans.items
                    if any(
                        finding.vulnerability_id == seeded_finding_id
                        for finding in item.findings
                    )
                ),
                None,
            )
            seeded_finding = next(
                (
                    finding
                    for item in security_scans.items
                    for finding in item.findings
                    if finding.vulnerability_id == seeded_finding_id
                ),
                None,
            )
            scan_request = next(
                request for request in scan_context.requests if request.line_no == 5
            )

        self.assertEqual(run_count[0], 6)
        self.assertEqual(pending_count[0], 4)
        self.assertEqual(event_count[0], 6)
        self.assertEqual(policy_count[0], 2)
        self.assertEqual(snooze_count[0], 2)
        self.assertEqual(dependency_snooze_count[0], 1)
        self.assertEqual(tag_exclusion_count[0], 2)
        self.assertEqual(
            active_exclusions[0][0],
            "ghcr.io/home-assistant/home-assistant",
        )
        self.assertEqual(versions[0][0], "success")
        self.assertEqual(versions[2][1], 1)
        self.assertTrue(versions[0][2].endswith("demo-success.log"))
        self.assertEqual(
            [row[0] for row in audit_modes],
            ["web-auth", "web-state", "web-settings"],
        )
        self.assertEqual(len(retag_candidates), 1)
        self.assertEqual(
            retag_candidates[0],
            (
                "ghcr.io/magrhino/wudup:latest",
                "v0.16.1",
                "latest",
                (
                    "ghcr.io/magrhino/wudup@sha256:"
                    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
                ),
            ),
        )
        self.assertIsNotNone(security_scan)
        self.assertEqual(
            security_scan[:5],
            (
                seeded_scan_ref,
                (
                    "sha256:"
                    "1111111111111111111111111111111111111111111111111111111111111111"
                ),
                "linux/amd64",
                "complete",
                "findings",
            ),
        )
        self.assertIn(seeded_finding_id, security_scan[5])
        self.assertIsNotNone(finding_scan)
        self.assertEqual(finding_scan.line_no, 5)
        self.assertIsNotNone(seeded_finding)
        self.assertEqual(seeded_finding.vulnerability_id, seeded_finding_id)
        self.assertEqual(scan_request.platform, ImagePlatform("linux", "amd64"))
        self.assertEqual(scan_request.identity_status, "pending")
        self.assertEqual(scan_request.error, "")


if __name__ == "__main__":
    unittest.main()
