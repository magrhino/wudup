from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from wudup import web_plan_staleness
from wudup.plan_models import (
    DryRunPlan,
    DryRunPlanIssue,
    DryRunPlanSource,
    DryRunPlanSummary,
    DryRunPlanTarget,
)


def _plan(plan_id: str = "reviewed", **changes: object) -> DryRunPlan:
    plan = DryRunPlan(
        plan_id=plan_id,
        dry_run=True,
        can_apply=True,
        status="ready",
        source_file="WUD API",
        mode="stop",
        max_wait=180,
        digest_pin_updates=False,
        selected_line_numbers=(1,),
        summary=DryRunPlanSummary(1, 1, 1, 1, 0, 0),
        source=DryRunPlanSource(
            configured="api",
            active="api",
            label="WUD API",
            source_hash="hash-a",
            source_ids_by_line={1: "container-a"},
            metadata_status_by_line={1: "fresh"},
        ),
        targets=(
            DryRunPlanTarget(
                line_no=1,
                raw="repo/app:1.0 tag=2.0",
                image="repo/app:1.0",
                resolved_image="repo/app:1.0",
                digest="",
                desired_tag="2.0",
                matched=True,
                action="update",
            ),
        ),
    )
    return replace(plan, **changes)


def _issue(message: str) -> DryRunPlanIssue:
    return DryRunPlanIssue(severity="error", code="code", message=message)


def test_missing_review_says_it_cannot_compare() -> None:
    reason = web_plan_staleness.stale_plan_reason(None, _plan())

    assert "no longer has the plan you reviewed" in reason
    assert reason.endswith("Review the plan again before applying.")


def test_source_change_names_selected_updates_wud_dropped() -> None:
    reviewed = _plan()
    current = _plan(
        "current",
        source=replace(reviewed.source, source_hash="hash-b"),
        targets=(replace(reviewed.targets[0], raw="repo/app:1.0 tag=3.0"),),
    )

    reason = web_plan_staleness.stale_plan_reason(reviewed, current)

    assert reason.startswith(
        "WUD no longer reports these selected updates: repo/app:1.0 tag=2.0."
    )


def test_recreated_container_and_metadata_freshness_are_reported() -> None:
    reviewed = _plan()
    current = _plan(
        "current",
        source=replace(
            reviewed.source,
            source_ids_by_line={1: "container-b"},
            metadata_status_by_line={1: "retained"},
        ),
    )

    reason = web_plan_staleness.stale_plan_reason(reviewed, current)

    assert "A selected container was recreated or replaced" in reason
    assert "changed freshness" in reason


def test_issue_changes_are_listed_without_doubled_periods() -> None:
    reviewed = _plan(issues=(_issue("Old problem."),))
    current = _plan(
        "current",
        issues=tuple(_issue(f"Registry lookup {index} failed.") for index in range(3)),
    )

    reason = web_plan_staleness.stale_plan_reason(reviewed, current)

    assert (
        "Rechecking found a new problem: Registry lookup 0 failed; "
        "Registry lookup 1 failed (and 1 more)." in reason
    )
    assert "A problem shown in the review no longer applies: Old problem." in reason
    assert ".." not in reason


def test_settings_change_and_unexplained_change_fallback() -> None:
    reviewed = _plan()

    settings_reason = web_plan_staleness.stale_plan_reason(
        reviewed, _plan("current", max_wait=60)
    )
    fallback_reason = web_plan_staleness.stale_plan_reason(
        reviewed, _plan("current", status="blocked")
    )

    assert settings_reason.startswith("WUDup update settings changed")
    assert fallback_reason.startswith("Some update details changed")


def test_detail_keeps_stale_prefix() -> None:
    assert web_plan_staleness.stale_plan_detail("Why.") == "plan is stale: Why."
    assert web_plan_staleness.stale_plan_detail("") == "plan is stale"


def test_reviewed_plans_are_bounded_and_most_recent_wins() -> None:
    state = SimpleNamespace()
    limit = web_plan_staleness.REVIEWED_PLAN_LIMIT
    for index in range(limit + 1):
        web_plan_staleness.remember_reviewed_plan(state, _plan(f"plan-{index}"))
    web_plan_staleness.remember_reviewed_plan(state, _plan("plan-1"))
    web_plan_staleness.remember_reviewed_plan(state, _plan("plan-new"))

    assert web_plan_staleness.reviewed_plan(state, "plan-0") is None
    assert web_plan_staleness.reviewed_plan(state, "plan-2") is None
    assert web_plan_staleness.reviewed_plan(state, "plan-1") is not None
    assert web_plan_staleness.reviewed_plan(state, "plan-new") is not None
    assert len(state.web_reviewed_plans) == limit
