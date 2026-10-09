"""Validate .trivyignore.yaml against the time-boxed image scan ignore policy.

Trivy stops applying an entry once its expired_at date passes, so a finding
that is still unpatched blocks the scans (and fails the scheduled scan) again.
Trivy treats an entry without a date as permanent, so this check requires
every entry to have an id, a statement, and an expired_at within MAX_DAYS.
See SECURITY.md#release-image-policy.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

IGNORE_FILE = Path(__file__).resolve().parents[1] / ".trivyignore.yaml"
MAX_DAYS = 90
TEXT_KEYS = ("id", "statement")


def _check_expiry(label: str, expires: Any, today: date, latest: date) -> tuple[list[str], list[str]]:
    if expires is None:
        return [f"{label} is missing expired_at."], []
    if isinstance(expires, datetime) or not isinstance(expires, date):
        return [f"{label} expired_at must be an unquoted date such as {latest.isoformat()}."], []
    if expires > latest:
        message = (
            f"{label} expires {expires.isoformat()}, more than {MAX_DAYS} days away; "
            f"use {latest.isoformat()} or earlier and renew it if still needed."
        )
        return [message], []
    if expires <= today:
        return [], [f"{label} expired on {expires.isoformat()} and no longer applies; remove it."]
    return [], []


def _check_entry(index: int, entry: Any, today: date, latest: date) -> tuple[list[str], list[str]]:
    if not isinstance(entry, dict):
        return [f"Entry {index} must be a mapping with id, statement, and expired_at."], []
    label = f"Entry {index} ({entry.get('id', 'no id')})"
    errors = [
        f"{label} is missing {key}; it must be non-empty text."
        for key in TEXT_KEYS
        if not isinstance(entry.get(key), str) or not entry[key].strip()
    ]
    expiry_errors, notices = _check_expiry(label, entry.get("expired_at"), today, latest)
    return errors + expiry_errors, notices


def validate(data: Any, today: date) -> tuple[list[str], list[str]]:
    """Return (errors, notices) for parsed ignore-file data."""
    if not isinstance(data, dict) or set(data) != {"vulnerabilities"}:
        return ["The ignore file must contain only a top-level 'vulnerabilities' list."], []
    entries = data["vulnerabilities"] or []
    if not isinstance(entries, list):
        return ["'vulnerabilities' must be a list (use [] when nothing is ignored)."], []

    latest = today + timedelta(days=MAX_DAYS)
    errors: list[str] = []
    notices: list[str] = []
    for index, entry in enumerate(entries, start=1):
        entry_errors, entry_notices = _check_entry(index, entry, today, latest)
        errors += entry_errors
        notices += entry_notices
    return errors, notices


def check_file(path: Path) -> int:
    """Print problems with the ignore file at path; return a process exit code."""
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        print(f"Could not read the image scan ignore file {path.name}: {exc}", file=sys.stderr)
        return 1
    errors, notices = validate(data, datetime.now(timezone.utc).date())
    for notice in notices:
        print(f"{path.name}: {notice}")
    if errors:
        print(f"{path.name} breaks the image scan ignore policy:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(check_file(IGNORE_FILE))
