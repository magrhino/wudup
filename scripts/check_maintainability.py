"""Compare physical line counts in committed Git blobs; never execute repo code."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import BinaryIO

POLICY_PATH = "maintainability-policy.json"
ENFORCED_CATEGORIES = {"production", "declarative"}
REGULAR_MODES = {"100644", "100755"}
BLOB_CHUNK_SIZE = 64 * 1024
MAX_POLICY_BYTES = 1024 * 1024


class Git:
    def __init__(self, repo: Path):
        self.repo = repo

    @staticmethod
    def environment() -> dict[str, str]:
        return dict(
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

    def run(self, *args: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            env=self.environment(),
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
        counts = {}
        if not oids:
            return counts
        with subprocess.Popen(
            ["git", "-C", str(self.repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self.environment(),
        ) as batch:
            try:
                # Drain each response before sending another OID to avoid pipe deadlocks.
                # Requests are object IDs, never candidate-controlled revision:path values.
                for oid in oids:
                    batch.stdin.write(f"{oid}\n".encode())
                    batch.stdin.flush()
                    counts[oid] = self.blob_line_count(batch.stdout, oid)
                batch.stdin.close()
                if batch.wait():
                    raise ValueError(
                        "Git cat-file failed; verify local commit objects."
                    )
            except BrokenPipeError as exc:
                raise ValueError(
                    "Git cat-file failed; verify local commit objects."
                ) from exc
            finally:
                if batch.poll() is None:
                    batch.kill()  # Popen's context manager closes pipes and reaps the child.
        return counts

    @staticmethod
    def blob_line_count(output: BinaryIO, oid: str) -> int:
        # SHA-256 OID + type + 64-bit size fits comfortably within this bounded header.
        header = output.readline(128)
        fields = header.split()
        if (
            not header.endswith(b"\n")
            or len(fields) != 3
            or fields[:2] != [oid.encode(), b"blob"]
            or not fields[2].isdigit()
        ):
            raise ValueError("Git did not return the requested blob and size.")
        remaining = int(fields[2])
        lines, final_line = 0, False
        while remaining:
            chunk = output.read(min(BLOB_CHUNK_SIZE, remaining))
            if not chunk:
                raise ValueError(
                    "Git returned a truncated blob; verify local commit objects."
                )
            remaining -= len(chunk)
            lines += chunk.count(b"\n")
            final_line = not chunk.endswith(b"\n")
        if output.read(1) != b"\n":
            raise ValueError(
                "Git returned an invalid blob separator; verify local commit objects."
            )
        return lines + int(final_line)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Invalid policy: {message}")


def keys(value: object, expected: set[str], label: str) -> dict:
    if isinstance(value, dict) and set(value) == expected:
        return value
    raise ValueError(f"Invalid policy: {label} fields must be {sorted(expected)}")


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


def commit_id(value: object) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)
    )


def validate_baseline(value: object) -> None:
    baseline = keys(value, {"status", "commit", "reason", "follow_up"}, "baseline")
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
            commit_id(baseline["commit"]),
            "ready baseline needs a full commit ID",
        )
    else:
        require(baseline["commit"] is None, "pending baseline must not claim a commit")


def validate_thresholds(value: object) -> None:
    thresholds = keys(value, {"warn", "block"}, "thresholds")
    warn, block = thresholds["warn"], thresholds["block"]
    require(
        type(warn) is int and type(block) is int and 0 < warn < block,
        "thresholds must be positive integers with warn < block",
    )


def validate_category_paths(
    definition_value: object, category: str, field: str
) -> None:
    definition = keys(
        definition_value,
        {field, "extensions"} if category == "production" else {field},
        category,
    )
    values = definition[field]
    require(isinstance(values, list), f"{category}.{field} must be a list")
    for value in values:
        candidate = value
        if isinstance(value, str) and field == "prefixes" and value.endswith("/"):
            candidate = value[:-1]
        require(
            exact_path(candidate) and (field != "prefixes" or value.endswith("/")),
            f"invalid {category}.{field} entry",
        )


def validate_exception(value: object, section: str) -> None:
    record = keys(
        value,
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


def validate_exceptions(value: object, section: str) -> None:
    if isinstance(value, dict):
        for path, record in value.items():
            require(
                exact_path(path), f"{section} needs exact repository-relative paths"
            )
            validate_exception(record, section)
        return
    raise ValueError(f"Invalid policy: {section} must be an exact-path mapping")


def validate_transitions(value: object) -> None:
    require(isinstance(value, dict), "transitions must be an exact-path mapping")
    for path, value_record in value.items():
        require(exact_path(path), "transitions need exact source paths")
        record = keys(
            value_record, {"base_commit", "destinations", "reason"}, "transition"
        )
        require(
            commit_id(record["base_commit"]), "transition needs a full base commit ID"
        )
        require(nonempty(record["reason"]), "transition needs a review reason")
        destinations = record["destinations"]
        require(
            isinstance(destinations, list) and all(exact_path(p) for p in destinations),
            "transition destinations must be exact paths, or [] for a genuine deletion",
        )
        require(
            len(set(destinations)) == len(destinations),
            "duplicate transition destination",
        )


def validate_policy(policy: object) -> dict:
    # Version 1 policies committed before transition review remain readable.
    if isinstance(policy, dict):
        policy = {"transitions": {}, **policy}
    policy = keys(
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
            "transitions",
            "remediation",
            "anti_gaming",
        },
        "root",
    )
    require(
        type(policy["version"]) is int and policy["version"] == 1, "version must be 1"
    )
    validate_baseline(policy["baseline"])
    validate_thresholds(policy["thresholds"])
    for category, field in (
        ("tests", "prefixes"),
        ("generated", "prefixes"),
        ("locks", "paths"),
        ("production", "paths"),
    ):
        validate_category_paths(policy[category], category, field)
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
        validate_exceptions(policy[section], section)
    validate_transitions(policy["transitions"])
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


def applicable_ceiling(
    path: str, old: str | None, current_category: str, policy: dict
) -> tuple[str | None, dict | None, int | None]:
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
    ceiling = None
    if record:
        ceiling = record["ceiling"]
    elif current_category in ENFORCED_CATEGORIES:
        ceiling = policy["thresholds"]["block"]
    return record_path, record, ceiling


def file_findings(row: dict, record: dict | None, policy: dict) -> tuple[list, list]:
    failures, warnings = [], []
    if row["status"] == "deleted":
        return failures, warnings
    base_category, head_category = row["base_category"], row["category"]
    base_lines, head_lines = row["base_lines"], row["head_lines"]
    if (
        base_category in ENFORCED_CATEGORIES
        and head_category not in ENFORCED_CATEGORIES
    ):
        failures.append(
            "Production moved to a non-enforced category; preserve its production classification or request an exact-path declarative allowance."
        )
    if head_category not in ENFORCED_CATEGORIES:
        return failures, warnings
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
    if head_lines > row["ceiling"] and (is_new_production or head_lines > base_lines):
        failures.append(
            "New or growing production file exceeds its applicable ceiling."
        )
    if head_category == "production" and head_lines > policy["thresholds"]["warn"]:
        warnings.append("Production size needs a responsibility and navigation review.")
    return failures, warnings


def file_status(
    path: str, old: str | None, old_entry: tuple | None, new_entry: tuple | None
) -> str:
    if not new_entry:
        return "deleted"
    if not old_entry:
        return "added"
    if path != old:
        return "renamed"
    return "unchanged" if old_entry == new_entry else "modified"


def file_report(
    path: str,
    old: str | None,
    before: dict,
    after: dict,
    counts: dict,
    policy: dict,
    base_policy: dict,
) -> dict:
    old_entry, new_entry = before.get(old), after.get(path)
    base_lines = counts.get(old_entry[1], 0) if old_entry else 0
    head_lines = counts.get(new_entry[1], 0) if new_entry else 0
    base_category = category(old, old_entry[0], base_policy) if old_entry else None
    head_category = category(path, new_entry[0], policy) if new_entry else None
    current_category = head_category or base_category
    record_path, record, ceiling = applicable_ceiling(
        path, old, current_category, policy
    )
    source_record = base_policy["ceilings"].get(old) or base_policy["allowances"].get(old)
    if path != old and not record and source_record:
        record_path, record, ceiling = old, source_record, source_record["ceiling"]
    row = {
        "path": path,
        "base_path": old,
        "status": file_status(path, old, old_entry, new_entry),
        "base_lines": base_lines,
        "head_lines": head_lines,
        "delta": head_lines - base_lines,
        "category": current_category,
        "base_category": base_category,
        "ceiling": ceiling,
        "ceiling_path": record_path,
    }
    row["failures"], row["warnings"] = file_findings(row, record, policy)
    if row["status"] == "renamed" and (source_record or record) and record_path != path:
        row["failures"].append(
            "Transfer the source's reviewed ceiling to the destination's exact path so it remains enforced after this rename."
        )
    return row


def transition_destination_error(
    path: str, source: str, before: dict, after: dict, policy: dict, base_policy: dict
) -> str | None:
    entry = after.get(path)
    old_entry = before.get(path)
    if not entry or (old_entry and entry[1] == old_entry[1]):
        return "Declare an added or modified destination with changed content in the head commit."
    if category(path, entry[0], policy) not in ENFORCED_CATEGORIES:
        return "Retain production classification or add an exact-path declarative allowance."
    source_record = base_policy["ceilings"].get(source) or base_policy[
        "allowances"
    ].get(source)
    destination_record = policy["ceilings"].get(path) or policy["allowances"].get(path)
    if source_record and not destination_record:
        return "Transfer the source's reviewed ceiling to the destination's exact path."
    previous_record = base_policy["ceilings"].get(path) or base_policy[
        "allowances"
    ].get(path)
    if (
        source_record
        and destination_record["ceiling"] > source_record["ceiling"]
        and destination_record == previous_record
    ):
        return "Review the destination's larger ceiling with an explicit policy record update for this transfer."
    return None


def review_category_moves(
    rows: list[dict],
    before: dict,
    after: dict,
    policy: dict,
    base_policy: dict,
    base: str,
) -> list[str]:
    deleted = [
        row
        for row in rows
        if row["status"] == "deleted" and row["base_category"] in ENFORCED_CATEGORIES
    ]
    if not deleted:
        return []
    candidates = sorted(
        path
        for path, entry in after.items()
        if entry != before.get(path)
        and category(path, entry[0], policy) not in ENFORCED_CATEGORIES
    )
    for row in deleted:
        record = policy["transitions"].get(row["path"])
        if not record or record["base_commit"] != base:
            if candidates:
                row["failures"].append(
                    "Possible category move: production disappeared while non-enforced paths "
                    "changed. Review category_move_candidates and add a transitions entry "
                    "for this source and comparison base, declaring destinations or a genuine deletion."
                )
            continue
        row["transition"] = record
        for destination in record["destinations"]:
            error = transition_destination_error(
                destination, row["path"], before, after, policy, base_policy
            )
            if error:
                row["failures"].append(f"{destination}: {error}")
    return candidates


def read_policy(git: Git, head_tree: dict, policy_file: Path | None) -> dict:
    size_error = "policy exceeds 1 MiB; reduce its size before running the checker."
    if policy_file:
        with policy_file.open("rb") as source:
            content = source.read(MAX_POLICY_BYTES + 1)
    else:
        entry = head_tree.get(POLICY_PATH)
        if not entry or entry[0] not in REGULAR_MODES:
            raise ValueError(
                "The committed head needs a regular maintainability-policy.json file."
            )
        # The same immutable object ID is sized and read, with replacements disabled.
        require(int(git.run("cat-file", "-s", entry[1])) <= MAX_POLICY_BYTES, size_error)
        content = git.run("cat-file", "blob", entry[1])
    require(len(content) <= MAX_POLICY_BYTES, size_error)
    try:
        return validate_policy(json.loads(content))
    except (TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid policy structure or encoding.") from exc


def comparison_result(
    report_only: bool, blocked: list, failed: bool
) -> tuple[str, int]:
    if report_only:
        return "REPORT ONLY / NOT ENFORCED", 0
    if blocked:
        return "BLOCKED", 2
    if failed:
        return "FAIL", 1
    return "PASS", 0


def run(args: argparse.Namespace) -> tuple[dict, int]:
    if args.policy_file and not args.report_only:
        raise ValueError(
            "--policy-file requires --report-only; enforcement uses the committed head policy."
        )
    git = Git(args.repo)
    base, head = git.commit(args.base), git.commit(args.head)
    before, after = git.tree(base), git.tree(head)
    policy = read_policy(git, after, args.policy_file)
    base_policy = read_policy(git, before, None) if POLICY_PATH in before else policy
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
            base_policy,
        )
        for path in sorted(after)
    ]
    rows.extend(
        file_report(path, path, before, after, counts, policy, base_policy)
        for path in sorted(before.keys() - after.keys() - set(renames.values()))
    )
    move_candidates = review_category_moves(
        rows, before, after, policy, base_policy, base
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
    result, code = comparison_result(args.report_only, blocked, failed)
    report = {
        "result": result,
        "base": base,
        "head": head,
        "policy_source": "report-only external policy"
        if args.policy_file
        else f"{head}:{POLICY_PATH}",
        "base_policy_source": f"{base}:{POLICY_PATH}"
        if POLICY_PATH in before
        else "comparison policy (base has no committed policy)",
        "category_move_candidates": move_candidates,
        "baseline": policy["baseline"],
        "thresholds": policy["thresholds"],
        "blocked": blocked,
        "files": rows,
        "remediation": policy["remediation"],
        "anti_gaming": policy["anti_gaming"],
    }
    return report, code


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
