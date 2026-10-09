"""Validate .trivyignore.yaml against the time-boxed image scan ignore policy.

Trivy stops applying an entry once its expired_at date passes, so a finding
that is still unpatched blocks the scans (and fails the scheduled scan) again.
Trivy treats an entry without a date as permanent, so this check requires
every entry to have an id, a statement, and an expired_at within MAX_DAYS.
See SECURITY.md#release-image-policy.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

IGNORE_FILE = ".trivyignore.yaml"
MAX_DAYS = 90
REQUIRED_KEYS = ("id", "statement", "expired_at")


def validate(data: Any, today: date) -> tuple[list[str], list[str]]:
    """Return (errors, notices) for parsed ignore-file data."""
    errors: list[str] = []
    notices: list[str] = []
    if not isinstance(data, dict) or set(data) != {"vulnerabilities"}:
        return ["The ignore file must contain only a top-level 'vulnerabilities' list."], notices
    entries = data["vulnerabilities"] or []
    if not isinstance(entries, list):
        return ["'vulnerabilities' must be a list (use [] when nothing is ignored)."], notices

    latest = today + timedelta(days=MAX_DAYS)
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            errors.append(f"Entry {index} must be a mapping with id, statement, and expired_at.")
            continue
        label = f"Entry {index} ({entry.get('id', 'no id')})"
        for key in REQUIRED_KEYS:
            value = entry.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"{label} is missing {key}.")
        expires = entry.get("expired_at")
        if expires is None:
            continue
        if isinstance(expires, datetime) or not isinstance(expires, date):
            errors.append(f"{label} expired_at must be an unquoted date such as {latest.isoformat()}.")
        elif expires > latest:
            errors.append(
                f"{label} expires {expires.isoformat()}, more than {MAX_DAYS} days away; "
                f"use {latest.isoformat()} or earlier and renew it if still needed."
            )
        elif expires <= today:
            notices.append(f"{label} expired on {expires.isoformat()} and no longer applies; remove it.")
    return errors, notices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", default=IGNORE_FILE)
    args = parser.parse_args(argv)
    path = Path(args.path)
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        print(f"Could not read the image scan ignore file {path}: {exc}", file=sys.stderr)
        return 1
    errors, notices = validate(data, datetime.now(timezone.utc).date())
    for notice in notices:
        print(f"{path}: {notice}")
    if errors:
        print(f"{path} breaks the image scan ignore policy:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
