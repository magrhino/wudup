"""WebUI automatic update scheduler for WUDup."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from datetime import time as datetime_time
from threading import Event, Thread
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException

from . import web_database, web_job_registry, web_jobs, web_pending_sources
from .config import UpdaterConfig
from .db import (
    active_dependency_snooze_rows,
    active_snooze,
    init_db,
    open_db,
    utc_timestamp,
)
from .digest_provenance import DigestTagProvenance
from .plans import (
    DryRunPlan,
    build_dry_run_plan_from_pending_source,
    resolve_pending_groups,
)
from .web_database import immediate_transaction as _immediate_transaction
from .web_job_registry import plan_can_apply
from .web_metadata import json_object as _json_object
from .web_metadata import json_object_or_empty
from .web_models import (
    ApplyJobResponse,
    ApplyJobStatus,
    AutoUpdatePolicy,
    AutoUpdateSelection,
    WebSettings,
)

AUTO_UPDATE_POLL_SECONDS = 60.0
AUTO_UPDATE_GRACE_SECONDS = 300
# A slot that could not start inside its grace window because something else
# blocked it is held this long before it is recorded as missed.
AUTO_UPDATE_MAX_LATE_SECONDS = 3600
AUTO_UPDATE_MISSED_MODE = "auto-update-missed"
AUTO_UPDATE_HOLD_BUSY = "Another WUDup job was still running."
AUTO_UPDATE_HOLD_OTHER_MODE = (
    "Services scheduled at the same time with a different update mode ran first."
)
AUTO_UPDATE_HOLD_SUBMIT_FAILED = "The update job could not be started."
AUTO_UPDATE_HOLD_PLAN_BLOCKED = (
    "The update plan could not be applied automatically; open Pending updates "
    "to review it."
)
AUTO_UPDATE_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LOGGER = logging.getLogger(__name__)
AutoUpdateCandidate = tuple[int, tuple[str, ...], tuple[AutoUpdatePolicy, ...]]


class AutoUpdateScheduleReservationError(RuntimeError):
    """Raised when an automatic update schedule slot was already claimed."""


class EffectiveConfigLoader(Protocol):
    def __call__(self, settings: WebSettings) -> UpdaterConfig: ...


def initialize_auto_update_scheduler_state(state: Any) -> None:
    state.web_auto_update_started_at = datetime.now(UTC)
    state.web_auto_update_stop = Event()
    state.web_auto_update_thread = None
    state.web_auto_update_held = {}


def shutdown_auto_update_scheduler_state(state: Any) -> None:
    stop_event: Event = state.web_auto_update_stop
    stop_event.set()
    thread = state.web_auto_update_thread
    if thread is not None:
        thread.join(timeout=1.0)


def start_auto_update_scheduler(
    app: FastAPI,
    settings: WebSettings,
    *,
    effective_config_loader: EffectiveConfigLoader,
) -> Thread | None:
    if not settings.mutations_enabled:
        return None
    existing_thread = app.state.web_auto_update_thread
    if existing_thread is not None and existing_thread.is_alive():
        return existing_thread
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
    stop_event = Event()
    app.state.web_auto_update_stop = stop_event
    thread = Thread(
        target=_auto_update_scheduler_loop,
        args=(app, settings, stop_event, effective_config_loader),
        name="wud-auto-update-scheduler",
        daemon=True,
    )
    app.state.web_auto_update_thread = thread
    thread.start()
    return thread


def _auto_update_scheduler_loop(
    app: FastAPI,
    settings: WebSettings,
    stop_event: Event,
    effective_config_loader: EffectiveConfigLoader,
) -> None:
    try:
        _auto_update_tick(
            app,
            settings,
            effective_config_loader=effective_config_loader,
        )
    except Exception:
        LOGGER.exception("auto update scheduler tick failed")
    while not stop_event.wait(AUTO_UPDATE_POLL_SECONDS):
        try:
            _auto_update_tick(
                app,
                settings,
                effective_config_loader=effective_config_loader,
            )
        except Exception:
            LOGGER.exception("auto update scheduler tick failed")


def _auto_update_tick(
    app: FastAPI,
    settings: WebSettings,
    *,
    effective_config_loader: EffectiveConfigLoader,
    now: datetime | None = None,
) -> ApplyJobResponse | None:
    if not settings.mutations_enabled:
        return None
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    started_at = app.state.web_auto_update_started_at
    if not isinstance(started_at, datetime):
        started_at = now_utc
    started_at_utc = started_at.astimezone(UTC)
    held = _auto_update_held_slots(app.state)

    job_submitted = False
    start_event: Event | None = None
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            policies = _runnable_auto_update_policies(
                conn,
                settings,
                app.state,
                held,
                now_utc=now_utc,
                started_at=started_at_utc,
            )
            if not policies:
                return None
            candidate = _auto_update_candidate(
                conn,
                settings,
                effective_config_loader=effective_config_loader,
                now_utc=now_utc,
                started_at=started_at_utc,
                policies=policies,
                held=held,
            )
            if candidate is None:
                return None
            selection, plan, pending_source = candidate
            with _immediate_transaction(conn):
                _reserve_auto_update_schedule_runs(conn, settings, selection)
                start_event = Event()
            try:
                response = web_jobs._submit_apply_job_state(
                    app.state,
                    settings,
                    plan,
                    allow_tag_updates=False,
                    tag_overrides=(),
                    effective_config_loader=effective_config_loader,
                    auto_update_schedule_run_updater=(
                        _safe_update_auto_update_schedule_runs
                    ),
                    run_context=web_jobs.ApplyJobRunContext(
                        update_mode_override=selection.update_mode,
                        metadata_extra={
                            "source": "webui-auto",
                            "actor_type": "scheduler",
                            "auto_update_service_keys": list(selection.service_keys),
                            "auto_update_schedule_keys": list(selection.schedule_keys),
                            "auto_update_scheduled_for": (
                                selection.scheduled_for.isoformat()
                            ),
                            "timezone": settings.config.timezone_name,
                        },
                        auto_update_schedule_keys=selection.schedule_keys,
                        start_event=start_event,
                        pending_source_text=pending_source.text,
                        pending_source_active=pending_source.active,
                        pending_source_label=pending_source.label,
                        pending_source_container_ids=(
                            web_pending_sources.container_ids_for_lines(
                                pending_source,
                                plan.selected_line_numbers,
                            )
                        ),
                    ),
                )
            except Exception as exc:
                try:
                    with _immediate_transaction(conn):
                        _release_auto_update_schedule_runs(conn, selection)
                except Exception:
                    LOGGER.exception(
                        "failed to release auto update schedule reservation"
                    )
                _hold_failed_submission(held, selection, exc)
                raise
            job_submitted = True
            with _immediate_transaction(conn):
                _queue_auto_update_schedule_runs(
                    conn,
                    settings,
                    selection,
                    response.job_id,
                )
        if start_event is not None:
            start_event.set()
        return response
    except AutoUpdateScheduleReservationError:
        return None
    except Exception:
        if job_submitted and start_event is not None:
            start_event.set()
        raise


def _auto_update_held_slots(state: Any) -> dict[str, str]:
    held = getattr(state, "web_auto_update_held", None)
    if not isinstance(held, dict):
        held = {}
        state.web_auto_update_held = held
    return held


def _runnable_auto_update_policies(
    conn: sqlite3.Connection,
    settings: WebSettings,
    state: Any,
    held: dict[str, str],
    *,
    now_utc: datetime,
    started_at: datetime,
) -> dict[str, AutoUpdatePolicy]:
    """Record expired held slots, prune held, and return policies to check now.

    Returns nothing while another job is running; due slots are held instead.
    """
    policies, missed = _due_auto_update_policies(
        conn,
        settings,
        now_utc=now_utc,
        started_at=started_at,
        held=held,
    )
    if missed:
        with _immediate_transaction(conn):
            _record_missed_auto_update_slots(
                conn, settings, missed, held, now_utc=now_utc
            )
    due_schedule_keys = {policy.schedule_key for policy in policies.values()}
    for schedule_key in [key for key in held if key not in due_schedule_keys]:
        del held[schedule_key]
    if not policies:
        return {}
    active_error = web_job_registry._active_mutation_error_in_state(state)
    if active_error:
        reason = _busy_hold_reason(active_error)
        for schedule_key in due_schedule_keys:
            held.setdefault(schedule_key, reason)
        return {}
    return policies


def _busy_hold_reason(active_error: str) -> str:
    detail = active_error.strip().rstrip(".")
    if not detail:
        return AUTO_UPDATE_HOLD_BUSY
    return f"{AUTO_UPDATE_HOLD_BUSY[:-1]}: {detail}."


def _hold_failed_submission(
    held: dict[str, str],
    selection: AutoUpdateSelection,
    exc: Exception,
) -> None:
    # Keep the slot so the next tick can still run it late. A 409 means another
    # job started while the plan was built.
    reason = (
        _busy_hold_reason(str(exc.detail))
        if isinstance(exc, HTTPException) and exc.status_code == 409
        else AUTO_UPDATE_HOLD_SUBMIT_FAILED
    )
    for schedule_key in selection.schedule_keys:
        held.setdefault(schedule_key, reason)


def _auto_update_candidate(
    conn: sqlite3.Connection,
    settings: WebSettings,
    *,
    effective_config_loader: EffectiveConfigLoader,
    now_utc: datetime,
    started_at: datetime,
    policies: Mapping[str, AutoUpdatePolicy] | None = None,
    held: dict[str, str] | None = None,
) -> tuple[
    AutoUpdateSelection,
    DryRunPlan,
    web_pending_sources.PendingSourceResult,
] | None:
    if policies is None:
        policies, _missed = _due_auto_update_policies(
            conn,
            settings,
            now_utc=now_utc,
            started_at=started_at,
        )
    if held is None:
        held = {}
    if not policies:
        return None
    pending_source = web_pending_sources.resolve_pending_source(
        settings,
        force_api=True,
    )
    parsed = pending_source.parsed

    effective_config = effective_config_loader(settings)
    known_digest_provenance_by_service = (
        web_database.known_digest_provenance_by_service(settings)
    )
    grouping = resolve_pending_groups(
        effective_config,
        parsed,
        host_docker_base=settings.host_docker_base,
        environ=settings.command_env,
        known_digest_provenance_by_service=known_digest_provenance_by_service,
    )
    if grouping.status != "ready":
        return None

    pending_service_keys = _pending_service_keys(grouping)
    dependency_snoozes = active_dependency_snooze_rows(
        conn,
        service_keys=pending_service_keys,
    )
    checked: dict[str, str] = {}
    result: tuple[
        AutoUpdateSelection,
        DryRunPlan,
        web_pending_sources.PendingSourceResult,
    ] | None = None
    remaining = dict(policies)
    while True:
        selection = _auto_update_selection(
            settings,
            grouping,
            remaining,
            dependency_snoozes=dependency_snoozes,
        )
        if selection is None:
            break
        plan = _build_auto_update_plan(
            settings,
            selection.line_numbers,
            update_mode_override=selection.update_mode,
            base_config=effective_config,
            known_digest_provenance_by_service=known_digest_provenance_by_service,
            pending_source=pending_source,
        )
        if plan_can_apply(plan, settings):
            checked.update(
                (schedule_key, AUTO_UPDATE_HOLD_OTHER_MODE)
                for schedule_key in selection.deferred_schedule_keys
            )
            result = (selection, plan, pending_source)
            break
        # A blocked plan must not starve due slots in other update modes.
        checked.update(
            (schedule_key, AUTO_UPDATE_HOLD_PLAN_BLOCKED)
            for schedule_key in selection.schedule_keys
        )
        remaining = {
            service_key: policy
            for service_key, policy in remaining.items()
            if policy.schedule_key not in selection.schedule_keys
        }
    # The check completed: hold only slots that had an update but could not
    # start now. Slots with nothing to apply are released. An error above
    # leaves the previous held set untouched.
    held.clear()
    held.update(checked)
    return result


def _build_auto_update_plan(
    settings: WebSettings,
    line_numbers: Sequence[int],
    *,
    update_mode_override: str | None,
    base_config: UpdaterConfig,
    known_digest_provenance_by_service: Mapping[str, DigestTagProvenance],
    pending_source: web_pending_sources.PendingSourceResult,
) -> DryRunPlan:
    config: UpdaterConfig = (
        base_config
        if update_mode_override is None
        else replace(base_config, update_mode=update_mode_override)
    )
    return build_dry_run_plan_from_pending_source(
        config,
        pending_source.parsed,
        source_file=pending_source.source_file,
        source_hash=pending_source.source_hash,
        source=pending_source.plan_source(),
        line_numbers=line_numbers,
        allow_tag_updates=False,
        digest_pin_label_rewrite_approvals=(),
        host_docker_base=settings.host_docker_base,
        environ=settings.command_env,
        known_digest_provenance_by_service=known_digest_provenance_by_service,
    )


def _due_auto_update_policies(
    conn: sqlite3.Connection,
    settings: WebSettings,
    *,
    now_utc: datetime,
    started_at: datetime,
    held: Collection[str] = (),
) -> tuple[dict[str, AutoUpdatePolicy], list[AutoUpdatePolicy]]:
    """Return due policies and held slots whose late limit has passed.

    A slot is due inside its grace window. A held slot (one that something
    else blocked during its window) stays due until the late limit.
    """
    tz = ZoneInfo(settings.config.timezone_name)
    local_now = now_utc.astimezone(tz)
    now_text = now_utc.replace(microsecond=0).isoformat()
    rows = conn.execute(
        """
        SELECT *
        FROM service_policy
        WHERE auto_update = 1
          AND auto_update_time IS NOT NULL
        ORDER BY service_key COLLATE BINARY
        """
    ).fetchall()
    policies: dict[str, AutoUpdatePolicy] = {}
    missed: list[AutoUpdatePolicy] = []
    for row in rows:
        days = _auto_update_days_from_row(row)
        update_time = str(row["auto_update_time"])
        occurrence = _auto_update_row_occurrence(
            update_time,
            days,
            local_now=local_now,
            now_utc=now_utc,
            tz=tz,
        )
        if occurrence is None:
            continue
        scheduled_local, scheduled_for = occurrence
        service_key = str(row["service_key"])
        schedule_key = _auto_update_schedule_key(
            service_key,
            local_date=scheduled_local.date().isoformat(),
            update_time=update_time,
            timezone_name=settings.config.timezone_name,
        )
        window_end = scheduled_for + timedelta(seconds=AUTO_UPDATE_GRACE_SECONDS)
        in_window = now_utc < window_end and started_at < window_end
        if not in_window and schedule_key not in held:
            continue
        if _auto_update_slot_closed(conn, service_key, schedule_key, now_text):
            continue
        policy = AutoUpdatePolicy(
            service_key=service_key,
            update_mode=str(row["update_mode"] or settings.config.update_mode),
            auto_update_time=update_time,
            auto_update_days=days,
            schedule_key=schedule_key,
            scheduled_for=scheduled_for,
        )
        late_limit = scheduled_for + timedelta(seconds=AUTO_UPDATE_MAX_LATE_SECONDS)
        if in_window or now_utc < late_limit:
            policies[service_key] = policy
        else:
            missed.append(policy)
    return policies, missed


def _auto_update_row_occurrence(
    update_time: str,
    days: Sequence[str],
    *,
    local_now: datetime,
    now_utc: datetime,
    tz: ZoneInfo,
) -> tuple[datetime, datetime] | None:
    try:
        parsed_time = datetime_time.fromisoformat(update_time)
    except ValueError:
        return None
    return _auto_update_latest_occurrence(
        local_now=local_now,
        parsed_time=parsed_time,
        days=days,
        now_utc=now_utc,
        tz=tz,
    )


def _auto_update_slot_closed(
    conn: sqlite3.Connection,
    service_key: str,
    schedule_key: str,
    now_text: str,
) -> bool:
    """Return whether the slot is snoozed or already has a schedule row."""
    if active_snooze(conn, service_key=service_key, now=now_text) is not None:
        return True
    return _auto_update_schedule_recorded(conn, schedule_key)


def _auto_update_latest_occurrence(
    *,
    local_now: datetime,
    parsed_time: datetime_time,
    days: Sequence[str],
    now_utc: datetime,
    tz: ZoneInfo,
) -> tuple[datetime, datetime] | None:
    candidate_dates = (
        local_now.date(),
        (local_now - timedelta(days=1)).date(),
    )
    for local_date in candidate_dates:
        scheduled_local = datetime.combine(local_date, parsed_time, tzinfo=tz)
        day = AUTO_UPDATE_DAYS[scheduled_local.weekday()]
        if day not in days:
            continue
        scheduled_for = scheduled_local.astimezone(UTC)
        if scheduled_for <= now_utc:
            return scheduled_local, scheduled_for
    return None


def _auto_update_selection(
    settings: WebSettings,
    grouping: Any,
    policies: Mapping[str, AutoUpdatePolicy],
    *,
    dependency_snoozes: Iterable[Any] = (),
) -> AutoUpdateSelection | None:
    if not policies:
        return None
    candidates_by_mode = _auto_update_candidates_by_mode(
        settings,
        grouping,
        policies,
    )
    lines_by_mode, services_by_mode, schedules_by_mode, scheduled_for_by_mode = (
        _eligible_auto_update_candidates_by_mode(
            candidates_by_mode,
            dependency_snoozes=dependency_snoozes,
        )
    )
    eligible_modes = [
        mode
        for mode in sorted(lines_by_mode)
        if lines_by_mode[mode] and mode in scheduled_for_by_mode
    ]
    if not eligible_modes:
        return None
    mode = min(eligible_modes, key=lambda item: scheduled_for_by_mode[item])
    line_numbers = tuple(sorted(set(lines_by_mode[mode])))
    deferred_schedule_keys = {
        schedule_key
        for other_mode in eligible_modes
        if other_mode != mode
        for schedule_key in schedules_by_mode[other_mode]
    }
    return AutoUpdateSelection(
        line_numbers=line_numbers,
        service_keys=tuple(sorted(services_by_mode[mode])),
        schedule_keys=tuple(sorted(schedules_by_mode[mode])),
        scheduled_for=scheduled_for_by_mode[mode],
        update_mode=mode,
        deferred_schedule_keys=tuple(sorted(deferred_schedule_keys)),
    )


def _auto_update_candidates_by_mode(
    settings: WebSettings,
    grouping: Any,
    policies: Mapping[str, AutoUpdatePolicy],
) -> dict[str, list[AutoUpdateCandidate]]:
    candidates_by_mode: dict[str, list[AutoUpdateCandidate]] = {}
    for group in grouping.groups:
        for item in group.items:
            candidate = _auto_update_candidate_for_item(
                settings,
                group.name,
                item,
                policies,
            )
            if candidate is None:
                continue
            mode, line_candidate = candidate
            candidates_by_mode.setdefault(mode, []).append(line_candidate)
    return candidates_by_mode


def _auto_update_candidate_for_item(
    settings: WebSettings,
    group_name: str,
    item: Any,
    policies: Mapping[str, AutoUpdatePolicy],
) -> tuple[str, AutoUpdateCandidate] | None:
    if item.desired_tag:
        return None
    service_keys = tuple(
        f"{group_name}/{service}" for service in item.services if service
    )
    if not service_keys:
        return None
    line_policies = tuple(policies.get(service_key) for service_key in service_keys)
    if any(policy is None for policy in line_policies):
        return None
    concrete = tuple(policy for policy in line_policies if policy is not None)
    mode = concrete[0].update_mode or settings.config.update_mode
    if any(
        (policy.update_mode or settings.config.update_mode) != mode
        for policy in concrete
    ):
        return None
    return mode, (item.line_no, service_keys, concrete)


def _eligible_auto_update_candidates_by_mode(
    candidates_by_mode: Mapping[str, Sequence[AutoUpdateCandidate]],
    *,
    dependency_snoozes: Iterable[Any],
) -> tuple[
    dict[str, list[int]],
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, datetime],
]:
    lines_by_mode: dict[str, list[int]] = {}
    services_by_mode: dict[str, set[str]] = {}
    schedules_by_mode: dict[str, set[str]] = {}
    scheduled_for_by_mode: dict[str, datetime] = {}
    for mode, candidates in candidates_by_mode.items():
        eligible = _dependency_eligible_auto_update_candidates(
            candidates,
            dependency_snoozes=dependency_snoozes,
        )
        for line_no, service_keys, concrete in eligible:
            lines_by_mode.setdefault(mode, []).append(line_no)
            services_by_mode.setdefault(mode, set()).update(service_keys)
            schedules_by_mode.setdefault(mode, set()).update(
                policy.schedule_key for policy in concrete
            )
            scheduled_for = min(policy.scheduled_for for policy in concrete)
            current = scheduled_for_by_mode.get(mode)
            scheduled_for_by_mode[mode] = (
                scheduled_for if current is None else min(current, scheduled_for)
            )
    return (
        lines_by_mode,
        services_by_mode,
        schedules_by_mode,
        scheduled_for_by_mode,
    )


def _dependency_eligible_auto_update_candidates(
    candidates: Sequence[AutoUpdateCandidate],
    *,
    dependency_snoozes: Iterable[Any],
) -> tuple[AutoUpdateCandidate, ...]:
    waits_by_service = _dependency_waits_by_service(dependency_snoozes)
    return tuple(
        candidate
        for candidate in candidates
        if all(service_key not in waits_by_service for service_key in candidate[1])
    )


def _dependency_waits_by_service(
    dependency_snoozes: Iterable[Any],
) -> dict[str, tuple[str, ...]]:
    waits: dict[str, list[str]] = {}
    for row in dependency_snoozes:
        service_key = _dependency_snooze_value(row, "service_key")
        wait_for_service_key = _dependency_snooze_value(row, "wait_for_service_key")
        if service_key and wait_for_service_key:
            waits.setdefault(service_key, []).append(wait_for_service_key)
    return {
        service_key: tuple(dict.fromkeys(wait_for_service_keys))
        for service_key, wait_for_service_keys in waits.items()
    }


def _dependency_snooze_value(row: Any, key: str) -> str:
    if isinstance(row, Mapping):
        return str(row.get(key, ""))
    try:
        return str(row[key])
    except (IndexError, KeyError, TypeError):
        return str(getattr(row, key, ""))


def _pending_service_keys(grouping: Any) -> tuple[str, ...]:
    service_keys: list[str] = []
    for group in grouping.groups:
        for item in group.items:
            for service in item.services:
                if service:
                    service_keys.append(f"{group.name}/{service}")
    return tuple(sorted(set(service_keys)))


def _auto_update_schedule_key(
    service_key: str,
    *,
    local_date: str,
    update_time: str,
    timezone_name: str,
) -> str:
    return f"{service_key}|{local_date}|{update_time}|{timezone_name}"


def _auto_update_schedule_recorded(conn: sqlite3.Connection, schedule_key: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM auto_update_schedule_runs
        WHERE schedule_key = ?
        LIMIT 1
        """,
        (schedule_key,),
    ).fetchone()
    return row is not None


def _auto_update_schedule_metadata(
    settings: WebSettings,
    selection: AutoUpdateSelection,
    *,
    job_id: str = "",
    status: str = "reserved",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": "webui-auto",
        "line_numbers": list(selection.line_numbers),
        "service_keys": list(selection.service_keys),
        "scheduled_for": selection.scheduled_for.isoformat(),
        "timezone": settings.config.timezone_name,
        "update_mode": selection.update_mode,
        "status": status,
    }
    if job_id:
        metadata["job_id"] = job_id
    return metadata


def _reserve_auto_update_schedule_runs(
    conn: sqlite3.Connection,
    settings: WebSettings,
    selection: AutoUpdateSelection,
) -> None:
    now = utc_timestamp()
    metadata = _json_object(_auto_update_schedule_metadata(settings, selection))
    for schedule_key in selection.schedule_keys:
        service_key = schedule_key.split("|", 1)[0]
        try:
            conn.execute(
                """
                INSERT INTO auto_update_schedule_runs (
                    schedule_key,
                    service_key,
                    scheduled_for,
                    run_id,
                    status,
                    created_at,
                    updated_at,
                    metadata_json
                )
                VALUES (?, ?, ?, NULL, 'reserved', ?, ?, ?)
                """,
                (
                    schedule_key,
                    service_key,
                    selection.scheduled_for.isoformat(),
                    now,
                    now,
                    metadata,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise AutoUpdateScheduleReservationError(schedule_key) from exc


def _record_missed_auto_update_slots(
    conn: sqlite3.Connection,
    settings: WebSettings,
    missed: Sequence[AutoUpdatePolicy],
    held: Mapping[str, str],
    *,
    now_utc: datetime,
) -> None:
    """Record held slots that passed the late limit as one run in History.

    Runs inside an immediate transaction. Each slot is rechecked against the
    current policy and claimed with a `missed` schedule row, so a slot is
    recorded at most once even across processes or restarts.
    """
    now = utc_timestamp()
    missed = [
        policy
        for policy in missed
        if _claim_missed_auto_update_slot(
            conn,
            settings,
            policy,
            reason=held.get(policy.schedule_key, AUTO_UPDATE_HOLD_BUSY),
            now=now,
            now_utc=now_utc,
        )
    ]
    if not missed:
        return
    late_minutes = AUTO_UPDATE_MAX_LATE_SECONDS // 60
    slots = [
        {
            "service_key": policy.service_key,
            "schedule_key": policy.schedule_key,
            "scheduled_for": policy.scheduled_for.isoformat(),
            "update_mode": policy.update_mode,
            "reason": held.get(policy.schedule_key, AUTO_UPDATE_HOLD_BUSY),
        }
        for policy in missed
    ]
    metadata = {
        "source": "webui-auto",
        "actor_type": "scheduler",
        "status": "missed",
        "summary": (
            f"Scheduled auto-update did not start within {late_minutes} minutes "
            "of its scheduled time, so it was skipped. If an update is still "
            "pending, it will run at the next scheduled time, or you can apply "
            "it from Pending updates."
        ),
        "service_keys": sorted({policy.service_key for policy in missed}),
        "slots": slots,
        "timezone": settings.config.timezone_name,
    }
    cursor = conn.execute(
        """
        INSERT INTO update_runs (
            started_at,
            finished_at,
            status,
            dry_run,
            mode,
            wud_file,
            log_file,
            metadata_json
        )
        VALUES (?, ?, 'missed', 0, ?, '', '', ?)
        """,
        (now, now, AUTO_UPDATE_MISSED_MODE, _json_object(metadata)),
    )
    run_id = int(cursor.lastrowid)
    conn.executemany(
        """
        UPDATE auto_update_schedule_runs
        SET run_id = ?
        WHERE schedule_key = ?
        """,
        [(run_id, policy.schedule_key) for policy in missed],
    )


def _claim_missed_auto_update_slot(
    conn: sqlite3.Connection,
    settings: WebSettings,
    policy: AutoUpdatePolicy,
    *,
    reason: str,
    now: str,
    now_utc: datetime,
) -> bool:
    if not _missed_auto_update_slot_still_scheduled(conn, settings, policy, now_utc):
        return False
    metadata = {
        "source": "webui-auto",
        "status": "missed",
        "scheduled_for": policy.scheduled_for.isoformat(),
        "update_mode": policy.update_mode,
        "reason": reason,
    }
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO auto_update_schedule_runs (
            schedule_key,
            service_key,
            scheduled_for,
            run_id,
            status,
            created_at,
            updated_at,
            metadata_json
        )
        VALUES (?, ?, ?, NULL, 'missed', ?, ?, ?)
        """,
        (
            policy.schedule_key,
            policy.service_key,
            policy.scheduled_for.isoformat(),
            now,
            now,
            _json_object(metadata),
        ),
    )
    return cursor.rowcount == 1


def _missed_auto_update_slot_still_scheduled(
    conn: sqlite3.Connection,
    settings: WebSettings,
    policy: AutoUpdatePolicy,
    now_utc: datetime,
) -> bool:
    """Return whether the current policy still schedules this slot."""
    row = conn.execute(
        """
        SELECT auto_update, auto_update_time, auto_update_days_json
        FROM service_policy
        WHERE service_key = ?
        """,
        (policy.service_key,),
    ).fetchone()
    if row is None or not row["auto_update"]:
        return False
    if str(row["auto_update_time"]) != policy.auto_update_time:
        return False
    tz = ZoneInfo(settings.config.timezone_name)
    day = AUTO_UPDATE_DAYS[policy.scheduled_for.astimezone(tz).weekday()]
    if day not in _auto_update_days_from_row(row):
        return False
    now_text = now_utc.replace(microsecond=0).isoformat()
    return active_snooze(conn, service_key=policy.service_key, now=now_text) is None


def _release_auto_update_schedule_runs(
    conn: sqlite3.Connection,
    selection: AutoUpdateSelection,
) -> None:
    for schedule_key in selection.schedule_keys:
        conn.execute(
            """
            DELETE FROM auto_update_schedule_runs
            WHERE schedule_key = ?
              AND run_id IS NULL
              AND status = 'reserved'
            """,
            (schedule_key,),
        )


def _queue_auto_update_schedule_runs(
    conn: sqlite3.Connection,
    settings: WebSettings,
    selection: AutoUpdateSelection,
    job_id: str,
) -> None:
    now = utc_timestamp()
    metadata = _json_object(
        _auto_update_schedule_metadata(
            settings,
            selection,
            job_id=job_id,
            status="queued",
        )
    )
    for schedule_key in selection.schedule_keys:
        conn.execute(
            """
            UPDATE auto_update_schedule_runs
            SET status = 'queued',
                updated_at = ?,
                metadata_json = ?
            WHERE schedule_key = ?
            """,
            (now, metadata, schedule_key),
        )


def _safe_update_auto_update_schedule_runs(
    settings: WebSettings,
    schedule_keys: Sequence[str],
    *,
    status: ApplyJobStatus,
    run_id: int | None,
    error: str = "",
) -> None:
    if not schedule_keys:
        return
    try:
        _update_auto_update_schedule_runs(
            settings,
            schedule_keys,
            status=status,
            run_id=run_id,
            error=error,
        )
    except Exception:
        LOGGER.exception("failed to update auto update schedule run status")


def _update_auto_update_schedule_runs(
    settings: WebSettings,
    schedule_keys: Sequence[str],
    *,
    status: ApplyJobStatus,
    run_id: int | None,
    error: str = "",
) -> None:
    now = utc_timestamp()
    with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
        init_db(conn)
        with conn:
            for schedule_key in schedule_keys:
                metadata = _auto_update_schedule_row_metadata(conn, schedule_key)
                metadata["status"] = status
                if run_id is None:
                    metadata.pop("run_id", None)
                else:
                    metadata["run_id"] = run_id
                if error:
                    metadata["error"] = error
                else:
                    metadata.pop("error", None)
                conn.execute(
                    """
                    UPDATE auto_update_schedule_runs
                    SET run_id = ?,
                        status = ?,
                        updated_at = ?,
                        metadata_json = ?
                    WHERE schedule_key = ?
                    """,
                    (run_id, status, now, _json_object(metadata), schedule_key),
                )


def _auto_update_schedule_row_metadata(
    conn: sqlite3.Connection,
    schedule_key: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT metadata_json
        FROM auto_update_schedule_runs
        WHERE schedule_key = ?
        LIMIT 1
        """,
        (schedule_key,),
    ).fetchone()
    if row is None:
        return {}
    return json_object_or_empty(row["metadata_json"])


def _auto_update_days_from_row(row: sqlite3.Row) -> tuple[str, ...]:
    raw = str(row["auto_update_days_json"] or "[]")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    days: list[str] = []
    for item in parsed:
        if isinstance(item, str) and item in AUTO_UPDATE_DAYS and item not in days:
            days.append(item)
    return tuple(days)
