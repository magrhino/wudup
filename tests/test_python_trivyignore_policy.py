from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_trivyignore.py"
TODAY = date(2026, 10, 9)

spec = importlib.util.spec_from_file_location("check_trivyignore", CHECKER)
assert spec and spec.loader
check_trivyignore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_trivyignore)
validate = check_trivyignore.validate


def entry(**overrides):
    base = {
        "id": "CVE-2026-78669",
        "paths": ["usr/local/libexec/docker/cli-plugins/docker-compose"],
        "statement": "Waiting for a compose release built with the fix.",
        "expired_at": date(2026, 11, 8),
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def errors_for(*entries):
    errors, _ = validate({"vulnerabilities": list(entries)}, TODAY)
    return errors


def test_repository_ignore_file_follows_the_policy() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(ROOT / ".trivyignore.yaml")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_valid_entries_and_empty_lists_pass() -> None:
    assert errors_for(entry()) == []
    assert errors_for(entry(paths=None)) == []
    assert validate({"vulnerabilities": []}, TODAY) == ([], [])
    assert validate({"vulnerabilities": None}, TODAY) == ([], [])


@pytest.mark.parametrize("missing", ["id", "statement", "expired_at"])
def test_every_entry_needs_id_statement_and_date(missing: str) -> None:
    assert any(f"missing {missing}" in error for error in errors_for(entry(**{missing: None})))
    assert any(f"missing {missing}" in error for error in errors_for(entry(**{missing: "  "})))


@pytest.mark.parametrize(
    ("expires", "message"),
    [
        (date(2027, 1, 8), "more than 90 days away"),
        ("2026-11-08", "unquoted date"),
        (datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc), "unquoted date"),
    ],
)
def test_expiry_must_be_a_date_within_90_days(expires, message: str) -> None:
    assert any(message in error for error in errors_for(entry(expired_at=expires)))


def test_expired_entries_are_reported_but_do_not_fail() -> None:
    errors, notices = validate({"vulnerabilities": [entry(expired_at=TODAY)]}, TODAY)
    assert errors == []
    assert notices and "no longer applies" in notices[0]


def test_malformed_files_fail() -> None:
    assert validate({"vulnerabilities": [], "secrets": []}, TODAY)[0]
    assert validate(["CVE-2026-78669"], TODAY)[0]
    assert errors_for("CVE-2026-78669")


def test_cli_rejects_a_dateless_entry(tmp_path: Path) -> None:
    ignore_file = tmp_path / ".trivyignore.yaml"
    ignore_file.write_text(
        "vulnerabilities:\n  - id: CVE-2026-78669\n    statement: no date\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(ignore_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "missing expired_at" in result.stderr
