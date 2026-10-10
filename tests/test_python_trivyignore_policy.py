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
assert spec is not None
assert spec.loader is not None
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
        [sys.executable, str(CHECKER)],
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
    assert any(missing in error for error in errors_for(entry(**{missing: "  "})))


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
    assert len(notices) == 1
    assert "no longer applies" in notices[0]


@pytest.mark.parametrize("key", ["id", "statement"])
@pytest.mark.parametrize("value", [["CVE-2026-78669"], {"text": "x"}, 7])
def test_id_and_statement_must_be_text(key: str, value) -> None:
    assert any(f"missing {key}" in error for error in errors_for(entry(**{key: value})))


@pytest.mark.parametrize("key", ["paths", "purls"])
@pytest.mark.parametrize("value", [[], None, "usr/local/bin/docker", [""], [7]])
def test_filters_must_be_non_empty_lists(key: str, value) -> None:
    errors = errors_for({**entry(), key: value})
    assert any(f"{key} must be a non-empty list" in error for error in errors)


def test_misspelled_filter_keys_fail() -> None:
    errors = errors_for(entry(paths=None, path=["usr/local/bin/docker"]))
    assert any("unsupported keys: path" in error for error in errors)


def test_purls_filter_is_allowed() -> None:
    assert errors_for(entry(purls=["pkg:golang/stdlib@v1.26.8"])) == []


def test_malformed_files_fail() -> None:
    assert validate({"vulnerabilities": [], "secrets": []}, TODAY)[0]
    assert validate(["CVE-2026-78669"], TODAY)[0]
    assert errors_for("CVE-2026-78669")


def test_check_file_rejects_a_dateless_entry(tmp_path: Path, capsys) -> None:
    ignore_file = tmp_path / ".trivyignore.yaml"
    ignore_file.write_text(
        "vulnerabilities:\n  - id: CVE-2026-78669\n    statement: no date\n",
        encoding="utf-8",
    )
    assert check_trivyignore.check_file(ignore_file) == 1
    assert "missing expired_at" in capsys.readouterr().err


def test_check_file_reports_unreadable_files(tmp_path: Path, capsys) -> None:
    assert check_trivyignore.check_file(tmp_path / "missing.yaml") == 1
    assert "Could not read" in capsys.readouterr().err
