from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from wudup import web_scheduler


def test_auto_update_selection_prefers_earliest_scheduled_mode() -> None:
    earlier = datetime(2026, 5, 30, 14, 0, tzinfo=UTC)
    later = datetime(2026, 5, 30, 15, 0, tzinfo=UTC)
    settings = SimpleNamespace(config=SimpleNamespace(update_mode="live"))
    grouping = SimpleNamespace(
        groups=(
            SimpleNamespace(
                name="stack",
                items=(
                    SimpleNamespace(
                        desired_tag="",
                        line_no=1,
                        services=("app",),
                    ),
                    SimpleNamespace(
                        desired_tag="",
                        line_no=2,
                        services=("worker",),
                    ),
                ),
            ),
        ),
    )
    policies = {
        "stack/app": web_scheduler.AutoUpdatePolicy(
            service_key="stack/app",
            update_mode="a-later",
            auto_update_time="10:00",
            auto_update_days=("sat",),
            schedule_key="stack/app|2026-05-30|10:00|America/Chicago",
            scheduled_for=later,
        ),
        "stack/worker": web_scheduler.AutoUpdatePolicy(
            service_key="stack/worker",
            update_mode="z-earlier",
            auto_update_time="09:00",
            auto_update_days=("sat",),
            schedule_key="stack/worker|2026-05-30|09:00|America/Chicago",
            scheduled_for=earlier,
        ),
    }

    selection = web_scheduler._auto_update_selection(settings, grouping, policies)

    assert selection is not None
    assert selection.update_mode == "z-earlier"
    assert selection.line_numbers == (2,)
    assert selection.service_keys == ("stack/worker",)
    assert selection.schedule_keys == (
        "stack/worker|2026-05-30|09:00|America/Chicago",
    )
    assert selection.scheduled_for == earlier
    assert selection.deferred_schedule_keys == (
        "stack/app|2026-05-30|10:00|America/Chicago",
    )


def test_auto_update_selection_excludes_unsatisfied_dependency_snoozes() -> None:
    scheduled = datetime(2026, 5, 30, 14, 0, tzinfo=UTC)
    settings = SimpleNamespace(config=SimpleNamespace(update_mode="live"))
    grouping = SimpleNamespace(
        groups=(
            SimpleNamespace(
                name="stack",
                items=(
                    SimpleNamespace(
                        desired_tag="",
                        line_no=1,
                        services=("app",),
                    ),
                    SimpleNamespace(
                        desired_tag="",
                        line_no=2,
                        services=("worker",),
                    ),
                    SimpleNamespace(
                        desired_tag="",
                        line_no=3,
                        services=("db",),
                    ),
                ),
            ),
        ),
    )
    policies = {
        "stack/app": web_scheduler.AutoUpdatePolicy(
            service_key="stack/app",
            update_mode="live",
            auto_update_time="09:00",
            auto_update_days=("sat",),
            schedule_key="stack/app|2026-05-30|09:00|America/Chicago",
            scheduled_for=scheduled,
        ),
        "stack/worker": web_scheduler.AutoUpdatePolicy(
            service_key="stack/worker",
            update_mode="live",
            auto_update_time="09:00",
            auto_update_days=("sat",),
            schedule_key="stack/worker|2026-05-30|09:00|America/Chicago",
            scheduled_for=scheduled,
        ),
        "stack/db": web_scheduler.AutoUpdatePolicy(
            service_key="stack/db",
            update_mode="live",
            auto_update_time="09:00",
            auto_update_days=("sat",),
            schedule_key="stack/db|2026-05-30|09:00|America/Chicago",
            scheduled_for=scheduled,
        ),
    }

    selection = web_scheduler._auto_update_selection(
        settings,
        grouping,
        policies,
        dependency_snoozes=(
            {
                "service_key": "stack/app",
                "wait_for_service_key": "stack/db",
            },
        ),
    )

    assert selection is not None
    assert selection.line_numbers == (2, 3)
    assert selection.service_keys == ("stack/db", "stack/worker")


def test_auto_update_selection_uses_eligible_candidate_schedule() -> None:
    earliest = datetime(2026, 5, 30, 14, 0, tzinfo=UTC)
    selected = datetime(2026, 5, 30, 14, 2, tzinfo=UTC)
    later = datetime(2026, 5, 30, 14, 4, tzinfo=UTC)
    settings = SimpleNamespace(config=SimpleNamespace(update_mode="live"))
    grouping = SimpleNamespace(
        groups=(
            SimpleNamespace(
                name="stack",
                items=(
                    SimpleNamespace(
                        desired_tag="",
                        line_no=1,
                        services=("snoozed",),
                    ),
                    SimpleNamespace(
                        desired_tag="",
                        line_no=2,
                        services=("later",),
                    ),
                    SimpleNamespace(
                        desired_tag="",
                        line_no=3,
                        services=("selected",),
                    ),
                ),
            ),
        ),
    )

    def policy(
        service: str,
        mode: str,
        scheduled_for: datetime,
    ) -> web_scheduler.AutoUpdatePolicy:
        return web_scheduler.AutoUpdatePolicy(
            service_key=f"stack/{service}",
            update_mode=mode,
            auto_update_time="09:00",
            auto_update_days=("sat",),
            schedule_key=f"stack/{service}|2026-05-30|09:00|America/Chicago",
            scheduled_for=scheduled_for,
        )

    selection = web_scheduler._auto_update_selection(
        settings,
        grouping,
        {
            "stack/snoozed": policy("snoozed", "mode-a", earliest),
            "stack/later": policy("later", "mode-a", later),
            "stack/selected": policy("selected", "mode-b", selected),
        },
        dependency_snoozes=(
            {
                "service_key": "stack/snoozed",
                "wait_for_service_key": "stack/dependency",
            },
        ),
    )

    assert selection is not None
    assert selection.update_mode == "mode-b"
    assert selection.line_numbers == (3,)
    assert selection.service_keys == ("stack/selected",)
    assert selection.schedule_keys == (
        "stack/selected|2026-05-30|09:00|America/Chicago",
    )
    assert selection.scheduled_for == selected


def test_auto_update_selection_requires_complete_consistent_service_policies() -> None:
    scheduled = datetime(2026, 5, 30, 14, 0, tzinfo=UTC)
    settings = SimpleNamespace(config=SimpleNamespace(update_mode="live"))
    grouping = SimpleNamespace(
        groups=(
            SimpleNamespace(
                name="stack",
                items=(
                    SimpleNamespace(
                        desired_tag="",
                        line_no=1,
                        services=("app", "sidecar"),
                    ),
                    SimpleNamespace(
                        desired_tag="",
                        line_no=2,
                        services=("worker", "db"),
                    ),
                    SimpleNamespace(
                        desired_tag="",
                        line_no=3,
                        services=("ready",),
                    ),
                ),
            ),
        ),
    )

    def policy(service: str, mode: str) -> web_scheduler.AutoUpdatePolicy:
        return web_scheduler.AutoUpdatePolicy(
            service_key=f"stack/{service}",
            update_mode=mode,
            auto_update_time="09:00",
            auto_update_days=("sat",),
            schedule_key=f"stack/{service}|2026-05-30|09:00|America/Chicago",
            scheduled_for=scheduled,
        )

    selection = web_scheduler._auto_update_selection(
        settings,
        grouping,
        {
            "stack/app": policy("app", "live"),
            "stack/worker": policy("worker", "live"),
            "stack/db": policy("db", "stop"),
            "stack/ready": policy("ready", "live"),
        },
    )

    assert selection is not None
    assert selection.line_numbers == (3,)
    assert selection.service_keys == ("stack/ready",)
    assert selection.schedule_keys == (
        "stack/ready|2026-05-30|09:00|America/Chicago",
    )
