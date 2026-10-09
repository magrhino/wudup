"""Validate .trivyignore.yaml against the time-boxed image scan ignore policy.

Trivy itself stops applying an entry once its expired_at date passes, but it
treats an entry without a date as permanent. This check makes every entry
name one vulnerability, scope it to exact image paths, explain why, and expire
within MAX_DAYS. See SECURITY.md#release-image-policy.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

IGNORE_FILE = ".trivyignore.yaml"
MAX_DAYS = 90
ID_PATTERN = re.compile(r"^(CVE-\d{4}-\d{4,}|GHSA(-[23456789cfghjmpqrvwx]{4}){3})$")
REQUIRED_KEYS = {"id", "paths", "statement", "expired_at"}
ALLOWED_KEYS = REQUIRED_KEYS | {"purls"}
GLOB_CHARS = set("*?[]{}")


def validate(data: Any, today: date) -> tuple[list[str], list[str]]:
    """Return (errors, notices) for parsed ignore-file data."""
    errors: list[str] = []
    notices: list[str] = []
    if not isinstance(data, dict) or set(data) != {"vulnerabilities"}:
        return [
            "The ignore file must contain only a top-level 'vulnerabilities' list."
        ], notices
    entries = data["vulnerabilities"]
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        return ["'vulnerabilities' must be a list (use [] when nothing is ignored)."], notices

    latest = today + timedelta(days=MAX_DAYS)
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        label = f"Entry {index}"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be a mapping with id, paths, statement, and expired_at.")
            continue
        vuln_id = entry.get("id")
        if isinstance(vuln_id, str):
            label = f"Entry {index} ({vuln_id})"
        missing = sorted(REQUIRED_KEYS - set(entry))
        if missing:
            errors.append(f"{label} is missing {', '.join(missing)}.")
        unknown = sorted(set(entry) - ALLOWED_KEYS)
        if unknown:
            errors.append(f"{label} has unsupported keys: {', '.join(unknown)}.")

        if "id" in entry:
            if not isinstance(vuln_id, str) or not ID_PATTERN.match(vuln_id):
                errors.append(f"{label} id must be one CVE-YYYY-NNNN or GHSA-xxxx-xxxx-xxxx identifier.")
            elif vuln_id in seen:
                errors.append(f"{label} is listed more than once; put all its paths in one entry.")
            else:
                seen.add(vuln_id)

        if "paths" in entry:
            paths = entry["paths"]
            if not isinstance(paths, list) or not paths:
                errors.append(f"{label} paths must be a non-empty list of image file paths.")
            else:
                for path in paths:
                    if not isinstance(path, str) or not path.strip():
                        errors.append(f"{label} paths must be non-empty strings.")
                    elif GLOB_CHARS & set(path) or path.startswith("/"):
                        errors.append(
                            f"{label} path {path!r} must be an exact image path without a "
                            "leading slash or wildcards, as shown in the scan output."
                        )

        if "purls" in entry:
            purls = entry["purls"]
            if not isinstance(purls, list) or not purls or not all(
                isinstance(purl, str) and purl.startswith("pkg:") for purl in purls
            ):
                errors.append(f"{label} purls must be a non-empty list of pkg: URLs when present.")

        if "statement" in entry:
            statement = entry["statement"]
            if not isinstance(statement, str) or not statement.strip():
                errors.append(f"{label} statement must explain why the finding is ignored.")

        if "expired_at" in entry:
            expires = entry["expired_at"]
            if isinstance(expires, datetime) or not isinstance(expires, date):
                errors.append(
                    f"{label} expired_at must be an unquoted date such as "
                    f"{latest.isoformat()}."
                )
            elif expires > latest:
                errors.append(
                    f"{label} expires {expires.isoformat()}, more than {MAX_DAYS} days away; "
                    f"use {latest.isoformat()} or earlier and renew it if still needed."
                )
            elif expires <= today:
                notices.append(
                    f"{label} expired on {expires.isoformat()} and no longer applies; remove it."
                )
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
