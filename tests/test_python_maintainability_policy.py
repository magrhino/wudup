"""Policy records survive renames, and policy reads have a fixed memory bound."""

from __future__ import annotations

import json
import tracemalloc

import pytest
from scripts import check_maintainability as checker
from tests.test_python_maintainability import POLICY_PATH, Repository, exception, row


@pytest.mark.parametrize("section", ["ceilings", "allowances"])
@pytest.mark.parametrize("keep_old_record", [False, True])
@pytest.mark.parametrize("ceiling", [700, 950])
def test_rename_requires_destination_record(
    tmp_path, section, keep_old_record, ceiling
):
    repo = Repository(tmp_path)
    repo.lines("old.py", min(900, ceiling))
    repo.policy[section]["old.py"] = exception(ceiling)
    repo.save_policy()
    repo.base = repo.commit()
    (repo.path / "old.py").rename(repo.path / "new.py")
    if not keep_old_record:
        repo.policy[section].pop("old.py")
        repo.save_policy()
    repo.commit()

    code, report = repo.check()
    assert code == 1
    renamed = row(report, "new.py")
    assert renamed["status"] == "renamed"
    assert renamed["ceiling_path"] == "old.py"
    assert renamed["ceiling"] == ceiling
    assert any("destination's exact path" in error for error in renamed["failures"])


@pytest.mark.parametrize("section", ["ceilings", "allowances"])
@pytest.mark.parametrize("ceiling", [700, 950])
def test_transferred_rename_record_still_applies_in_later_comparisons(
    tmp_path, section, ceiling
):
    repo = Repository(tmp_path)
    repo.lines("old.py", min(900, ceiling))
    repo.policy[section]["old.py"] = exception(ceiling)
    repo.save_policy()
    repo.base = repo.commit()
    (repo.path / "old.py").rename(repo.path / "new.py")
    repo.policy[section]["new.py"] = repo.policy[section].pop("old.py")
    repo.save_policy()
    renamed_commit = repo.commit()
    assert repo.check()[0] == 0

    repo.base = renamed_commit
    repo.lines("unrelated.py", 1)
    repo.commit()
    code, report = repo.check()
    assert code == 0
    unchanged = row(report, "new.py")
    assert unchanged["status"] == "unchanged"
    assert unchanged["ceiling_path"] == "new.py"
    assert unchanged["ceiling"] == ceiling

    repo.lines("new.py", ceiling + 1)
    repo.commit()
    code, report = repo.check()
    assert code == 1
    assert "exceeds its applicable ceiling" in row(report, "new.py")["failures"][0]


@pytest.mark.parametrize("location", ["head", "base", "external"])
@pytest.mark.parametrize("extra_byte", [0, 1])
def test_policy_size_limit_includes_boundary(tmp_path, location, extra_byte):
    repo = Repository(tmp_path)
    content = json.dumps(repo.policy).encode()
    content = content.ljust(1024 * 1024 + extra_byte, b" ")
    extra = ()
    if location == "external":
        path = tmp_path / "external-policy.json"
        path.write_bytes(content)
        extra = ("--report-only", "--policy-file", str(path))
    else:
        repo.write(POLICY_PATH, content)
        committed = repo.commit()
        if location == "base":
            repo.base = committed
            repo.save_policy()
            repo.commit()

    code, report = repo.check(*extra)
    assert code == (2 if extra_byte else 0)
    if extra_byte:
        assert report["result"] == "ERROR"
        assert "policy exceeds 1 MiB" in report["error"]


@pytest.mark.parametrize("external", [False, True])
def test_oversized_policy_is_rejected_without_buffering_it(
    tmp_path, external
):
    repo = Repository(tmp_path)
    repo.policy["remediation"] = "x" * (8 * 1024 * 1024)
    repo.save_policy()
    head = repo.commit()
    git = checker.Git(tmp_path)
    tree = git.tree(head)
    path = tmp_path / POLICY_PATH if external else None
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="policy exceeds 1 MiB"):
            checker.read_policy(git, tree, path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Permit the bounded external read and interpreter/process bookkeeping.
    assert peak < 3 * 1024 * 1024


def test_policy_parser_recursion_returns_structured_cli_error(
    tmp_path, monkeypatch, capsys
):
    repo = Repository(tmp_path)

    def fail_decode(_content):
        raise RecursionError("JSON decoder nesting limit")

    # Decoder nesting limits vary across supported Python versions.
    with monkeypatch.context() as patch:
        patch.setattr(checker.json, "loads", fail_decode)
        patch.setattr(
            checker.sys,
            "argv",
            [
                str(checker.__file__),
                "--repo", str(tmp_path),
                "--base", repo.base,
                "--head=HEAD",
            ],
        )
        assert checker.main() == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "result": "ERROR",
        "error": "Invalid policy structure or encoding.",
    }


@pytest.mark.parametrize(
    "content",
    [b'{"x":' * 10000 + b"0" + b"}" * 10000, b"[" * 100000 + b"]" * 100000],
)
def test_deeply_nested_policy_is_a_structured_error(tmp_path, content):
    repo = Repository(tmp_path)
    repo.write(POLICY_PATH, content)
    repo.commit()
    code, report = repo.check()
    assert code == 2
    assert report["result"] == "ERROR"
