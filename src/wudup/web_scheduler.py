"""WebUI automatic update scheduler for WUDup."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time
from threading import Event, Thread
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from fastapi import FastAPI

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
# A slot that no tick could check during its grace window, because a job was
# running or the check failed (including WUD or Compose discovery being
# unavailable), may still run late, but never more than this
# long after its scheduled time. Past that it is recorded as missed.
AUTO_UPDATE_MAX_LATE_SECONDS = 3600
AUTO_UPDATE_MISSED_IDLE_REASON = (
    "WUDup checked this scheduled update but found no pending update it could "
    "apply automatically, so nothing ran. If you expected an update, check "
    "that WUD reports one for this service and that the service is not "
    "snoozed, waiting on another service, or blocked in the update plan."
)
AUTO_UPDATE_MISSED_LATE_REASON = (
    "WUDup could not start this scheduled update within "
    f"{AUTO_UPDATE_MAX_LATE_SECONDS // 60} minutes of its scheduled time "
    "because another update job was running or the update check kept "
    "failing, so it was skipped. It will run at its next scheduled time; "
    "apply the update from the WebUI to install it sooner."
)
AUTO_UPDATE_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LOGGER = logging.getLogger(__name__)
AutoUpdateCandidate = tuple[int, tuple[str, ...], tuple[AutoUpdatePolicy, ...]]


class AutoUpdateScheduleReservationError(RuntimeError):
    """Raised when an automatic update schedule slot was already claimed."""


class _AutoUpdateCheckIncomplete(RuntimeError):
    """Raised when WUD or Compose discovery was unavailable for a check."""


class EffectiveConfigLoader(Protocol):
    def __call__(self, settings: WebSettings) -> UpdaterConfig: ...


def initialize_auto_update_scheduler_state(state: Any) -> None:
    state.web_auto_update_started_at = datetime.now(timezone.utc)
    # When a tick last finished checking every due slot without submitting a
    # job or failing. A slot whose grace window closed with no such check
    # since its scheduled time stays due until one runs, for at most
    # AUTO_UPDATE_MAX_LATE_SECONDS. A slot that will no longer run is recorded
    # as missed in auto_update_schedule_runs.
    state.web_auto_update_evaluated_at = None
    state.web_auto_update_stop = Event()
    state.web_auto_update_thread = None


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
    if (
        not settings.mutations_enabled
        or web_job_registry._active_apply_job_exists_in_state(app.state)
    ):
        return None
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started_at = app.state.web_auto_update_started_at
    if not isinstance(started_at, datetime):
        started_at = now_utc
    started_at_utc = started_at.astimezone(timezone.utc)

    job_submitted = False
    start_event: Event | None = None
    try:
        with open_db(settings.config.db_path, owner_uid=settings.config.out_uid) as conn:
            init_db(conn)
            candidate = _auto_update_candidate(
                conn,
                settings,
                effective_config_loader=effective_config_loader,
                now_utc=now_utc,
                started_at=started_at_utc,
                last_evaluated_at=_auto_update_evaluated_at(app.state),
            )
            if candidate is None:
                app.state.web_auto_update_evaluated_at = now_utc
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
            except Exception:
                try:
                    with _immediate_transaction(conn):
                        _release_auto_update_schedule_runs(conn, selection)
                except Exception:
                    LOGGER.exception(
                        "failed to release auto update schedule reservation"
                    )
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
    except _AutoUpdateCheckIncomplete:
        # Leave the evaluated time unchanged so the slot stays due.
        return None
    except Exception:
        if job_submitted and start_event is not None:
            start_event.set()
        raise


def _auto_update_evaluated_at(state: Any) -> datetime | None:
    evaluated_at = getattr(state, "web_auto_update_evaluated_at", None)
    return evaluated_at if isinstance(evaluated_at, datetime) else None


def _auto_update_candidate(
    conn: sqlite3.Connection,
    settings: WebSettings,
    *,
    effective_config_loader: EffectiveConfigLoader,
    now_utc: datetime,
    started_at: datetime,
    last_evaluated_at: datetime | None = None,
) -> tuple[
    AutoUpdateSelection,
    DryRunPlan,
    web_pending_sources.PendingSourceResult,
] | None:
    policies = _due_auto_update_policies(
        conn,
        settings,
        now_utc=now_utc,
        started_at=started_at,
        last_evaluated_at=last_evaluated_at,
    )
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
        raise _AutoUpdateCheckIncomplete

    pending_service_keys = _pending_service_keys(grouping)
    dependency_snoozes = active_dependency_snooze_rows(
        conn,
        service_keys=pending_service_keys,
    )
    selection = _auto_update_selection(
        settings,
        grouping,
        policies,
        dependency_snoozes=dependency_snoozes,
    )
    if selection is None:
        if pending_source.degraded and not pending_source.exists:
            # WUD's container metadata was unavailable, so the source is empty
            # and nothing was checked. A source degraded only by individual
            # containers still counts as a full check, so one container WUD
            # cannot scan does not hold every idle slot for the late limit.
            raise _AutoUpdateCheckIncomplete
        return None

    plan = _build_auto_update_plan(
        settings,
        selection.line_numbers,
        update_mode_override=selection.update_mode,
        base_config=effective_config,
        known_digest_provenance_by_service=known_digest_provenance_by_service,
        pending_source=pending_source,
    )
    if not plan_can_apply(plan, settings):
        return None
    return selection, plan, pending_source


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
    last_evaluated_at: datetime | None = None,
) -> dict[str, AutoUpdatePolicy]:
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
    for row in rows:
        days = _auto_update_days_from_row(row)
        update_time = str(row["auto_update_time"])
        try:
            parsed_time = datetime_time.fromisoformat(update_time)
        except ValueError:
            continue
        occurrence = _auto_update_latest_occurrence(
            local_now=local_now,
            parsed_time=parsed_time,
            days=days,
            now_utc=now_utc,
            tz=tz,
        )
        if occurrence is None:
            continue
        scheduled_local, scheduled_for = occurrence
        window_end = scheduled_for + timedelta(seconds=AUTO_UPDATE_GRACE_SECONDS)
        if started_at >= window_end:
            continue
        service_key = str(row["service_key"])
        schedule_key = _auto_update_schedule_key(
            service_key,
            local_date=scheduled_local.date().isoformat(),
            update_time=update_time,
            timezone_name=settings.config.timezone_name,
        )
        if _auto_update_schedule_recorded(conn, schedule_key):
            continue
        saved_after_slot = str(row["updated_at"]) > scheduled_for.isoformat()
        if saved_after_slot and now_utc >= window_end:
            # A policy saved after the slot's time may not have scheduled that
            # slot, so it may run only inside the slot's grace window. Any
            # policy edit updates updated_at, so an edit while a slot is held
            # late also drops that slot here without a missed row; it fails
            # closed and the next scheduled slot runs normally.
            continue
        policy = AutoUpdatePolicy(
            service_key=service_key,
            update_mode=str(row["update_mode"] or settings.config.update_mode),
            auto_update_time=update_time,
            auto_update_days=days,
            schedule_key=schedule_key,
            scheduled_for=scheduled_for,
        )
        missed_reason = _auto_update_missed_reason(
            scheduled_for,
            now_utc=now_utc,
            last_evaluated_at=last_evaluated_at,
        )
        if missed_reason is not None:
            _record_missed_auto_update_slot(conn, settings, policy, missed_reason)
            continue
        if active_snooze(conn, service_key=service_key, now=now_text) is not None:
            continue
        policies[service_key] = policy
    return policies


def _auto_update_latest_occurrence(
    *,
    local_now: datetime,
    parsed_time: datetime_time,
    days: Sequence[str],
    now_utc: datetime,
    tz: ZoneInfo,
) -> tuple[datetime, datetime] | None:
    """Return the most recent slot at or before now, from today or yesterday."""
    candidate_dates = (
        local_now.date(),
        (local_now - timedelta(days=1)).date(),
    )
    for local_date in candidate_dates:
        scheduled_local = datetime.combine(local_date, parsed_time, tzinfo=tz)
        day = AUTO_UPDATE_DAYS[scheduled_local.weekday()]
        if day not in days:
            continue
        scheduled_for = scheduled_local.astimezone(timezone.utc)
        if scheduled_for > now_utc:
            continue
        return scheduled_local, scheduled_for
    return None


def _auto_update_missed_reason(
    scheduled_for: datetime,
    *,
    now_utc: datetime,
    last_evaluated_at: datetime | None,
) -> str | None:
    """Return why a past slot will no longer run, or None while it is due.

    A slot is due during its grace window. After that it stays due only while
    no tick has finished checking it since its scheduled time, because a job
    was running or the check failed, and for at most
    AUTO_UPDATE_MAX_LATE_SECONDS.
    """
    if now_utc < scheduled_for + timedelta(seconds=AUTO_UPDATE_GRACE_SECONDS):
        return None
    if last_evaluated_at is not None and last_evaluated_at >= scheduled_for:
        return AUTO_UPDATE_MISSED_IDLE_REASON
    if now_utc >= scheduled_for + timedelta(seconds=AUTO_UPDATE_MAX_LATE_SECONDS):
        return AUTO_UPDATE_MISSED_LATE_REASON
    return None


def _record_missed_auto_update_slot(
    conn: sqlite3.Connection,
    settings: WebSettings,
    policy: AutoUpdatePolicy,
    reason: str,
) -> None:
    now = utc_timestamp()
    metadata = {
        "source": "webui-auto",
        "service_keys": [policy.service_key],
        "scheduled_for": policy.scheduled_for.isoformat(),
        "timezone": settings.config.timezone_name,
        "update_mode": policy.update_mode,
        "status": "missed",
        "reason": reason,
    }
    with _immediate_transaction(conn):
        conn.execute(
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
    return AutoUpdateSelection(
        line_numbers=line_numbers,
        service_keys=tuple(sorted(services_by_mode[mode])),
        schedule_keys=tuple(sorted(schedules_by_mode[mode])),
        scheduled_for=scheduled_for_by_mode[mode],
        update_mode=mode,
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
