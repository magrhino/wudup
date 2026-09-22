"""Compare physical line counts in committed Git blobs; never execute repo code."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

POLICY_PATH = "maintainability-policy.json"
ENFORCED_CATEGORIES = {"production", "declarative"}
REGULAR_MODES = {"100644", "100755"}


class Git:
    def __init__(self, repo: Path):
        self.repo = repo

    def run(self, *args: str, data: bytes | None = None) -> bytes:
        env = dict(
            {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            },
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_NO_LAZY_FETCH="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
        )
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            input=data,
            capture_output=True,
            env=env,
            check=False,
        )
        if result.returncode:
            raise ValueError(
                f"Git {args[0]} failed; verify the repository and local commit objects."
            )
        return result.stdout

    def commit(self, ref: str) -> str:
        return (
            self.run("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
            .decode()
            .strip()
        )

    def tree(self, commit: str) -> dict[str, tuple[str, str]]:
        entries = {}
        for record in self.run("ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
            if record:
                metadata, path = record.split(b"\t", 1)
                mode, _, oid = metadata.decode().split()
                entries[os.fsdecode(path)] = (mode, oid)
        return entries

    def renames(self, base: str, head: str) -> dict[str, str]:
        fields = iter(
            self.run(
                "diff",
                "--name-status",
                "-z",
                "--find-renames=50%",
                "--no-ext-diff",
                "--no-textconv",
                "--no-relative",
                base,
                head,
                "--",
            ).split(b"\0")
        )
        renames = {}
        for status in fields:
            if not status:
                continue
            old = os.fsdecode(next(fields))
            if status.startswith(b"R"):
                renames[os.fsdecode(next(fields))] = old
        return renames

    def line_counts(self, trees: list[dict[str, tuple[str, str]]]) -> dict[str, int]:
        oids = sorted(
            {
                oid
                for tree in trees
                for mode, oid in tree.values()
                if mode in REGULAR_MODES
            }
        )
        # Batch by object ID, never by a user-controlled revision:path expression.
        output = self.run(
            "cat-file", "--batch", data="".join(f"{oid}\n" for oid in oids).encode()
        )
        counts = {}
        offset = 0
        for oid in oids:
            end = output.index(b"\n", offset)
            object_id, kind, size = output[offset:end].split()
            if object_id.decode() != oid or kind != b"blob":
                raise ValueError("Git did not return the requested blob.")
            offset = end + 1
            blob = output[offset : offset + int(size)]
            counts[oid] = blob.count(b"\n") + int(
                bool(blob) and not blob.endswith(b"\n")
            )
            offset += int(size) + 1
        return counts


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Invalid policy: {message}")


def keys(value: object, expected: set[str], label: str) -> None:
    require(
        isinstance(value, dict) and set(value) == expected,
        f"{label} fields must be {sorted(expected)}",
    )


def nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def exact_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith("/")
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and not any(char in value for char in "\\*?[]\0\r\n")
    )


def issue_link(value: object) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"https://[^\s/]+/[^\s]+", value)
    )


def validate_policy(policy: object) -> dict:
    keys(
        policy,
        {
            "version",
            "baseline",
            "thresholds",
            "production",
            "tests",
            "generated",
            "locks",
            "ceilings",
            "allowances",
            "remediation",
            "anti_gaming",
        },
        "root",
    )
    require(
        type(policy["version"]) is int and policy["version"] == 1, "version must be 1"
    )
    baseline = policy["baseline"]
    keys(baseline, {"status", "commit", "reason", "follow_up"}, "baseline")
    require(
        baseline["status"] in {"pending", "ready"},
        "baseline status must be pending or ready",
    )
    require(
        nonempty(baseline["reason"]) and issue_link(baseline["follow_up"]),
        "baseline needs a reason and linked follow-up",
    )
    if baseline["status"] == "ready":
        require(
            isinstance(baseline["commit"], str)
            and bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", baseline["commit"])),
            "ready baseline needs a full commit ID",
        )
    else:
        require(baseline["commit"] is None, "pending baseline must not claim a commit")
    keys(policy["thresholds"], {"warn", "block"}, "thresholds")
    warn, block = policy["thresholds"]["warn"], policy["thresholds"]["block"]
    require(
        type(warn) is int and type(block) is int and 0 < warn < block,
        "thresholds must be positive integers with warn < block",
    )
    for category, field in (
        ("tests", "prefixes"),
        ("generated", "prefixes"),
        ("locks", "paths"),
        ("production", "paths"),
    ):
        keys(
            policy[category],
            {field, "extensions"} if category == "production" else {field},
            category,
        )
        values = policy[category][field]
        require(isinstance(values, list), f"{category}.{field} must be a list")
        for value in values:
            candidate = (
                value[:-1]
                if isinstance(value, str)
                and field == "prefixes"
                and value.endswith("/")
                else value
            )
            require(
                exact_path(candidate) and (field != "prefixes" or value.endswith("/")),
                f"invalid {category}.{field} entry",
            )
    extensions = policy["production"]["extensions"]
    require(
        isinstance(extensions, list)
        and bool(extensions)
        and all(
            isinstance(ext, str) and re.fullmatch(r"\.[a-z0-9]+", ext)
            for ext in extensions
        ),
        "production extensions must be suffixes",
    )
    for section in ("ceilings", "allowances"):
        require(
            isinstance(policy[section], dict),
            f"{section} must be an exact-path mapping",
        )
        for path, record in policy[section].items():
            require(
                exact_path(path), f"{section} needs exact repository-relative paths"
            )
            keys(
                record,
                {"ceiling", "reason", "deferred", "follow_up", "re_review"},
                section,
            )
            require(
                type(record["ceiling"]) is int and record["ceiling"] > 0,
                "ceiling must be a positive integer",
            )
            require(
                nonempty(record["reason"]) and nonempty(record["re_review"]),
                "exceptions need a reason and re-review condition",
            )
            require(type(record["deferred"]) is bool, "deferred must be boolean")
            require(
                issue_link(record["follow_up"])
                if record["deferred"]
                else record["follow_up"] is None or issue_link(record["follow_up"]),
                "deferred extraction needs a linked follow-up",
            )
    require(
        not (policy["ceilings"].keys() & policy["allowances"].keys()),
        "a path cannot have both a ceiling and an allowance",
    )
    require(nonempty(policy["remediation"]), "remediation is required")
    require(
        isinstance(policy["anti_gaming"], list)
        and bool(policy["anti_gaming"])
        and all(nonempty(item) for item in policy["anti_gaming"]),
        "anti-gaming guidance is required",
    )
    return policy


def category(path: str, mode: str, policy: dict) -> str:
    if mode not in REGULAR_MODES:
        return "symlink" if mode == "120000" else "submodule"
    # Exact reviewed records remain visible even within an excluded directory.
    if path in policy["allowances"]:
        return "declarative"
    if path in policy["ceilings"] or path in policy["production"]["paths"]:
        return "production"
    for name in ("tests", "generated"):
        if path.startswith(tuple(policy[name]["prefixes"])):
            return name
    if path in policy["locks"]["paths"]:
        return "locks"
    if PurePosixPath(path).suffix in policy["production"]["extensions"]:
        return "production"
    return "other"


def file_report(
    path: str, old: str | None, before: dict, after: dict, counts: dict, policy: dict
) -> dict:
    old_entry, new_entry = before.get(old), after.get(path)
    base_lines = counts.get(old_entry[1], 0) if old_entry else 0
    head_lines = counts.get(new_entry[1], 0) if new_entry else 0
    base_category = category(old, old_entry[0], policy) if old_entry else None
    head_category = category(path, new_entry[0], policy) if new_entry else None
    current_category = head_category or base_category
    record_path = next(
        (
            name
            for name in (path, old)
            if name in policy["ceilings"] or name in policy["allowances"]
        ),
        None,
    )
    record = policy["ceilings"].get(record_path) or policy["allowances"].get(
        record_path
    )
    ceiling = (
        record["ceiling"]
        if record
        else policy["thresholds"]["block"]
        if current_category in ENFORCED_CATEGORIES
        else None
    )
    failures, warnings = [], []
    if (
        new_entry
        and base_category in ENFORCED_CATEGORIES
        and head_category not in ENFORCED_CATEGORIES
    ):
        failures.append(
            "Production moved to a non-enforced category; preserve its production classification or request an exact-path declarative allowance."
        )
    if new_entry and head_category in ENFORCED_CATEGORIES:
        is_new_production = base_category not in ENFORCED_CATEGORIES
        if (
            not is_new_production
            and base_lines > policy["thresholds"]["block"]
            and head_lines > policy["thresholds"]["block"]
            and not record
        ):
            failures.append(
                "Existing oversized production file has no reviewed ceiling; complete the approved baseline."
            )
        if head_lines > ceiling and (is_new_production or head_lines > base_lines):
            failures.append(
                "New or growing production file exceeds its applicable ceiling."
            )
        if head_category == "production" and head_lines > policy["thresholds"]["warn"]:
            warnings.append(
                "Production size needs a responsibility and navigation review."
            )
    if not new_entry:
        status = "deleted"
    elif not old_entry:
        status = "added"
    elif path != old:
        status = "renamed"
    else:
        status = "unchanged" if old_entry == new_entry else "modified"
    return {
        "path": path,
        "base_path": old,
        "status": status,
        "base_lines": base_lines,
        "head_lines": head_lines,
        "delta": head_lines - base_lines,
        "category": current_category,
        "base_category": base_category,
        "ceiling": ceiling,
        "ceiling_path": record_path,
        "failures": failures,
        "warnings": warnings,
    }


def read_policy(git: Git, head_tree: dict, policy_file: Path | None) -> dict:
    if policy_file:
        content = policy_file.read_bytes()
    else:
        entry = head_tree.get(POLICY_PATH)
        if not entry or entry[0] not in REGULAR_MODES:
            raise ValueError(
                "The committed head needs a regular maintainability-policy.json file."
            )
        content = git.run("cat-file", "blob", entry[1])
    try:
        return validate_policy(json.loads(content))
    except (TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("Invalid policy structure or encoding.") from exc


def run(args: argparse.Namespace) -> tuple[dict, int]:
    if args.policy_file and not args.report_only:
        raise ValueError(
            "--policy-file requires --report-only; enforcement uses the committed head policy."
        )
    git = Git(args.repo)
    base, head = git.commit(args.base), git.commit(args.head)
    before, after = git.tree(base), git.tree(head)
    policy = read_policy(git, after, args.policy_file)
    counts = git.line_counts([before, after])
    renames = git.renames(base, head)
    rows = [
        file_report(
            path,
            renames.get(path, path if path in before else None),
            before,
            after,
            counts,
            policy,
        )
        for path in sorted(after)
    ]
    rows.extend(
        file_report(path, path, before, after, counts, policy)
        for path in sorted(before.keys() - after.keys() - set(renames.values()))
    )
    blocked = []
    if policy["baseline"]["status"] != "ready":
        blocked.append(
            "Baseline pending: enforcement is blocked until ceilings from post-refactor main are approved."
        )
    else:
        baseline = git.commit(policy["baseline"]["commit"])
        git.run("merge-base", "--is-ancestor", baseline, base)
    failed = any(row["failures"] for row in rows)
    result = (
        "REPORT ONLY / NOT ENFORCED"
        if args.report_only
        else "BLOCKED"
        if blocked
        else "FAIL"
        if failed
        else "PASS"
    )
    report = {
        "result": result,
        "base": base,
        "head": head,
        "policy_source": "report-only external policy"
        if args.policy_file
        else f"{head}:{POLICY_PATH}",
        "baseline": policy["baseline"],
        "thresholds": policy["thresholds"],
        "blocked": blocked,
        "files": rows,
        "remediation": policy["remediation"],
        "anti_gaming": policy["anti_gaming"],
    }
    return report, 0 if args.report_only else 2 if blocked else 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument(
        "--policy-file",
        type=Path,
        help="External policy, allowed only for report-only inventory",
    )
    args = parser.parse_args()
    try:
        report, code = run(args)
    except (ValueError, OSError) as exc:
        print(json.dumps({"result": "ERROR", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
