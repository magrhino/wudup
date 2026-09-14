from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ContainerSmokeTests(unittest.TestCase):
    def test_health_fixture_exercises_permission_repair_with_configured_owner(self) -> None:
        source = (ROOT / "tests/smoke-container-image.sh").read_text()
        function = re.search(
            r"^smoke_default_web_health\(\)\{.*?^\}", source, re.MULTILINE | re.DOTALL
        )
        self.assertIsNotNone(function)
        with tempfile.TemporaryDirectory(prefix="wudup-smoke-test.") as tmp:
            args_path = Path(tmp) / "docker-args"
            result = subprocess.run(
                ["bash", "-c", "\n".join((
                    "set -euo pipefail",
                    function.group(),
                    'run_with_timeout(){ printf "%s\\0" "$@" > "$SMOKE_ARGS"; }',
                    'run(){ :; }',
                    "umask 000",
                    "smoke_default_web_health",
                    'printf "%s" "$HEALTH_TMP"',
                ))],
                env={
                    **os.environ,
                    "TMPDIR": tmp,
                    "SMOKE_ARGS": str(args_path),
                    "SMOKE_LABEL": "wudup.image-smoke=test",
                    "RUN_ID_COMPONENT": "test",
                    "IMAGE": "wudup:test",
                },
                capture_output=True,
                text=True,
                check=True,
            )
            logs = Path(result.stdout) / "logs"
            args = args_path.read_text().rstrip("\0").split("\0")
            options = list(pairwise(args))
            self.assertEqual(stat.S_IMODE(logs.stat().st_mode), 0o770)
            self.assertIn(("-v", f"{logs}:/logs"), options)
            for name, value in (("OUT_UID", os.getuid()), ("OUT_GID", os.getgid())):
                self.assertIn(("-e", f"{name}={value}"), options)


if __name__ == "__main__":
    unittest.main()
