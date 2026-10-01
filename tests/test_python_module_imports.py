from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# Import every wudup module first in a clean module cache, so a circular import
# cannot hide behind the order in which web.py happens to import modules.
_SCRIPT = """
import importlib
import pkgutil
import sys

import wudup

failures = []
for info in sorted(pkgutil.iter_modules(wudup.__path__), key=lambda info: info.name):
    for loaded in [name for name in sys.modules if name == "wudup" or name.startswith("wudup.")]:
        del sys.modules[loaded]
    name = f"wudup.{info.name}"
    try:
        importlib.import_module(name)
    except Exception as exc:
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
print("\\n".join(failures))
sys.exit(1 if failures else 0)
"""


class ModuleImportTests(unittest.TestCase):
    def test_every_module_imports_first_without_circular_import(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(SRC), env.get("PYTHONPATH", "")) if part
        )

        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT],
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=120,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
