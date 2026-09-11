"""Restart-safe storage for last-known-good WUD pending observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .db import init_db, open_db, utc_timestamp

WudContainerIdentity = tuple[str, str, str, str, str, str, str]
PENDING_OBSERVATIONS_SETTING_KEY = "wud.pending_observations"


@dataclass(frozen=True)
class StoredPendingObservation:
    identity: WudContainerIdentity
    observation: Mapping[str, object]
    observed_at: str


def source_key(normalized_base_url: str) -> str:
    """Return a stable, non-reversible key without persisting the API URL."""

    return hashlib.sha256(normalized_base_url.encode("utf-8")).hexdigest()


def load_pending_observations(
    db_path: str | Path,
    *,
    source: str,
    owner_uid: int | None = None,
) -> tuple[StoredPendingObservation, ...]:
    if str(db_path) == ":memory:":
        return ()
    with open_db(db_path, owner_uid=owner_uid) as conn:
        init_db(conn)
        row = conn.execute(
            """
            SELECT value
            FROM web_settings
            WHERE key = ?
            """,
            (PENDING_OBSERVATIONS_SETTING_KEY,),
        ).fetchone()
    if row is None:
        return ()

    try:
        payload = json.loads(str(row["value"]))
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict) or payload.get("source") != source:
        return ()
    observations = payload.get("observations")
    if not isinstance(observations, list):
        return ()
    return tuple(
        stored
        for raw in observations
        if (stored := _stored_observation(raw)) is not None
    )


def replace_pending_observations(
    db_path: str | Path,
    *,
    source: str,
    observations: Sequence[StoredPendingObservation],
    owner_uid: int | None = None,
) -> None:
    if str(db_path) == ":memory:":
        return
    value = json.dumps(
        {
            "source": source,
            "observations": [
                {
                    "identity": item.identity,
                    "observation": dict(item.observation),
                    "observed_at": item.observed_at,
                }
                for item in observations
            ],
        }
    )
    with open_db(db_path, owner_uid=owner_uid) as conn:
        init_db(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO web_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (PENDING_OBSERVATIONS_SETTING_KEY, value, utc_timestamp()),
            )


def _stored_observation(raw: object) -> StoredPendingObservation | None:
    if not isinstance(raw, dict):
        return None
    identity = raw.get("identity")
    observation = raw.get("observation")
    observed_at = raw.get("observed_at")
    if (
        not isinstance(identity, list)
        or len(identity) != 7
        or not all(isinstance(item, str) for item in identity)
        or not isinstance(observation, dict)
        or not isinstance(observed_at, str)
        or not observed_at
    ):
        return None
    return StoredPendingObservation(
        identity=cast(WudContainerIdentity, tuple(identity)),
        observation=observation,
        observed_at=observed_at,
    )
