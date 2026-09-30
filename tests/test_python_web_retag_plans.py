from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

from wudup import web_retags
from wudup.compose import ComposeStack
from wudup.digest_provenance import DigestTagProvenance
from wudup.updater_models import AppliedDigestPinUpdate, DigestPinUpdate
from wudup.web_models import RetagPlanIssue, RetagPlanResponse
from wudup.web_retag_identity import retag_target_id
from wudup.web_retag_plans import (
    RetagPlanUpdate,
    ordered_retag_stacks,
    retag_plan_id,
    retag_plan_stacks,
    retag_plan_status,
)


def test_retag_plan_helpers_render_ordered_stacks_and_stable_ids(
    tmp_path: Path,
) -> None:
    alpha = _update(tmp_path, stack_index=1, stack_name="alpha", service="web")
    bravo = _update(tmp_path, stack_index=2, stack_name="bravo", service="api")
    selected = (bravo, alpha)

    assert [stack.name for stack in ordered_retag_stacks(selected)] == [
        "alpha",
        "bravo",
    ]

    stacks = retag_plan_stacks(selected)
    assert [stack.stack for stack in stacks] == ["alpha", "bravo"]
    assert stacks[0].services == ["web"]
    assert stacks[1].tag_updates[0].service_key == "bravo/api"
    assert stacks[1].digest_pin_updates[0].service_key == "bravo/api"
    assert (
        stacks[1].digest_pin_updates[0].final_image
        == stacks[1].tag_updates[0].final_image
    )

    plan = RetagPlanResponse(
        plan_id="",
        status=retag_plan_status(
            selected,
            {"alpha/web": "switch-to-concrete"},
            (),
        ),
        can_apply=True,
        selected_count=2,
        stacks=stacks,
    )

    assert retag_plan_id(
        plan,
        updates=selected,
        compose_hashes={"bravo": "2", "alpha": "1"},
    ) == retag_plan_id(
        plan,
        updates=(alpha, bravo),
        compose_hashes={"alpha": "1", "bravo": "2"},
    )

    running = replace(alpha, runtime_state="running")
    running_plan_id = retag_plan_id(
        plan,
        updates=(running,),
        compose_hashes={"alpha": "1"},
    )
    assert running_plan_id != retag_plan_id(
        plan,
        updates=(replace(running, runtime_state="not-running"),),
        compose_hashes={"alpha": "1"},
    )
    assert running_plan_id != retag_plan_id(
        plan,
        updates=(replace(running, allow_start=True),),
        compose_hashes={"alpha": "1"},
    )


def test_retag_plan_discloses_added_wud_tag_transform(tmp_path: Path) -> None:
    item = _update(tmp_path, stack_index=1, stack_name="arr", service="sonarr")
    transform = r"^(\d+)\.(\d+)\.(\d+)\.(\d+)-ls(\d+)$ => $1.$2.$3-$4.$5"
    applied = AppliedDigestPinUpdate(
        **{field.name: getattr(item.update, field.name) for field in fields(item.update)},
        replacements=1,
        added_transforms=(("sonarr", transform), ("other", "ignored")),
    )

    disclosed = web_retags._retag_update_with_label_rewrites(item, applied)
    stack = retag_plan_stacks((disclosed,))[0]

    assert disclosed.transform_label_value == transform
    assert stack.tag_updates[0].transform_label_value == transform
    assert stack.digest_pin_updates[0].transform_label_value == transform
    assert retag_plan_stacks((item,))[0].tag_updates[0].transform_label_value == ""


def test_retag_plan_status_reports_empty_and_blocked_states(
    tmp_path: Path,
) -> None:
    selected = (_update(tmp_path, stack_index=1, stack_name="alpha", service="web"),)

    assert retag_plan_status((), {}, ()) == "empty"
    assert retag_plan_status(selected, {}, ()) == "empty"
    assert (
        retag_plan_status(
            selected,
            {"alpha/web": "switch-to-concrete"},
            (
                RetagPlanIssue(
                    severity="error",
                    code="retag-target-not-eligible",
                    message="not eligible",
                ),
            ),
        )
        == "blocked"
    )


def _update(
    tmp_path: Path,
    *,
    stack_index: int,
    stack_name: str,
    service: str,
) -> RetagPlanUpdate:
    stack_dir = tmp_path / stack_name
    stack = ComposeStack(
        index=stack_index,
        directory=stack_dir,
        file="compose.yml",
        name=stack_name,
        images=(f"example/{service}:latest",),
        service_images=(),
    )
    return RetagPlanUpdate(
        target_id=retag_target_id(stack_dir, stack.file, "", stack.name, service),
        service_key=f"{stack_name}/{service}",
        stack=stack,
        update=DigestPinUpdate(
            old_image=f"example/{service}:latest",
            resolved_tag="latest",
            resolved_image=f"example/{service}:latest",
            planned_digest="sha256:" + "a" * 64,
            final_image=f"example/{service}@sha256:" + "a" * 64,
            watch_tag="latest",
            marker="wud.tag.include=latest",
            label_key="wud.tag.include",
            label_value="latest",
            services=(service,),
        ),
        provenance=DigestTagProvenance(
            source_image=f"example/{service}:latest",
            resolved_tag="latest",
            watch_tag="latest",
            target_digest="sha256:" + "a" * 64,
            final_image=f"example/{service}@sha256:" + "a" * 64,
            provenance_source="apply",
            provenance_confidence="verified",
        ),
    )
