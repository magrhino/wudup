from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
PRIVILEGED_WORKFLOW = ROOT / ".github/workflows/dependency-automerge.yml"
CANDIDATE_WORKFLOW = ROOT / ".github/workflows/dependency-automerge-candidate.yml"


class DependencyAutomergeWorkflowTests(unittest.TestCase):
    @staticmethod
    def _step(workflow: dict, job: str, name: str) -> dict:
        return next(s for s in workflow["jobs"][job]["steps"] if s["name"] == name)

    @staticmethod
    def _workflow(path: Path) -> tuple[dict, str]:
        text = path.read_text(encoding="utf-8")
        return YAML(typ="safe").load(text), text

    @staticmethod
    def _provenance_program(text: str) -> str:
        match = re.search(
            r'--arg bot "\$(?:BOT|bot)" \\\n\s+\'([^\']+)\'',
            text,
        )
        if match is None:
            raise AssertionError("provenance jq program not found")
        return match.group(1)

    @staticmethod
    def _verified_commit(author: str) -> dict:
        return {
            "author": {"login": author},
            "committer": {"login": "web-flow"},
            "commit": {"verification": {"verified": True, "reason": "valid"}},
        }

    def _jq_accepts(self, program: str, pages: list[list[dict]]) -> bool:
        result = subprocess.run(
            ["jq", "-e", "--arg", "bot", "dependabot[bot]", program],
            input=json.dumps(pages),
            capture_output=True,
            check=False,
            text=True,
        )
        return result.returncode == 0

    def test_privileged_workflow_uses_verified_atomic_merge(self) -> None:
        workflow, text = self._workflow(PRIVILEGED_WORKFLOW)

        self.assertIn("workflow_run", workflow["on"])
        self.assertNotIn("pull_request_target", workflow["on"])
        self.assertEqual(workflow["permissions"], {})
        self.assertEqual(
            workflow["jobs"]["merge-candidate"]["permissions"],
            {
                "actions": "read",
                "contents": "read",
                "pull-requests": "read",
            },
        )
        self.assertNotIn("permissions", workflow["jobs"]["rebase-dependabot"])
        self.assertNotIn("actions/checkout@", text)
        self.assertNotIn("gh pr review", text)
        self.assertNotIn("--auto", text)
        self.assertIn('"repos/$GH_REPO/pulls/$PR_NUMBER/merge"', text)
        self.assertIn('-f sha="$head_sha"', text)
        self.assertIn('gh pr checks "$PR_NUMBER"', text)

        program = self._provenance_program(text)
        valid = self._verified_commit("dependabot[bot]")
        self.assertTrue(self._jq_accepts(program, [[valid]]))
        self.assertFalse(self._jq_accepts(program, [[valid, valid]]))

    def test_candidate_workflow_is_read_only_and_verifies_bots(self) -> None:
        workflow, text = self._workflow(CANDIDATE_WORKFLOW)

        self.assertIn("pull_request_target", workflow["on"])
        self.assertTrue(
            all(permission == "read" for permission in workflow["permissions"].values())
        )
        self.assertNotIn("actions/checkout@", text)
        self.assertNotIn("secrets.", text)
        self.assertIn("dependabot/fetch-metadata@", text)
        self.assertIn("[.[][]] |", text)
        self.assertIn("length == 1", text)
        self.assertIn(".author.login == $bot", text)
        self.assertIn('.committer.login == "web-flow"', text)
        self.assertIn(".commit.verification.verified == true", text)
        self.assertIn('.commit.verification.reason == "valid"', text)
        self.assertIn(".actor.login // empty", text)

        privileged_text = PRIVILEGED_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("length == 1", privileged_text)
        self.assertIn(".author.login == $bot", privileged_text)
        self.assertIn('.committer.login == "web-flow"', privileged_text)

        program = self._provenance_program(text)
        valid = self._verified_commit("dependabot[bot]")
        collaborator = self._verified_commit("maintainer")
        self.assertTrue(self._jq_accepts(program, [[valid]]))
        self.assertFalse(self._jq_accepts(program, [[valid, collaborator]]))

    def test_renovate_marks_only_non_major_updates(self) -> None:
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        rule = config["packageRules"][0]

        self.assertEqual(
            rule["matchUpdateTypes"], ["minor", "patch", "pin", "digest"]
        )
        self.assertEqual(rule["addLabels"], ["automerge"])
        self.assertNotIn("automerge", rule)

    def test_renovate_holds_trivy_versions_but_allows_digest_updates(self) -> None:
        config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        rule = config["packageRules"][1]

        self.assertEqual(rule["matchDepNames"], ["aquasec/trivy"])
        self.assertEqual(rule["matchUpdateTypes"], ["major", "minor", "patch"])
        self.assertFalse(rule["enabled"])

    def test_dependency_review_checks_all_scopes_and_missing_licenses(self) -> None:
        workflow, _ = self._workflow(ROOT / ".github/workflows/security.yml")
        options = self._step(
            workflow, "dependency-review", "Check dependency changes"
        )["with"]
        self.assertEqual(options["fail-on-severity"], "high")
        self.assertEqual(options["fail-on-scopes"], "runtime, development, unknown")
        self.assertTrue(options["license-check"])
        self.assertIn("MIT", options["allow-licenses"].split(", "))
        self.assertNotIn("allow-ghsas", options)
        self.assertEqual(
            options["allow-dependencies-licenses"].split(", "),
            ["pkg:pypi/certifi@2026.7.22", "pkg:pypi/typing-extensions@4.16.0"],
        )
        program = self._step(
            workflow, "dependency-review", "Require complete license evidence"
        )["run"]
        clean = {"unlicensed": [], "unresolved": [], "forbidden": []}
        cases = [(json.dumps(clean), True), ("", False), ("{}", False)]
        for key in clean:
            cases.append((json.dumps({**clean, key: [{"name": "example"}]}), False))
            cases.append((json.dumps({**clean, key: None}), False))
        for payload, allowed in cases:
            with self.subTest(payload=payload):
                result = subprocess.run(
                    ["bash", "-c", program],
                    env={**os.environ, "LICENSE_RESULTS": payload, "DEPENDENCY_CHANGES": "[]"},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, allowed, result.stderr)

    def test_license_decisions_are_limited_to_reviewed_usage(self) -> None:
        workflow, _ = self._workflow(ROOT / ".github/workflows/security.yml")
        program = self._step(
            workflow, "dependency-review", "Require complete license evidence"
        )["run"]
        for package, license_expression in (
            ("pkg:pypi/certifi@2026.7.22", "MPL-2.0"),
            ("pkg:pypi/typing-extensions@4.16.0", "PSF-2.0"),
            ("pkg:pypi/typing-extensions@4.16.0", (
                "Python-2.0 AND GPL-1.0-or-later AND Python-2.0 AND BSD-3-Clause "
                "AND Python-2.0 AND BSD-3-Clause AND 0BSD"
            )),
        ):
            reviewed = {
                "change_type": "added", "package_url": package,
                "manifest": "requirements-dev.txt", "scope": "development",
                "license": license_expression,
            }
            cases = [(json.dumps([reviewed]), True), ("", False), ("{}", False)]
            for field, value in (
                ("manifest", "requirements.txt"), ("scope", "runtime"),
                ("scope", "unknown"), ("license", None), ("license", "AGPL-3.0"),
            ):
                cases.append((json.dumps([{**reviewed, field: value}]), False))
            cases.append((json.dumps([{
                **reviewed, "change_type": "removed", "scope": "runtime",
            }]), True))
            for changes, allowed in cases:
                with self.subTest(package=package, changes=changes):
                    result = subprocess.run(
                        ["bash", "-c", program],
                        env={**os.environ, "DEPENDENCY_CHANGES": changes,
                             "LICENSE_RESULTS": json.dumps({
                                 "unlicensed": [], "unresolved": [], "forbidden": [],
                             })},
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode == 0, allowed, result.stderr)

    def test_dependency_evidence_survives_failed_checks(self) -> None:
        workflow, _ = self._workflow(ROOT / ".github/workflows/security.yml")
        job = workflow["jobs"]["dependency-review"]
        self.assertFalse(job.get("continue-on-error", False))
        for step in job["steps"]:
            self.assertFalse(step.get("continue-on-error", False))
        validate, record, upload = [self._step(workflow, "dependency-review", name) for name in (
            "Require complete license evidence", "Record dependency review evidence",
            "Retain dependency review evidence",
        )]
        for step in (validate, record, upload):
            self.assertEqual(step["if"], "${{ always() }}")
        self.assertEqual(record["env"]["REVIEW_OUTCOME"], "${{ steps.dependencies.outcome }}")
        self.assertEqual(
            record["env"]["LICENSE_OUTCOME"],
            "${{ steps." + validate["id"] + ".outcome }}",
        )
        self.assertEqual(upload["with"]["path"], "${{ runner.temp }}/dependency-review.json")
        clean = json.dumps({"unlicensed": [], "unresolved": [], "forbidden": []})
        cases = (
            ("clean", "success", "[]", clean, True),
            ("vulnerability", "failure", '[{"vulnerabilities": [{"severity": "high"}]}]', clean, True),
            ("license", "failure", "[]", json.dumps({
                "unlicensed": [], "unresolved": [], "forbidden": [{"license": "AGPL-3.0"}],
            }), False),
            ("exception", "success", json.dumps([{
                "change_type": "added", "package_url": "pkg:pypi/certifi@2026.7.22",
                "manifest": "requirements.txt", "scope": "runtime", "license": "MPL-2.0",
            }]), clean, False),
            ("missing", "failure", "", "", False),
            ("malformed", "failure", "not-json", "{", False),
        )
        for name, outcome, changes, licenses, valid in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                env = {
                    **os.environ, "DEPENDENCY_CHANGES": changes, "LICENSE_RESULTS": licenses,
                    "BASE_SHA": "b" * 40, "HEAD_SHA": "a" * 40, "RUNNER_TEMP": temp,
                    "REVIEW_OUTCOME": outcome,
                }
                result = subprocess.run(
                    ["bash", "-c", validate["run"]], env=env,
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode == 0, valid, result.stderr)
                env["LICENSE_OUTCOME"] = "success" if valid else "failure"
                subprocess.run(
                    ["bash", "-c", record["run"]], env=env,
                    capture_output=True, text=True, check=True,
                )
                evidence = json.loads((Path(temp) / "dependency-review.json").read_text())
                self.assertEqual(evidence["base"], env["BASE_SHA"])
                self.assertEqual(evidence["head"], env["HEAD_SHA"])
                self.assertEqual(evidence["review_outcome"], outcome)
                self.assertEqual(evidence["license_outcome"], env["LICENSE_OUTCOME"])
                for key, raw in (("changes", changes), ("license_results", licenses)):
                    expected = ({"unavailable": True, "raw": raw}
                                if name in ("missing", "malformed") else json.loads(raw))
                    self.assertEqual(evidence[key], expected)

    def test_merge_waits_for_current_complete_workflows(self) -> None:
        workflow, _ = self._workflow(PRIVILEGED_WORKFLOW)
        program = self._step(
            workflow, "merge-candidate",
            "Merge the verified candidate when required checks pass",
        )["run"]
        fake_gh = r'''
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
scenario = os.environ["SCENARIO"]
sha = "a" * 40
root = Path(os.environ["RUNNER_TEMP"])
pr = {"state": "open", "draft": False, "base": {"ref": "main"},
      "head": {"sha": sha, "repo": {"full_name": "example/repo"}},
      "user": {"login": "dependabot[bot]"}, "mergeable_state": "clean"}
if scenario == "behind":
    pr["mergeable_state"] = "behind"
if scenario == "changed" and (root / "checked").exists():
    pr["head"]["sha"] = "b" * 40
if scenario == "retargeted" and (root / "checked").exists():
    pr["base"]["ref"] = "other"
if args[:2] == ["pr", "checks"]:
    assert "--required" in args and "--watch" in args and "--fail-fast" in args
    (root / "checked").touch()
    sys.exit(1 if scenario == "required-failed" else 0)
if args[:2] == ["run", "download"]:
    target = Path(args[args.index("--dir") + 1])
    (target / "candidate.json").write_text(json.dumps({
        "schema": 1, "eligible": True, "repository": "example/repo",
        "bot": "dependabot[bot]", "head_sha": sha, "pr_number": 1}))
    sys.exit(0)
path = next(a for a in args if a.startswith("repos/"))
if path.endswith("/merge"):
    assert "sha=" + sha in args
    (root / "merged").touch()
    data = {"merged": True}
elif path.endswith("/pulls/1"):
    data = pr
elif "/commits?" in path:
    data = [[{"author": {"login": "dependabot[bot]"},
              "committer": {"login": "web-flow"},
              "commit": {"verification": {"verified": True, "reason": "valid"}}}]]
elif "/artifacts?" in path:
    print("99")
    sys.exit(0)
elif path.endswith("dependency-automerge-candidate.yml"):
    print("10")
    sys.exit(0)
elif path.endswith("/runs/99"):
    data = {"workflow_id": 10, "name": "dependency auto-merge candidate",
            "event": "pull_request_target", "conclusion": "success"}
elif "/workflows/" in path and "/runs?" in path:
    assert "head_sha=" + sha in path and "event=pull_request" in path
    name = path.split("/workflows/")[1].split("/")[0]
    run = {"id": {"ci.yml": 1, "security.yml": 2, "codeql.yml": 3}[name],
           "head_sha": sha, "event": "pull_request", "status": "completed",
           "conclusion": "success", "pull_requests": [{"number": 1}]}
    if name == "ci.yml":
        if scenario in ("failure", "cancelled", "skipped", "timed_out"):
            run["conclusion"] = scenario
        if scenario == "pending":
            run.update(status="in_progress", conclusion=None)
        if scenario == "stale":
            run["head_sha"] = "b" * 40
        if scenario == "wrong-pr":
            run["pull_requests"] = [{"number": 2}]
    if name == "security.yml" and scenario == "security-failure":
        run["conclusion"] = "failure"
    if name == "codeql.yml" and scenario == "codeql-pending":
        run.update(status="in_progress", conclusion=None)
    data = {"workflow_runs": [] if scenario == "missing-run" else [run]}
    if scenario == "newer-failure":
        data["workflow_runs"] = [{**run, "conclusion": "failure"}, run]
elif "/jobs?" in path:
    names = {"1": ["python tests", "shell tests", "webui checks"],
             "2": ["dependency review", "workflow security"],
             "3": ["Analyze (actions)", "Analyze (python)",
                   "Analyze (javascript-typescript)"]}
    run_id = path.split("/runs/")[1].split("/")[0]
    jobs = [{"name": n, "status": "completed", "conclusion": "success"}
            for n in names[run_id]]
    jobs.append({"name": "optional", "status": "completed", "conclusion": "skipped"})
    for job_id, job in enumerate(jobs):
        job["id"] = job_id
    if scenario in ("retried-pass", "retried-failure"):
        jobs.append({**jobs[0], "id": 100,
                     "conclusion": "success" if scenario == "retried-pass" else "failure"})
    if scenario == "skipped-core" or scenario == "skipped-core-" + run_id:
        jobs[0]["conclusion"] = "skipped"
    if scenario == "missing-core":
        jobs.pop(0)
    if scenario == "api-error":
        sys.exit(1)
    data = [{"jobs": jobs}]
else:
    raise AssertionError(args)
print(json.dumps(data))
'''
        cases = [
            "green", "failure", "cancelled", "skipped", "timed_out", "pending",
            "stale", "wrong-pr", "missing-run", "newer-failure", "skipped-core",
            "missing-core", "required-failed", "behind", "changed", "api-error",
            "retargeted", "security-failure", "codeql-pending",
            "skipped-core-2", "skipped-core-3",
            "retried-pass", "retried-failure",
        ]
        for scenario in cases:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                gh = root / "gh"
                gh.write_text(f"#!{sys.executable}\n" + fake_gh, encoding="utf-8")
                gh.chmod(0o755)
                result = subprocess.run(
                    ["bash", "-c", program],
                    env={**os.environ, "PATH": tmp + os.pathsep + os.environ["PATH"],
                         "GH_REPO": "example/repo", "PR_NUMBER": "1",
                         "RUNNER_TEMP": tmp, "SCENARIO": scenario,
                         "MERGE_TOKEN": "example-only"},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    (root / "merged").exists(),
                    scenario in ("green", "retried-pass"), result.stderr,
                )
                self.assertEqual(result.returncode, 1 if scenario == "api-error" else 0,
                                 result.stderr)


if __name__ == "__main__":
    unittest.main()
