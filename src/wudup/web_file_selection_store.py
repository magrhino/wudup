"""Restart-safe completion state for partial file-backed WebUI updates."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Collection, Sequence
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from .db import init_db, open_db, utc_timestamp
from .plan_matching import pending_target_key
from .updater_models import CompletedUpdateSelection
from .wud_file import parse_wud_text

FILE_SELECTIONS_SETTING_KEY = "wud.file_selection_completions"
_READ_SELECTIONS_ERROR = "Could not read partial update completion state."
LOGGER = logging.getLogger(__name__)


class FileSelectionStoreError(OSError):
    """Raised when stored partial-update completion state is unreadable."""


class FileSelectionCheckpointError(RuntimeError):
    """Raised when partial file-backed completion state cannot be persisted."""


def load_completed_update_selections(
    db_path: str | Path,
    *,
    pending_file: Path,
    pending_target_keys: Collection[str],
) -> tuple[CompletedUpdateSelection, ...]:
    if str(db_path) == ":memory:":
        raise FileSelectionStoreError(_READ_SELECTIONS_ERROR)
    pending_file_key = _pending_file_key(pending_file)
    path = Path(db_path)
    if not path.is_file():
        return ()
    try:
        with closing(
            sqlite3.connect(
                f"file:{quote(str(path), safe='/')}?mode=ro",
                uri=True,
            )
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute(
                """
                SELECT value
                FROM web_settings
                WHERE key = ?
                """,
                (FILE_SELECTIONS_SETTING_KEY,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise FileSelectionStoreError(_READ_SELECTIONS_ERROR) from exc
    if row is None:
        return ()

    try:
        payload = json.loads(str(row["value"]))
    except json.JSONDecodeError as exc:
        raise FileSelectionStoreError(_READ_SELECTIONS_ERROR) from exc
    if not isinstance(payload, dict):
        raise FileSelectionStoreError(_READ_SELECTIONS_ERROR)
    if payload.get("version") != 1:
        raise FileSelectionStoreError(_READ_SELECTIONS_ERROR)
    stored_pending_file_key = payload.get("pending_file_key")
    if not isinstance(stored_pending_file_key, str):
        raise FileSelectionStoreError(_READ_SELECTIONS_ERROR)
    if stored_pending_file_key != pending_file_key:
        return ()
    raw_items = payload.get("selections")
    if not isinstance(raw_items, list):
        raise FileSelectionStoreError(_READ_SELECTIONS_ERROR)
    selections: list[CompletedUpdateSelection] = []
    for raw in raw_items:
        item = _completed_update_selection(raw)
        if item is None:
            raise FileSelectionStoreError(_READ_SELECTIONS_ERROR)
        if item.target_key in pending_target_keys:
            selections.append(item)
    return tuple(selections)


def replace_completed_update_selections(
    db_path: str | Path,
    *,
    pending_file: Path,
    selections: Sequence[CompletedUpdateSelection],
    owner_uid: int | None = None,
) -> None:
    if str(db_path) == ":memory:":
        raise FileSelectionStoreError(
            "Could not persist partial update completion state."
        )
    unique = {
        (item.target_key, item.completion_id): item
        for item in selections
        if item.target_key and item.completion_id
    }
    with open_db(db_path, owner_uid=owner_uid) as conn:
        init_db(conn)
        with conn:
            if not unique:
                conn.execute(
                    "DELETE FROM web_settings WHERE key = ?",
                    (FILE_SELECTIONS_SETTING_KEY,),
                )
                return
            value = json.dumps(
                {
                    "version": 1,
                    "pending_file_key": _pending_file_key(pending_file),
                    "selections": [
                        {
                            "target_key": item.target_key,
                            "completion_id": item.completion_id,
                        }
                        for _key, item in sorted(unique.items())
                    ],
                },
                sort_keys=True,
            )
            conn.execute(
                """
                INSERT INTO web_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (FILE_SELECTIONS_SETTING_KEY, value, utc_timestamp()),
            )


def checkpoint_completed_update_selections(
    db_path: str | Path,
    *,
    pending_file: Path,
    scoped: bool,
    previous: Sequence[CompletedUpdateSelection],
    successful: Sequence[CompletedUpdateSelection],
    discovered: Sequence[CompletedUpdateSelection],
    owner_uid: int | None = None,
) -> None:
    try:
        text = pending_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        _raise_checkpoint_error(exc)

    try:
        pending_target_keys = {
            pending_target_key(target.raw)
            for target in parse_wud_text(text).targets
        }
        retained = retained_completed_update_selections(
            pending_target_keys=pending_target_keys,
            previous=(
                previous
                if scoped
                else load_completed_update_selections(
                    db_path,
                    pending_file=pending_file,
                    pending_target_keys=pending_target_keys,
                )
            ),
            successful=successful if scoped else (),
            discovered=discovered,
            reset_target_keys=(
                set()
                if scoped
                else {item.target_key for item in discovered}
            ),
        )
        replace_completed_update_selections(
            db_path,
            pending_file=pending_file,
            selections=retained,
            owner_uid=owner_uid,
        )
    except Exception as exc:  # noqa: BLE001 - expose only the sanitized failure.
        _raise_checkpoint_error(exc)


def retained_completed_update_selections(
    *,
    pending_target_keys: set[str],
    previous: Sequence[CompletedUpdateSelection],
    successful: Sequence[CompletedUpdateSelection],
    discovered: Sequence[CompletedUpdateSelection],
    reset_target_keys: set[str] | None = None,
) -> tuple[CompletedUpdateSelection, ...]:
    reset = reset_target_keys or set()
    discovered_items = {
        (item.target_key, item.completion_id): item for item in discovered
    }
    discovered_target_keys = {
        item.target_key for item in discovered_items.values()
    }
    completed: dict[tuple[str, str], CompletedUpdateSelection] = {}
    for item in previous:
        key = (item.target_key, item.completion_id)
        if item.target_key not in pending_target_keys or item.target_key in reset:
            continue
        if item.target_key in discovered_target_keys and key not in discovered_items:
            continue
        completed[key] = item
    for item in successful:
        key = (item.target_key, item.completion_id)
        if (
            item.target_key in pending_target_keys
            and item.target_key not in reset
            and key in discovered_items
        ):
            completed[key] = item
    completed_ids_by_target: dict[str, set[str]] = {}
    for item in completed.values():
        completed_ids_by_target.setdefault(item.target_key, set()).add(
            item.completion_id
        )
    discovered_ids_by_target: dict[str, set[str]] = {}
    for item in discovered_items.values():
        discovered_ids_by_target.setdefault(item.target_key, set()).add(
            item.completion_id
        )
    fully_completed_target_keys = {
        target_key
        for target_key, discovered_ids in discovered_ids_by_target.items()
        if target_key not in reset
        and discovered_ids
        and discovered_ids <= completed_ids_by_target.get(target_key, set())
    }
    return tuple(
        item
        for (target_key, _completion_id), item in sorted(completed.items())
        if target_key not in fully_completed_target_keys
    )


def _pending_file_key(pending_file: Path) -> str:
    return hashlib.sha256(
        str(pending_file.resolve(strict=False)).encode("utf-8")
    ).hexdigest()


def _completed_update_selection(raw: object) -> CompletedUpdateSelection | None:
    if not isinstance(raw, dict):
        return None
    target_key = raw.get("target_key")
    completion_id = raw.get("completion_id")
    if (
        not isinstance(target_key, str)
        or not target_key
        or not isinstance(completion_id, str)
        or not completion_id
    ):
        return None
    return CompletedUpdateSelection(
        target_key=target_key,
        completion_id=completion_id,
    )


def _raise_checkpoint_error(exc: BaseException) -> None:
    LOGGER.error("File selection completion checkpoint failed")
    raise FileSelectionCheckpointError(
        "Could not persist partial update completion state."
    ) from exc
