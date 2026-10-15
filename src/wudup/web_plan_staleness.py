"""Explain why a reviewed WebUI plan no longer matches its apply-time rebuild."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import asdict
from threading import Lock
from typing import Any

from .plans import DryRunPlan

STALE_PLAN_DETAIL = "plan is stale"
# Plans kept so an apply-time mismatch can be explained. Plan IDs are content
# hashes, so a hit is the exact plan the operator reviewed.
REVIEWED_PLAN_LIMIT = 8
_MAX_LISTED = 2
_MAX_MESSAGE_CHARS = 200
_STATE_LOCK = Lock()


def stale_plan_detail(reason: str) -> str:
    return f"{STALE_PLAN_DETAIL}: {reason}" if reason else STALE_PLAN_DETAIL


def remember_reviewed_plan(state: Any, plan: DryRunPlan) -> None:
    plans, lock = _reviewed_plans(state)
    with lock:
        plans[plan.plan_id] = plan
        plans.move_to_end(plan.plan_id)
        while len(plans) > REVIEWED_PLAN_LIMIT:
            plans.popitem(last=False)


def reviewed_plan(state: Any, plan_id: str) -> DryRunPlan | None:
    plans, lock = _reviewed_plans(state)
    with lock:
        return plans.get(plan_id)


def _reviewed_plans(state: Any) -> tuple[OrderedDict[str, DryRunPlan], Lock]:
    with _STATE_LOCK:
        if not hasattr(state, "web_reviewed_plans"):
            state.web_reviewed_plans = OrderedDict()
            state.web_reviewed_plans_lock = Lock()
        return state.web_reviewed_plans, state.web_reviewed_plans_lock


def stale_plan_reason(reviewed: DryRunPlan | None, current: DryRunPlan) -> str:
    """Return a plain-language reason the rebuilt plan differs from the review."""

    if reviewed is None:
        return (
            "WUDup no longer has the plan you reviewed, so it cannot confirm "
            "nothing changed (WUDup restarted or the review is too old). "
            "Review the plan again before applying."
        )
    reasons = [
        *_settings_reasons(reviewed, current),
        *_source_reasons(reviewed, current),
        *_issue_reasons(reviewed, current),
    ]
    # A stack difference is usually the effect of a reason above; name it only
    # when nothing more specific explains the mismatch.
    if not reasons:
        reasons.extend(_stack_reasons(reviewed, current))
    if not reasons:
        reasons.append(
            "Some update details changed when WUDup rechecked Docker, Compose, "
            "and the registries."
        )
    return " ".join([*reasons[:3], "Review the plan again before applying."])


def _settings_reasons(reviewed: DryRunPlan, current: DryRunPlan) -> list[str]:
    if (
        reviewed.mode,
        reviewed.max_wait,
        reviewed.digest_pin_updates,
        reviewed.source_file,
    ) != (
        current.mode,
        current.max_wait,
        current.digest_pin_updates,
        current.source_file,
    ):
        return ["WUDup update settings changed since the review."]
    return []


def _source_reasons(reviewed: DryRunPlan, current: DryRunPlan) -> list[str]:
    reasons: list[str] = []
    if reviewed.source.source_hash != current.source.source_hash:
        reviewed_raw = {target.raw for target in reviewed.targets}
        current_raw = {target.raw for target in current.targets}
        gone = sorted(reviewed_raw - current_raw)
        if gone:
            reasons.append(
                "WUD no longer reports these selected updates: "
                f"{_listed(gone)}."
            )
        else:
            reasons.append(
                "WUD's list of pending updates changed since the review "
                "(a WUD scan finished or a container changed)."
            )
    elif _selected(reviewed, reviewed.source.source_ids_by_line) != _selected(
        current, current.source.source_ids_by_line
    ):
        reasons.append(
            "A selected container was recreated or replaced since the review."
        )
    reviewed_status = _selected(reviewed, reviewed.source.metadata_status_by_line)
    current_status = _selected(current, current.source.metadata_status_by_line)
    if reviewed_status != current_status:
        reasons.append(
            "WUD metadata for a selected update changed freshness since the review."
        )
    return reasons


def _issue_reasons(reviewed: DryRunPlan, current: DryRunPlan) -> list[str]:
    reviewed_messages = _issue_messages(reviewed)
    current_messages = _issue_messages(current)
    reasons: list[str] = []
    added = [message for message in current_messages if message not in reviewed_messages]
    removed = [message for message in reviewed_messages if message not in current_messages]
    if added:
        reasons.append(f"Rechecking found a new problem: {_listed(added)}.")
    if removed:
        reasons.append(
            f"A problem shown in the review no longer applies: {_listed(removed)}."
        )
    return reasons


def _stack_reasons(reviewed: DryRunPlan, current: DryRunPlan) -> list[str]:
    reviewed_stacks = {stack.name: asdict(stack) for stack in reviewed.stacks}
    current_stacks = {stack.name: asdict(stack) for stack in current.stacks}
    changed = sorted(
        name
        for name in reviewed_stacks.keys() | current_stacks.keys()
        if reviewed_stacks.get(name) != current_stacks.get(name)
    )
    if not changed:
        return []
    label = "stack" if len(changed) == 1 else "stacks"
    return [
        (
            f"The planned changes for {label} {_listed(changed)} changed "
            "(Compose file, services, or image tags)."
        )
    ]


def _issue_messages(plan: DryRunPlan) -> list[str]:
    return list(dict.fromkeys(issue.message for issue in plan.issues))


def _selected(plan: DryRunPlan, values: Any) -> dict[int, object]:
    return {
        line_no: values.get(line_no)
        for line_no in plan.selected_line_numbers
    }


def _listed(values: Iterable[str]) -> str:
    items = [_clip(value) for value in values]
    shown = "; ".join(items[:_MAX_LISTED])
    extra = len(items) - _MAX_LISTED
    return f"{shown} (and {extra} more)" if extra > 0 else shown


def _clip(value: str) -> str:
    value = " ".join(value.split()).rstrip(".")
    if len(value) <= _MAX_MESSAGE_CHARS:
        return value
    return f"{value[: _MAX_MESSAGE_CHARS - 3]}..."
