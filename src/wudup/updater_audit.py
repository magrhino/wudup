"""Audit database helpers for ``update-from-wud``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import compose_rewrite
from .db import (
    DatabaseError,
    active_tag_exclusion_rules,
    connect_db,
    init_db,
    insert_pending_update,
    insert_update_event,
    insert_update_run,
    update_pending_update,
    upsert_known_image,
    upsert_tag_exclusion_rule,
)
from .db import (
    utc_timestamp as db_utc_timestamp,
)
from .digest_provenance import (
    DigestTagProvenance,
    digest_from_image,
    digest_provenance_from_unpin_update,
    digest_provenance_from_update,
)
from .file_ops import (
    OwnerConfig,
    OwnerConfigError,
)
from .file_ops import (
    apply_configured_owner as _apply_configured_owner,
)
from .images import image_repo_ref
from .naming import DB_FILENAME
from .updater_digest_pin import _digest_pin_match_tag
from .updater_matching import (
    _failed_line_numbers,
    _failed_match_for_line,
    _first_match_by_line,
    _line_status_reason,
    _service_key,
    _stacks_to_update,
)
from .updater_models import (
    STALE_PENDING_DIGEST_REASON,
    FailureRecord,
    ImageState,
    Match,
    StackStatus,
    TagExclusionUpdate,
    UpdaterError,
    UpdaterOptions,
)
from .updater_planning import _first_tag_exclusion_by_line
from .wud_file import ParsedWudFile, WudTarget, parse_wud_file

ApplyOwner = Callable[[str | Path, OwnerConfig | None], None]
InsertUpdateEvent = Callable[..., Any]


def audit_parsed_file(
    runner: Any,
    parsed: ParsedWudFile,
    audit_lines: Sequence[int],
) -> ParsedWudFile:
    target_lines = {target.line_no for target in parsed.targets}
    if set(audit_lines).issubset(target_lines):
        return parsed
    audit_parsed = parse_wud_file(
        runner.options.wud_file,
        selected_lines=sorted(target_lines | set(audit_lines)),
    )
    return runner._apply_tag_overrides(audit_parsed, log=False)


def existing_exact_tag_exclusions(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
) -> dict[str, set[str]]:
    existing: dict[str, set[str]] = {}
    if runner.audit_conn is None:
        return existing
    for update in updates:
        rows = active_tag_exclusion_rules(
            runner.audit_conn,
            image_repo=update.image_repo,
            service_key=update.service_key,
        )
        tags = existing.setdefault(update.service, set())
        tags.update(str(row["tag"]) for row in rows)
    return existing


def record_tag_exclusion_rules(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
) -> None:
    if runner.audit_conn is None:
        return
    seen: set[tuple[str, str, str, str]] = set()
    for update in updates:
        service_key = "" if update.scope == "image_repo" else update.service_key
        key = (update.scope, update.image_repo, service_key, update.tag)
        if key in seen:
            continue
        seen.add(key)
        upsert_tag_exclusion_rule(
            runner.audit_conn,
            scope=update.scope,
            image_repo=update.image_repo,
            service_key=service_key,
            tag=update.tag,
            regex_fragment=compose_rewrite.js_regex_escape(update.tag),
        )


def mark_tag_exclusions_pending(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
) -> None:
    if runner.audit_conn is None or runner.audit_run_id is None:
        return
    for line_no, update in _first_tag_exclusion_by_line(updates).items():
        update_pending_update(
            runner.audit_conn,
            run_id=runner.audit_run_id,
            line_no=line_no,
            status="in_progress",
            status_reason="tag-exclusion",
            service_key=update.service_key,
            stack_name=update.stack.name,
            service_name=update.service,
        )


def mark_successful_tag_exclusions(
    runner: Any,
    updates: Sequence[TagExclusionUpdate],
    statuses: Mapping[int, StackStatus],
) -> None:
    if runner.audit_conn is None or runner.audit_run_id is None:
        return
    for line_no, update in _first_tag_exclusion_by_line(updates).items():
        status = statuses.get(line_no, StackStatus("failure", "missing"))
        update_pending_update(
            runner.audit_conn,
            run_id=runner.audit_run_id,
            line_no=line_no,
            status="resolved" if status.status == "success" else "failed",
            status_reason=status.reason,
            service_key=update.service_key,
            stack_name=update.stack.name,
            service_name=update.service,
        )


def mark_tag_exclusion_failures(
    runner: Any,
    failures: Sequence[tuple[WudTarget, str]],
) -> None:
    if runner.audit_conn is None or runner.audit_run_id is None:
        return
    for target, reason in failures:
        update_pending_update(
            runner.audit_conn,
            run_id=runner.audit_run_id,
            line_no=target.line_no,
            status="failed",
            status_reason=f"tag-exclusion-{reason}",
        )


def start_audit(
    runner: Any,
    parsed: ParsedWudFile,
) -> None:
    if runner.audit_run_id is not None:
        return
    conn: sqlite3.Connection | None = None
    try:
        audit_db_path = db_path(runner.options, runner.environ)
        chown_parent = sqlite_parent_missing(audit_db_path)
        conn = connect_db(audit_db_path)
        runner.audit_db_path = audit_db_path
        init_db(conn)
        runner.audit_conn = conn
        runner.audit_run_id = insert_update_run(
            conn,
            status="started",
            dry_run=False,
            mode=runner.options.mode,
            wud_file=runner.options.wud_file_label or str(runner.options.wud_file),
            log_file=str(runner.log_file),
            metadata_json=runner.options.metadata_json,
        )
        for target in parsed.targets:
            insert_pending_update(
                conn,
                run_id=runner.audit_run_id,
                line_no=target.line_no,
                raw=target.raw,
                image=target.first,
                target_digest=target.digest,
                desired_tag=target.desired_tag,
            )
        apply_sqlite_owner(
            audit_db_path,
            runner.owner,
            chown_parent=chown_parent,
        )
    except (OSError, sqlite3.Error, DatabaseError, OwnerConfigError) as exc:
        if runner.audit_conn is not None and runner.audit_run_id is not None:
            finish_audit_run(runner, "failure", best_effort=True)
        if conn is not None:
            conn.close()
        runner.audit_conn = None
        runner.audit_run_id = None
        runner.audit_db_path = None
        raise UpdaterError(f"Could not initialize audit database: {exc}") from exc


def finish_audit_run(
    runner: Any,
    status: str,
    *,
    best_effort: bool = False,
) -> None:
    if runner.audit_conn is None or runner.audit_run_id is None:
        return
    try:
        with runner.audit_conn:
            runner.audit_conn.execute(
                """
                UPDATE update_runs
                SET status = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (status, db_utc_timestamp(), runner.audit_run_id),
            )
    except sqlite3.Error:
        if best_effort:
            return
        raise


def mark_unmatched_pending(
    runner: Any,
    parsed: ParsedWudFile,
    matches: Sequence[Match],
    skipped_tags: Sequence[WudTarget],
) -> None:
    if runner.audit_conn is None or runner.audit_run_id is None:
        return
    matched_lines = {match.target.line_no for match in matches}
    skipped_lines = {target.line_no for target in skipped_tags}
    for target in parsed.targets:
        if target.line_no in matched_lines:
            continue
        reason = "tag-update-disabled" if target.line_no in skipped_lines else "unmatched"
        update_pending_update(
            runner.audit_conn,
            run_id=runner.audit_run_id,
            line_no=target.line_no,
            status="pending",
            status_reason=reason,
        )


def mark_matched_pending(
    runner: Any,
    matches: Sequence[Match],
    *,
    status: str,
    status_reason: str = "matched",
) -> None:
    if runner.audit_conn is None or runner.audit_run_id is None:
        return
    provenance_by_line = _planned_digest_provenance_by_line(runner, matches)
    for line_no, match in _first_match_by_line(matches).items():
        update_pending_update(
            runner.audit_conn,
            run_id=runner.audit_run_id,
            line_no=line_no,
            status=status,
            status_reason=status_reason,
            service_key=_service_key(match),
            stack_name=match.stack.name,
            service_name=match.service,
            digest_provenance=provenance_by_line.get(line_no),
        )


def mark_removed_pending(
    runner: Any,
    parsed: ParsedWudFile,
    remove_lines: Iterable[int],
    matches: Sequence[Match],
) -> None:
    if runner.audit_conn is None or runner.audit_run_id is None:
        return
    matched_lines = {match.target.line_no for match in matches}
    removed_lines = set(remove_lines) - matched_lines
    for target in parsed.targets:
        if target.line_no not in removed_lines:
            continue
        update_pending_update(
            runner.audit_conn,
            run_id=runner.audit_run_id,
            line_no=target.line_no,
            status="resolved",
            status_reason="removed-before-run",
        )


def mark_failed_pending(
    runner: Any,
    matches: Sequence[Match],
    stack_statuses: Mapping[int, StackStatus],
    failed_lines: Iterable[int],
) -> None:
    if runner.audit_conn is None or runner.audit_run_id is None:
        return
    failed = list(dict.fromkeys(failed_lines))
    stale_lines = runner._stale_pending_digest_line_numbers(matches, failed)
    for line_no in failed:
        match = _failed_match_for_line(line_no, matches, stack_statuses)
        if match is None:
            continue
        if line_no in runner.preflight_skipped_pending_line_numbers:
            reason = "preflight-skipped"
        elif line_no in stale_lines:
            reason = STALE_PENDING_DIGEST_REASON
        else:
            outcome = runner._expected_digest_outcome(match)
            if outcome == "failed":
                reason = "expected-digest-not-reached"
            elif runner._expected_digest_failed_in_stack(match.stack):
                reason = "expected-digest-sibling-failed"
            else:
                reason = _line_status_reason(line_no, matches, stack_statuses)
        update_pending_update(
            runner.audit_conn,
            run_id=runner.audit_run_id,
            line_no=line_no,
            status="failed",
            status_reason=reason,
            service_key=_service_key(match),
            stack_name=match.stack.name,
            service_name=match.service,
        )


def mark_successful_pending(
    runner: Any,
    matches: Sequence[Match],
    stack_statuses: Mapping[int, StackStatus],
) -> None:
    if runner.audit_conn is None or runner.audit_run_id is None:
        return
    failed = set(_failed_line_numbers(matches, stack_statuses))
    for line_no, match in _first_match_by_line(matches).items():
        if line_no in failed:
            continue
        reason = _line_status_reason(line_no, matches, stack_statuses)
        digest_provenance = _applied_digest_provenance_for_match(runner, match)
        update_pending_update(
            runner.audit_conn,
            run_id=runner.audit_run_id,
            line_no=line_no,
            status="resolved",
            status_reason=reason,
            service_key=_service_key(match),
            stack_name=match.stack.name,
            service_name=match.service,
            digest_provenance=digest_provenance,
        )


def record_update_events(
    runner: Any,
    matches: Sequence[Match],
    stack_statuses: Mapping[int, StackStatus],
    *,
    insert_event: InsertUpdateEvent = insert_update_event,
) -> None:
    if runner.audit_conn is None or runner.audit_run_id is None:
        return
    for match in matches:
        status = _event_status_for_match(runner, match, stack_statuses)
        digest_provenance = _digest_provenance_for_event(runner, match)
        target_image = runner._target_image_for_match(match)
        old_state, new_state = _image_states_for_match(runner, match, target_image)
        metadata = _event_metadata_for_match(runner, match, status)
        insert_event(
            runner.audit_conn,
            run_id=runner.audit_run_id,
            service_name=match.service,
            stack_name=match.stack.name,
            image=match.compose_image,
            target_image=target_image,
            old_image_id="" if old_state is None else old_state.image_id,
            new_image_id="" if new_state is None else new_state.image_id,
            old_digest=_old_event_digest(match, digest_provenance, old_state),
            new_digest=_new_event_digest(digest_provenance, new_state),
            status=status.status,
            metadata_json=json.dumps(metadata, sort_keys=True),
            digest_provenance=digest_provenance,
        )


def _event_status_for_match(
    runner: Any,
    match: Match,
    stack_statuses: Mapping[int, StackStatus],
) -> StackStatus:
    status = stack_statuses.get(
        match.stack.index,
        StackStatus("failure", "missing"),
    )
    reason = {
        "stale": STALE_PENDING_DIGEST_REASON,
        "failed": "manifest-integrity-mismatch",
    }.get(runner._preflight_expected_digest_outcome(match))
    if reason:
        return StackStatus("failure", reason)
    if match.target.line_no in runner.preflight_skipped_pending_line_numbers:
        return StackStatus("failure", "preflight-skipped")
    if status.status != "failure" or not runner._expected_digest_failed_in_stack(
        match.stack
    ):
        return status
    outcome = runner._expected_digest_outcome(match)
    if outcome == "failed":
        return StackStatus("failure", "expected-digest-not-reached")
    if outcome == "stale":
        return StackStatus("failure", STALE_PENDING_DIGEST_REASON)
    return StackStatus("failure", "expected-digest-sibling-failed")


def record_known_images(
    runner: Any,
    matches: Sequence[Match],
    stack_statuses: Mapping[int, StackStatus],
) -> None:
    if runner.audit_conn is None:
        return
    for match in matches:
        status = stack_statuses.get(match.stack.index)
        if status is None or status.status != "success":
            continue
        image = runner._target_image_for_match(match)
        digest_provenance = _applied_digest_provenance_for_match(runner, match)
        upsert_known_image(
            runner.audit_conn,
            service_key=_service_key(match),
            image=image,
            image_id=runner.docker.image_id(image),
            digest=runner.docker.image_digest(image),
            digest_provenance=digest_provenance,
        )


def _planned_digest_provenance_by_line(
    runner: Any,
    matches: Sequence[Match],
) -> dict[int, DigestTagProvenance]:
    by_line: dict[int, DigestTagProvenance] = {}
    for match in matches:
        update = _planned_digest_unpin_for_match(runner, match)
        if update is None:
            continue
        by_line.setdefault(
            match.target.line_no,
            digest_provenance_from_unpin_update(
                update,
                provenance_source="plan",
                provenance_confidence="recovered",
            ),
        )
    if not runner.options.digest_pin_updates:
        return by_line
    for stack in _stacks_to_update(matches):
        stack_matches = [
            match for match in matches if match.stack.index == stack.index
        ]
        updates = runner._digest_pin_updates(stack_matches)
        by_key = {
            (update.old_image, update.resolved_tag): update
            for update in updates
        }
        for match in stack_matches:
            resolved_tag = _digest_pin_match_tag(match)
            if not resolved_tag:
                continue
            update = by_key.get((match.compose_image, resolved_tag))
            if update is None:
                continue
            by_line.setdefault(
                match.target.line_no,
                digest_provenance_from_update(
                    update,
                    provenance_source="plan",
                    provenance_confidence="verified",
                ),
            )
    return by_line


def _applied_digest_provenance_for_match(
    runner: Any,
    match: Match,
) -> DigestTagProvenance | None:
    unpin = runner.applied_digest_unpins.get(
        (match.stack.index, match.target.line_no, match.service)
    )
    if unpin is not None:
        return digest_provenance_from_unpin_update(
            unpin,
            provenance_source="apply",
            provenance_confidence="verified",
        )
    update = runner.applied_digest_pins.get(
        (match.stack.index, match.target.line_no, match.service)
    )
    if update is None:
        return None
    return digest_provenance_from_update(
        update,
        provenance_source="apply",
        provenance_confidence="verified",
    )


def _event_metadata_for_match(
    runner: Any,
    match: Match,
    status: StackStatus,
) -> dict[str, Any]:
    metadata = {"reason": status.reason}
    metadata.update(_runtime_metadata_for_match(runner, match, status))
    failure = _failure_for_match(runner, match, status)
    if failure is None:
        return metadata
    metadata["failure_phase"] = failure.phase
    health_evidence = _health_evidence(failure)
    if health_evidence:
        metadata["health_evidence"] = health_evidence
    return metadata


def _runtime_metadata_for_match(
    runner: Any,
    match: Match,
    status: StackStatus,
) -> dict[str, Any]:
    runtime_state = runner.stack_runtime_states.get(match.stack.index)
    if runtime_state is None:
        return {}

    metadata: dict[str, Any] = {}
    stopped_services = runtime_state[1]
    verified_after = runner.stack_runtime_states_after.get(match.stack.index)
    after_running, after_stopped = verified_after or ((), ())
    if stopped_services:
        metadata["stopped_services_before"] = list(stopped_services)
        if verified_after is not None:
            metadata["stopped_services_after"] = list(after_stopped)
        elif status.status == "success":
            metadata["stopped_services_after"] = list(stopped_services)
    if match.service not in stopped_services:
        return metadata

    metadata["runtime_state_before"] = "not-running"
    if match.service in after_stopped:
        metadata["runtime_state_after"] = "not-running"
    elif match.service in after_running:
        metadata["runtime_state_after"] = "running"
    elif status.status == "success" and verified_after is None:
        metadata["runtime_state_after"] = "not-running"
    else:
        metadata["runtime_state_after"] = "unknown"
    return metadata


def _old_event_digest(
    match: Match,
    digest_provenance: DigestTagProvenance | None,
    old_state: ImageState | None,
) -> str:
    old_digest = old_state.digest if old_state is not None else ""
    if old_digest:
        return old_digest
    if digest_provenance is not None:
        return digest_from_image(match.compose_image)
    return ""


def _new_event_digest(
    digest_provenance: DigestTagProvenance | None,
    new_state: ImageState | None,
) -> str:
    if digest_provenance is not None:
        return digest_provenance.target_digest
    return new_state.digest if new_state is not None else ""


def _failure_for_match(
    runner: Any,
    match: Match,
    status: StackStatus,
) -> FailureRecord | None:
    for failure in reversed(runner.failures):
        if failure.reason != status.reason:
            continue
        if match in failure.matches:
            return failure
    return None


def _health_evidence(failure: FailureRecord) -> str:
    if not failure.health_details:
        return ""
    if "docker compose ps -q returned no containers" in failure.health_details:
        return "service_disappeared"
    if failure.reason == "health-failed":
        return "timed_out"
    return ""


def _image_states_for_match(
    runner: Any,
    match: Match,
    target_image: str,
) -> tuple[ImageState | None, ImageState | None]:
    states = runner.stack_image_states.get(match.stack.index)
    if states is None:
        return None, None
    before, after = states
    old_state = _image_state_for_reference(before, match.compose_image)
    new_state = (
        _image_state_for_reference(after, target_image)
        or _image_state_for_reference(after, match.resolved)
        or _image_state_for_reference(after, match.compose_image)
    )
    return old_state, new_state


def _image_state_for_reference(
    states: Mapping[str, ImageState],
    image: str,
) -> ImageState | None:
    if not image:
        return None
    state = states.get(image)
    if state is not None:
        return state
    repository = image_repo_ref(image)
    for candidate, candidate_state in states.items():
        if image_repo_ref(candidate) == repository:
            return candidate_state
    return None


def _planned_digest_unpin_for_match(runner: Any, match: Match) -> Any | None:
    for update in runner.options.digest_unpin_plan:
        if update.old_image != match.compose_image:
            continue
        if update.tag_image != match.resolved:
            continue
        if update.target_digest != match.target.digest:
            continue
        if match.service and match.service not in update.services:
            continue
        return update
    return None


def _digest_provenance_for_event(
    runner: Any,
    match: Match,
) -> DigestTagProvenance | None:
    applied = _applied_digest_provenance_for_match(runner, match)
    if applied is not None:
        return applied
    return _planned_digest_provenance_by_line(runner, (match,)).get(
        match.target.line_no
    )


def db_path(options: UpdaterOptions, environ: Mapping[str, str]) -> Path:
    configured = environ.get("WUD_DB_PATH")
    if configured:
        return Path(configured)
    if options.db_path is not None:
        return options.db_path
    return options.log_dir / DB_FILENAME


def sqlite_parent_missing(db_path: Path) -> bool:
    return str(db_path) != ":memory:" and not db_path.parent.exists()


def apply_sqlite_owner(
    db_path: Path,
    owner: OwnerConfig,
    *,
    chown_parent: bool = False,
    apply_owner: ApplyOwner = _apply_configured_owner,
) -> None:
    if not owner.configured or str(db_path) == ":memory:":
        return
    if chown_parent and db_path.parent.exists():
        apply_owner(db_path.parent, owner)
    for path in sqlite_state_paths(db_path):
        if path.exists():
            apply_owner(path, owner)


def sqlite_state_paths(db_path: Path) -> tuple[Path, ...]:
    return (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        Path(f"{db_path}-journal"),
    )
