from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_maintainability.py"
POLICY_PATH = "maintainability-policy.json"


def exception(ceiling: int) -> dict:
    return {
        "ceiling": ceiling,
        "reason": "One cohesive synthetic responsibility; extraction is deferred.",
        "deferred": True,
        "follow_up": "https://github.com/example/project/issues/1",
        "re_review": "When another responsibility is added.",
    }


class Repository:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q", "--template=")
        initial = self.commit()
        self.policy = json.loads((ROOT / POLICY_PATH).read_text())
        self.policy["baseline"].update(
            status="ready", commit=initial, reason="Synthetic fixture baseline only."
        )
        self.save_policy()
        self.base = self.commit()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.path),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            check=True,
            capture_output=True,
        )
        return result.stdout.decode().strip()

    def commit(self) -> str:
        self.git("add", "--all")
        self.git("commit", "-qm", "test: synthetic fixture", "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def write(self, path: str, content: bytes) -> None:
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def lines(self, path: str, count: int) -> None:
        self.write(
            path, b"".join(f"# line {index}\n".encode() for index in range(count))
        )

    def save_policy(self) -> None:
        self.write(POLICY_PATH, json.dumps(self.policy).encode())

    def check(
        self, *extra: str, base: str | None = None, head: str = "HEAD"
    ) -> tuple[int, dict]:
        result = subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--repo",
                str(self.path),
                "--base",
                base or self.base,
                f"--head={head}",
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "Traceback" not in result.stderr
        return result.returncode, json.loads(result.stdout or result.stderr)


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository(tmp_path)


def row(report: dict, path: str) -> dict:
    return next(item for item in report["files"] if item["path"] == path)


@pytest.mark.parametrize(
    "content,count",
    [
        (b"", 0),
        (b"one", 1),
        (b"one\n", 1),
        (b"one\ntwo", 2),
        (b"one\r\ntwo\r\n", 2),
        ("one\u2028two".encode(), 1),
    ],
)
def test_physical_lf_count_including_nonfinal_newline(repo, content, count):
    repo.write("source.py", content)
    head = repo.commit()
    code, report = repo.check()
    assert code == 0
    assert report["base"] == repo.base and report["head"] == head
    assert row(report, "source.py") == {
        "path": "source.py",
        "base_path": None,
        "status": "added",
        "base_lines": 0,
        "head_lines": count,
        "delta": count,
        "category": "production",
        "base_category": None,
        "ceiling": 800,
        "ceiling_path": None,
        "failures": [],
        "warnings": [],
    }


@pytest.mark.parametrize(
    "count,code,warns",
    [(500, 0, False), (501, 0, True), (800, 0, True), (801, 1, True)],
)
def test_new_production_threshold_boundaries(repo, count, code, warns):
    repo.lines("source.py", count)
    repo.commit()
    actual, report = repo.check()
    assert actual == code
    assert bool(row(report, "source.py")["warnings"]) is warns
    assert bool(row(report, "source.py")["failures"]) is bool(code)
    if code:
        assert "Danger acknowledgement cannot bypass" in report["remediation"]
        assert len(report["anti_gaming"]) == 6


@pytest.mark.parametrize("count,code", [(900, 0), (850, 0), (950, 0), (951, 1)])
def test_existing_ceiling_allows_unchanged_shrinking_and_growth_within_ceiling(
    repo, count, code
):
    repo.lines("source.py", 900)
    repo.policy["ceilings"]["source.py"] = exception(950)
    repo.save_policy()
    repo.base = repo.commit()
    repo.lines("source.py", count)
    repo.commit()
    actual, report = repo.check()
    assert actual == code
    source = row(report, "source.py")
    assert source["base_lines"] == 900
    assert source["delta"] == count - 900
    assert source["ceiling"] == 950
    assert source["status"] == ("unchanged" if count == 900 else "modified")


def test_ready_policy_cannot_silently_grandfather_missing_ceiling(repo):
    repo.lines("source.py", 900)
    repo.base = repo.commit()
    code, report = repo.check()
    assert code == 1
    assert "no reviewed ceiling" in row(report, "source.py")["failures"][0]


@pytest.mark.parametrize("before,after,code", [(799, 801, 1), (900, 799, 0)])
def test_threshold_crossing_and_shrinking_below_threshold(repo, before, after, code):
    repo.lines("source.py", before)
    repo.base = repo.commit()
    repo.lines("source.py", after)
    repo.commit()
    assert repo.check()[0] == code


@pytest.mark.parametrize("count,code", [(900, 0), (901, 1)])
def test_git_rename_preserves_old_path_ceiling_including_growth(repo, count, code):
    repo.lines("old.py", 900)
    repo.policy["ceilings"]["old.py"] = exception(900)
    repo.save_policy()
    repo.base = repo.commit()
    (repo.path / "old.py").rename(repo.path / "new.py")
    repo.lines("new.py", count)
    repo.commit()
    actual, report = repo.check()
    assert actual == code
    source = row(report, "new.py")
    assert source["status"] == "renamed"
    assert source["base_path"] == source["ceiling_path"] == "old.py"
    assert source["base_lines"] == source["ceiling"] == 900
    assert source["delta"] == count - 900
    assert not any(item["path"] == "old.py" for item in report["files"])


@pytest.mark.parametrize(
    "target,category",
    [
        ("tests/moved.py", "tests"),
        ("src/wudup/web_static/moved.py", "generated"),
        ("requirements.txt", "locks"),
        ("README.md", "other"),
    ],
)
def test_production_category_moves_cannot_silently_bypass(repo, target, category):
    repo.lines("source.py", 900)
    repo.policy["ceilings"]["source.py"] = exception(900)
    repo.save_policy()
    repo.base = repo.commit()
    (repo.path / target).parent.mkdir(parents=True, exist_ok=True)
    (repo.path / "source.py").rename(repo.path / target)
    repo.commit()
    code, report = repo.check()
    assert code == 1
    source = row(report, target)
    assert source["category"] == category and source["base_category"] == "production"
    assert "non-enforced category" in source["failures"][0]


def test_tests_moved_to_production_are_new_production(repo):
    repo.lines("tests/source.py", 900)
    repo.base = repo.commit()
    (repo.path / "tests/source.py").rename(repo.path / "source.py")
    repo.commit()
    code, report = repo.check()
    assert code == 1
    assert "exceeds" in row(report, "source.py")["failures"][0]


def test_deleting_oversized_file_needs_no_exception(repo):
    repo.lines("source.py", 900)
    repo.base = repo.commit()
    (repo.path / "source.py").unlink()
    repo.commit()
    code, report = repo.check()
    assert code == 0
    assert row(report, "source.py")["status"] == "deleted"
    assert row(report, "source.py")["delta"] == -900


@pytest.mark.parametrize(
    "path,category",
    [
        ("tests/large.py", "tests"),
        ("webui/tests/large.test.ts", "tests"),
        ("src/wudup/web_static/assets/large.js", "generated"),
        ("webui/package-lock.json", "locks"),
        ("requirements-dev.txt", "locks"),
    ],
)
def test_explicit_non_enforced_categories_stay_visible(repo, path, category):
    repo.lines(path, 1000)
    repo.commit()
    code, report = repo.check()
    assert code == 0
    source = row(report, path)
    assert source["category"] == category and source["head_lines"] == 1000
    assert source["ceiling"] is None and source["failures"] == []


@pytest.mark.parametrize(
    "section,category", [("ceilings", "production"), ("allowances", "declarative")]
)
def test_reviewed_exact_path_records_allow_size_but_not_further_growth(
    repo, section, category
):
    repo.policy[section]["schema.py"] = exception(950)
    repo.save_policy()
    repo.lines("schema.py", 950)
    repo.commit()
    code, report = repo.check()
    assert code == 0
    assert row(report, "schema.py")["category"] == category
    repo.lines("schema.py", 951)
    repo.commit()
    assert repo.check()[0] == 1
    repo.lines("another.py", 951)
    repo.commit()
    assert row(repo.check()[1], "another.py")["ceiling"] == 800


def test_worktree_policy_edits_and_untracked_source_are_ignored(repo):
    repo.lines("source.py", 800)
    head = repo.commit()
    repo.lines("source.py", 1000)
    repo.lines("untracked.py", 1000)
    repo.write(POLICY_PATH, b"not JSON")
    code, report = repo.check()
    assert code == 0 and report["head"] == head
    assert report["policy_source"] == f"{head}:{POLICY_PATH}"
    assert row(report, "source.py")["head_lines"] == 800
    assert not any(item["path"] == "untracked.py" for item in report["files"])


def test_committed_source_is_never_executed_and_odd_paths_are_literal(repo):
    path = "--odd\tname\n$(touch marker).py"
    repo.write(path, b"raise RuntimeError('never execute')\n")
    repo.commit()
    code, report = repo.check()
    assert code == 0 and row(report, path)["head_lines"] == 1
    assert not (repo.path / "marker").exists()


def test_symlink_is_not_followed(repo, tmp_path):
    target = tmp_path.parent / f"{tmp_path.name}-outside.py"
    target.write_text("outside\n" * 1000)
    (repo.path / "source.py").symlink_to(target)
    repo.commit()
    code, report = repo.check()
    assert code == 0
    assert row(report, "source.py")["category"] == "symlink"
    assert row(report, "source.py")["head_lines"] == 0


def test_pending_baseline_blocks_enforcement_and_report_only_never_claims_pass(repo):
    repo.policy["baseline"].update(status="pending", commit=None)
    repo.save_policy()
    repo.commit()
    code, report = repo.check()
    assert code == 2 and report["result"] == "BLOCKED"
    code, report = repo.check("--report-only")
    assert code == 0 and report["result"] == "REPORT ONLY / NOT ENFORCED"
    assert "Baseline pending" in report["blocked"][0]


def test_external_policy_is_only_allowed_for_explicit_report_only(repo):
    external = repo.path / "external-policy.json"
    external.write_text(json.dumps(repo.policy))
    code, report = repo.check("--policy-file", str(external))
    assert code == 2 and "requires --report-only" in report["error"]
    historical = repo.policy["baseline"]["commit"]
    code, report = repo.check(
        "--report-only",
        "--policy-file",
        str(external),
        base=historical,
        head=historical,
    )
    assert code == 0 and report["result"] == "REPORT ONLY / NOT ENFORCED"
    assert report["policy_source"] == "report-only external policy"


def test_missing_committed_policy_is_an_error(repo):
    (repo.path / POLICY_PATH).unlink()
    repo.commit()
    code, report = repo.check()
    assert code == 2 and "committed head" in report["error"]


@pytest.mark.parametrize(
    "change",
    [
        {"reason": " "},
        {"re_review": ""},
        {"follow_up": None},
        {"ceiling": True},
        {"ceiling": -1},
        {"deferred": "yes"},
    ],
)
def test_malformed_exception_metadata_fails_closed(repo, change):
    record = exception(900)
    record.update(change)
    repo.policy["ceilings"]["source.py"] = record
    repo.save_policy()
    repo.commit()
    code, report = repo.check()
    assert code == 2 and "Invalid policy" in report["error"]


@pytest.mark.parametrize(
    "path",
    ["/source.py", "../source.py", "src/*.py", "src/../source.py"],
)
def test_exceptions_must_be_exact_production_paths(repo, path):
    repo.policy["ceilings"][path] = exception(900)
    repo.save_policy()
    repo.commit()
    assert repo.check()[0] == 2


def test_reviewed_move_allowance_remains_enforced_inside_test_directory(repo):
    repo.lines("source.py", 900)
    repo.policy["ceilings"]["source.py"] = exception(900)
    repo.save_policy()
    repo.base = repo.commit()
    (repo.path / "tests").mkdir()
    (repo.path / "source.py").rename(repo.path / "tests/source.py")
    repo.policy["allowances"]["tests/source.py"] = exception(900)
    repo.save_policy()
    repo.commit()
    code, report = repo.check()
    assert code == 0
    assert row(report, "tests/source.py")["category"] == "declarative"
    repo.base = repo.commit()
    repo.lines("tests/source.py", 901)
    repo.commit()
    assert repo.check()[0] == 1


def test_git_repository_environment_cannot_redirect_explicit_repo(repo, monkeypatch):
    repo.lines("source.py", 1)
    repo.commit()
    monkeypatch.setenv("GIT_DIR", str(repo.path / "missing-git-dir"))
    code, report = repo.check()
    assert code == 0 and row(report, "source.py")["head_lines"] == 1


def test_diff_drivers_are_never_executed(repo):
    repo.write(".gitattributes", b"*.py diff=unsafe\n")
    repo.write("driver.py", b"from pathlib import Path\nPath('driver-ran').touch()\n")
    repo.git("config", "diff.unsafe.command", f"{sys.executable} driver.py")
    repo.git("config", "diff.unsafe.textconv", f"{sys.executable} driver.py")
    repo.lines("source.py", 10)
    repo.base = repo.commit()
    (repo.path / "source.py").rename(repo.path / "moved.py")
    repo.lines("moved.py", 11)
    repo.commit()
    code, report = repo.check()
    assert code == 0 and row(report, "moved.py")["status"] == "renamed"
    assert not (repo.path / "driver-ran").exists()


@pytest.mark.parametrize("invalid", [[], None, {"version": 1}])
def test_malformed_policy_root_fails_without_traceback(repo, invalid):
    repo.write(POLICY_PATH, json.dumps(invalid).encode())
    repo.commit()
    assert repo.check()[0] == 2


def test_thresholds_come_from_the_policy(repo):
    repo.policy["thresholds"] = {"warn": 4, "block": 8}
    repo.save_policy()
    repo.lines("source.py", 9)
    repo.commit()
    code, report = repo.check()
    assert code == 1 and row(report, "source.py")["ceiling"] == 8


def test_non_deferred_record_may_have_no_followup(repo):
    record = exception(900)
    record.update(deferred=False, follow_up=None)
    repo.policy["allowances"]["schema.py"] = record
    repo.save_policy()
    repo.lines("schema.py", 900)
    repo.commit()
    assert repo.check()[0] == 0


def test_baseline_must_be_ancestor_of_requested_base(repo):
    later = repo.commit()
    repo.policy["baseline"]["commit"] = later
    repo.save_policy()
    repo.commit()
    code, report = repo.check()
    assert code == 2 and "merge-base failed" in report["error"]


def test_option_like_ref_does_not_become_git_option(repo):
    code, report = repo.check(head="--help")
    assert code == 2 and "rev-parse failed" in report["error"]


def test_draft_policy_has_no_initial_ceilings_or_integration_baseline():
    policy = json.loads((ROOT / POLICY_PATH).read_text())
    assert policy["baseline"]["status"] == "pending"
    assert policy["baseline"]["commit"] is None
    assert policy["ceilings"] == policy["allowances"] == {}
    assert policy["thresholds"] == {"warn": 500, "block": 800}
    assert "not an integrated baseline" in policy["baseline"]["reason"]


def test_malformed_thresholds_and_unknown_fields_fail_closed(repo):
    valid = copy.deepcopy(repo.policy)
    for update in ({"thresholds": {"warn": 9, "block": 8}}, {"acknowledged": True}):
        repo.policy = {**valid, **update}
        repo.save_policy()
        repo.commit()
        assert repo.check()[0] == 2
