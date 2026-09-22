"""SQLite schema and migration helpers for WUDup."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone

from .digest_provenance import DIGEST_PROVENANCE_SQL_COLUMNS

SCHEMA_VERSION = 14

ColumnSchema = tuple[str, str, int, str | None, int]
SchemaDefinition = dict[str, tuple[ColumnSchema, ...]]
Migration = Callable[[sqlite3.Connection], None]
SECURITY_SCAN_CACHE_FINDINGS_COLUMN: ColumnSchema = (
    "findings_json",
    "TEXT",
    1,
    "'[]'",
    0,
)
RELEASE_NOTE_BODY_COLUMN: ColumnSchema = ("body", "TEXT", 1, "''", 0)
RELEASE_NOTE_TARGET_DIGEST_COLUMN: ColumnSchema = (
    "target_digest",
    "TEXT",
    1,
    "''",
    0,
)
DIGEST_PROVENANCE_COLUMN_SCHEMA: tuple[ColumnSchema, ...] = tuple(
    (column, "TEXT", 1, "''", 0) for column in DIGEST_PROVENANCE_SQL_COLUMNS
)

EXPECTED_SCHEMA: SchemaDefinition = {
    "schema_migrations": (
        ("version", "INTEGER", 0, None, 1),
        ("name", "TEXT", 1, None, 0),
        ("applied_at", "TEXT", 1, None, 0),
    ),
    "update_runs": (
        ("id", "INTEGER", 0, None, 1),
        ("started_at", "TEXT", 1, None, 0),
        ("finished_at", "TEXT", 0, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("dry_run", "INTEGER", 1, "0", 0),
        ("mode", "TEXT", 1, "''", 0),
        ("wud_file", "TEXT", 1, "''", 0),
        ("log_file", "TEXT", 1, "''", 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
    ),
    "update_events": (
        ("id", "INTEGER", 0, None, 1),
        ("run_id", "INTEGER", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("service_name", "TEXT", 1, None, 0),
        ("stack_name", "TEXT", 1, "''", 0),
        ("image", "TEXT", 1, None, 0),
        ("target_image", "TEXT", 1, "''", 0),
        ("old_image_id", "TEXT", 1, "''", 0),
        ("new_image_id", "TEXT", 1, "''", 0),
        ("old_digest", "TEXT", 1, "''", 0),
        ("new_digest", "TEXT", 1, "''", 0),
        ("status", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
        *DIGEST_PROVENANCE_COLUMN_SCHEMA,
    ),
    "snoozes": (
        ("id", "INTEGER", 0, None, 1),
        ("service_key", "TEXT", 1, None, 0),
        ("snoozed_until", "TEXT", 1, None, 0),
        ("reason", "TEXT", 1, "''", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
    ),
    "dependency_snoozes": (
        ("id", "INTEGER", 0, None, 1),
        ("service_key", "TEXT", 1, None, 0),
        ("wait_for_service_key", "TEXT", 1, None, 0),
        ("reason", "TEXT", 1, "''", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
    ),
    "service_policy": (
        ("service_key", "TEXT", 0, None, 1),
        ("update_mode", "TEXT", 1, "''", 0),
        ("auto_update", "INTEGER", 1, "1", 0),
        ("snooze_default_seconds", "INTEGER", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
        ("auto_update_time", "TEXT", 0, None, 0),
        ("auto_update_days_json", "TEXT", 1, "'[]'", 0),
    ),
    "auto_update_schedule_runs": (
        ("schedule_key", "TEXT", 0, None, 1),
        ("service_key", "TEXT", 1, None, 0),
        ("scheduled_for", "TEXT", 1, None, 0),
        ("run_id", "INTEGER", 0, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
    ),
    "known_images": (
        ("service_key", "TEXT", 0, None, 1),
        ("image", "TEXT", 1, None, 0),
        ("image_id", "TEXT", 1, "''", 0),
        ("digest", "TEXT", 1, "''", 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
        *DIGEST_PROVENANCE_COLUMN_SCHEMA,
    ),
    "pending_updates": (
        ("id", "INTEGER", 0, None, 1),
        ("run_id", "INTEGER", 1, None, 0),
        ("line_no", "INTEGER", 1, None, 0),
        ("raw", "TEXT", 1, None, 0),
        ("image", "TEXT", 1, None, 0),
        ("target_digest", "TEXT", 1, "''", 0),
        ("desired_tag", "TEXT", 1, "''", 0),
        ("service_key", "TEXT", 1, "''", 0),
        ("stack_name", "TEXT", 1, "''", 0),
        ("service_name", "TEXT", 1, "''", 0),
        ("status", "TEXT", 1, None, 0),
        ("status_reason", "TEXT", 1, "''", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
        *DIGEST_PROVENANCE_COLUMN_SCHEMA,
    ),
    "release_note_cache": (
        ("cache_key", "TEXT", 0, None, 1),
        ("provider", "TEXT", 1, None, 0),
        ("image_repo", "TEXT", 1, "''", 0),
        ("upstream_repo", "TEXT", 1, "''", 0),
        ("current_tag", "TEXT", 1, "''", 0),
        ("target_tag", "TEXT", 1, "''", 0),
        ("status", "TEXT", 1, None, 0),
        ("release_tag", "TEXT", 1, "''", 0),
        ("title", "TEXT", 1, "''", 0),
        ("published_at", "TEXT", 1, "''", 0),
        ("breaking", "INTEGER", 1, "0", 0),
        ("breaking_reasons_json", "TEXT", 1, "'[]'", 0),
        ("links_json", "TEXT", 1, "'[]'", 0),
        ("error", "TEXT", 1, "''", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
        RELEASE_NOTE_BODY_COLUMN,
        RELEASE_NOTE_TARGET_DIGEST_COLUMN,
    ),
    "release_notification_history": (
        ("notification_key", "TEXT", 0, None, 1),
        ("mode", "TEXT", 1, "''", 0),
        ("status", "TEXT", 1, None, 0),
        ("last_attempted_at", "TEXT", 1, None, 0),
        ("last_sent_at", "TEXT", 1, "''", 0),
        ("send_count", "INTEGER", 1, "0", 0),
        ("last_audit_run_id", "INTEGER", 1, "0", 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
    ),
    "security_scan_cache": (
        ("cache_key", "TEXT", 0, None, 1),
        ("request_key", "TEXT", 1, None, 0),
        ("subject_id", "TEXT", 1, "''", 0),
        ("canonical_registry", "TEXT", 1, "''", 0),
        ("canonical_repository", "TEXT", 1, "''", 0),
        ("requested_ref", "TEXT", 1, "''", 0),
        ("reported_digest", "TEXT", 1, "''", 0),
        ("index_digest", "TEXT", 1, "''", 0),
        ("manifest_digest", "TEXT", 1, "''", 0),
        ("platform", "TEXT", 1, "''", 0),
        ("platform_os", "TEXT", 1, "''", 0),
        ("platform_architecture", "TEXT", 1, "''", 0),
        ("platform_variant", "TEXT", 1, "''", 0),
        ("platform_source", "TEXT", 1, "''", 0),
        ("identity_status", "TEXT", 1, "''", 0),
        ("state", "TEXT", 1, None, 0),
        ("verdict", "TEXT", 1, "''", 0),
        ("scanner", "TEXT", 1, "''", 0),
        ("scanner_version", "TEXT", 1, "''", 0),
        ("scanner_schema", "TEXT", 1, "''", 0),
        ("db_revision", "TEXT", 1, "''", 0),
        ("db_updated_at", "TEXT", 1, "''", 0),
        ("severity_counts_json", "TEXT", 1, "'{}'", 0),
        ("fixable_counts_json", "TEXT", 1, "'{}'", 0),
        ("unfixed_count", "INTEGER", 1, "0", 0),
        ("warnings_json", "TEXT", 1, "'[]'", 0),
        ("error_code", "TEXT", 1, "''", 0),
        ("error_message", "TEXT", 1, "''", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
        SECURITY_SCAN_CACHE_FINDINGS_COLUMN,
    ),
    "tag_exclusion_rules": (
        ("id", "INTEGER", 0, None, 1),
        ("scope", "TEXT", 1, None, 0),
        ("image_repo", "TEXT", 1, None, 0),
        ("service_key", "TEXT", 1, "''", 0),
        ("match_type", "TEXT", 1, None, 0),
        ("tag", "TEXT", 1, None, 0),
        ("regex_fragment", "TEXT", 1, None, 0),
        ("status", "TEXT", 1, "'active'", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
    ),
    "web_users": (
        ("id", "INTEGER", 0, None, 1),
        ("username", "TEXT", 1, None, 0),
        ("password_hash", "TEXT", 1, None, 0),
        ("role", "TEXT", 1, "'admin'", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("password_updated_at", "TEXT", 1, None, 0),
        ("disabled_at", "TEXT", 0, None, 0),
    ),
    "web_sessions": (
        ("id_hash", "TEXT", 0, None, 1),
        ("user_id", "INTEGER", 1, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("last_seen_at", "TEXT", 1, None, 0),
        ("expires_at", "TEXT", 1, None, 0),
        ("user_agent_hash", "TEXT", 1, "''", 0),
        ("revoked_at", "TEXT", 0, None, 0),
    ),
    "web_settings": (
        ("key", "TEXT", 0, None, 1),
        ("value", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
    ),
}

WEB_SCHEMA_TABLES = frozenset(
    {"schema_migrations", "web_users", "web_sessions", "web_settings"}
)

EXPECTED_SCHEMA_V12: SchemaDefinition = dict(EXPECTED_SCHEMA)
EXPECTED_SCHEMA_V12["release_note_cache"] = tuple(
    column
    for column in EXPECTED_SCHEMA["release_note_cache"]
    if column[0] != RELEASE_NOTE_TARGET_DIGEST_COLUMN[0]
)

EXPECTED_SCHEMA_V11: SchemaDefinition = dict(EXPECTED_SCHEMA_V12)
EXPECTED_SCHEMA_V11["release_note_cache"] = tuple(
    column
    for column in EXPECTED_SCHEMA_V12["release_note_cache"]
    if column[0] != RELEASE_NOTE_BODY_COLUMN[0]
)

EXPECTED_SCHEMA_V10: SchemaDefinition = {
    name: columns
    for name, columns in EXPECTED_SCHEMA_V11.items()
    if name != "release_notification_history"
}

EXPECTED_SCHEMA_V9: SchemaDefinition = dict(EXPECTED_SCHEMA_V10)
EXPECTED_SCHEMA_V9["security_scan_cache"] = tuple(
    column
    for column in EXPECTED_SCHEMA_V10["security_scan_cache"]
    if column[0] != SECURITY_SCAN_CACHE_FINDINGS_COLUMN[0]
)

EXPECTED_SCHEMA_V8: SchemaDefinition = {
    name: columns
    for name, columns in EXPECTED_SCHEMA_V9.items()
    if name != "security_scan_cache"
}

EXPECTED_SCHEMA_V7 = {
    name: columns
    for name, columns in EXPECTED_SCHEMA_V8.items()
    if name != "dependency_snoozes"
}

EXPECTED_SCHEMA_V6 = {
    name: (
        tuple(
            column
            for column in columns
            if name not in {"pending_updates", "update_events", "known_images"}
            or column[0] not in DIGEST_PROVENANCE_SQL_COLUMNS
        )
    )
    for name, columns in EXPECTED_SCHEMA_V7.items()
}

EXPECTED_SCHEMA_V5 = {
    name: (
        tuple(
            column
            for column in columns
            if name != "service_policy"
            or column[0] not in {"auto_update_time", "auto_update_days_json"}
        )
    )
    for name, columns in EXPECTED_SCHEMA_V6.items()
    if name != "auto_update_schedule_runs"
}

EXPECTED_SCHEMA_V4 = {
    name: columns
    for name, columns in EXPECTED_SCHEMA_V5.items()
    if name != "release_note_cache"
}

EXPECTED_SCHEMA_V3 = {
    name: columns
    for name, columns in EXPECTED_SCHEMA_V4.items()
    if name not in WEB_SCHEMA_TABLES
}

EXPECTED_SCHEMA_V2 = {
    name: columns
    for name, columns in EXPECTED_SCHEMA_V3.items()
    if name != "tag_exclusion_rules"
}

EXPECTED_SCHEMA_V1 = {
    name: columns
    for name, columns in EXPECTED_SCHEMA_V2.items()
    if name != "pending_updates"
}

_EXPECTED_SCHEMAS_BY_VERSION: dict[int, SchemaDefinition] = {
    1: EXPECTED_SCHEMA_V1,
    2: EXPECTED_SCHEMA_V2,
    3: EXPECTED_SCHEMA_V3,
    4: EXPECTED_SCHEMA_V4,
    5: EXPECTED_SCHEMA_V5,
    6: EXPECTED_SCHEMA_V6,
    7: EXPECTED_SCHEMA_V7,
    8: EXPECTED_SCHEMA_V8,
    9: EXPECTED_SCHEMA_V9,
    10: EXPECTED_SCHEMA_V10,
    11: EXPECTED_SCHEMA_V11,
    12: EXPECTED_SCHEMA_V12,
    13: EXPECTED_SCHEMA,
    SCHEMA_VERSION: EXPECTED_SCHEMA,
}
_SCHEMA_IDENTIFIERS = frozenset(EXPECTED_SCHEMA) | frozenset(
    column[0] for columns in EXPECTED_SCHEMA.values() for column in columns
)

_SECURITY_SCAN_CACHE_SCHEMA_V9_SQL = """
CREATE TABLE IF NOT EXISTS security_scan_cache (
    cache_key TEXT PRIMARY KEY,
    request_key TEXT NOT NULL,
    subject_id TEXT NOT NULL DEFAULT '',
    canonical_registry TEXT NOT NULL DEFAULT '',
    canonical_repository TEXT NOT NULL DEFAULT '',
    requested_ref TEXT NOT NULL DEFAULT '',
    reported_digest TEXT NOT NULL DEFAULT '',
    index_digest TEXT NOT NULL DEFAULT '',
    manifest_digest TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    platform_os TEXT NOT NULL DEFAULT '',
    platform_architecture TEXT NOT NULL DEFAULT '',
    platform_variant TEXT NOT NULL DEFAULT '',
    platform_source TEXT NOT NULL DEFAULT '',
    identity_status TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    verdict TEXT NOT NULL DEFAULT '',
    scanner TEXT NOT NULL DEFAULT '',
    scanner_version TEXT NOT NULL DEFAULT '',
    scanner_schema TEXT NOT NULL DEFAULT '',
    db_revision TEXT NOT NULL DEFAULT '',
    db_updated_at TEXT NOT NULL DEFAULT '',
    severity_counts_json TEXT NOT NULL DEFAULT '{}',
    fixable_counts_json TEXT NOT NULL DEFAULT '{}',
    unfixed_count INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""

_SECURITY_SCAN_CACHE_SCHEMA_V10_SQL = """
CREATE TABLE IF NOT EXISTS security_scan_cache (
    cache_key TEXT PRIMARY KEY,
    request_key TEXT NOT NULL,
    subject_id TEXT NOT NULL DEFAULT '',
    canonical_registry TEXT NOT NULL DEFAULT '',
    canonical_repository TEXT NOT NULL DEFAULT '',
    requested_ref TEXT NOT NULL DEFAULT '',
    reported_digest TEXT NOT NULL DEFAULT '',
    index_digest TEXT NOT NULL DEFAULT '',
    manifest_digest TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    platform_os TEXT NOT NULL DEFAULT '',
    platform_architecture TEXT NOT NULL DEFAULT '',
    platform_variant TEXT NOT NULL DEFAULT '',
    platform_source TEXT NOT NULL DEFAULT '',
    identity_status TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    verdict TEXT NOT NULL DEFAULT '',
    scanner TEXT NOT NULL DEFAULT '',
    scanner_version TEXT NOT NULL DEFAULT '',
    scanner_schema TEXT NOT NULL DEFAULT '',
    db_revision TEXT NOT NULL DEFAULT '',
    db_updated_at TEXT NOT NULL DEFAULT '',
    severity_counts_json TEXT NOT NULL DEFAULT '{}',
    fixable_counts_json TEXT NOT NULL DEFAULT '{}',
    unfixed_count INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    findings_json TEXT NOT NULL DEFAULT '[]'
);
"""

_SECURITY_SCAN_CACHE_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_security_scan_cache_request
    ON security_scan_cache (request_key, updated_at);
CREATE INDEX IF NOT EXISTS idx_security_scan_cache_image_digest_platform
    ON security_scan_cache (requested_ref, reported_digest, platform, updated_at);
"""

_SECURITY_SCAN_CACHE_SCHEMA_V9_SQL += _SECURITY_SCAN_CACHE_INDEXES_SQL
_SECURITY_SCAN_CACHE_SCHEMA_V10_SQL += _SECURITY_SCAN_CACHE_INDEXES_SQL
_SECURITY_SCAN_CACHE_SCHEMA_SQL = _SECURITY_SCAN_CACHE_SCHEMA_V10_SQL


class DatabaseError(RuntimeError):
    """Raised when the SQLite schema cannot be initialized safely."""


def init_schema(conn: sqlite3.Connection) -> None:
    """Create or validate the current database schema."""

    version = _user_version(conn)
    if version > 0:
        expected_schema = _EXPECTED_SCHEMAS_BY_VERSION.get(version)
        if expected_schema is None:
            raise DatabaseError(f"Unsupported database schema version: {version}")

        _validate_schema(conn, expected_schema=expected_schema)
        _ensure_schema_migrations(conn)
        _backfill_schema_migrations(conn, version)
        for target_version in range(version + 1, SCHEMA_VERSION + 1):
            migration = _MIGRATIONS_BY_TARGET_VERSION[target_version]
            migration(conn)
        _validate_schema(conn)
        return

    if version != 0:
        raise DatabaseError(f"Unsupported database schema version: {version}")

    _validate_existing_schema_objects(conn)
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS update_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT '',
                wud_file TEXT NOT NULL DEFAULT '',
                log_file TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS update_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                service_name TEXT NOT NULL,
                stack_name TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL,
                target_image TEXT NOT NULL DEFAULT '',
                old_image_id TEXT NOT NULL DEFAULT '',
                new_image_id TEXT NOT NULL DEFAULT '',
                old_digest TEXT NOT NULL DEFAULT '',
                new_digest TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                digest_source_image TEXT NOT NULL DEFAULT '',
                digest_resolved_tag TEXT NOT NULL DEFAULT '',
                digest_watch_tag TEXT NOT NULL DEFAULT '',
                digest_target_digest TEXT NOT NULL DEFAULT '',
                digest_final_image TEXT NOT NULL DEFAULT '',
                digest_provenance_source TEXT NOT NULL DEFAULT '',
                digest_provenance_confidence TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (run_id) REFERENCES update_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS snoozes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_key TEXT NOT NULL,
                snoozed_until TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS dependency_snoozes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_key TEXT NOT NULL,
                wait_for_service_key TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS service_policy (
                service_key TEXT PRIMARY KEY,
                update_mode TEXT NOT NULL DEFAULT '',
                auto_update INTEGER NOT NULL DEFAULT 1,
                snooze_default_seconds INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                auto_update_time TEXT,
                auto_update_days_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS auto_update_schedule_runs (
                schedule_key TEXT PRIMARY KEY,
                service_key TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                run_id INTEGER,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (run_id) REFERENCES update_runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS known_images (
                service_key TEXT PRIMARY KEY,
                image TEXT NOT NULL,
                image_id TEXT NOT NULL DEFAULT '',
                digest TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                digest_source_image TEXT NOT NULL DEFAULT '',
                digest_resolved_tag TEXT NOT NULL DEFAULT '',
                digest_watch_tag TEXT NOT NULL DEFAULT '',
                digest_target_digest TEXT NOT NULL DEFAULT '',
                digest_final_image TEXT NOT NULL DEFAULT '',
                digest_provenance_source TEXT NOT NULL DEFAULT '',
                digest_provenance_confidence TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS pending_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                line_no INTEGER NOT NULL,
                raw TEXT NOT NULL,
                image TEXT NOT NULL,
                target_digest TEXT NOT NULL DEFAULT '',
                desired_tag TEXT NOT NULL DEFAULT '',
                service_key TEXT NOT NULL DEFAULT '',
                stack_name TEXT NOT NULL DEFAULT '',
                service_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                status_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                digest_source_image TEXT NOT NULL DEFAULT '',
                digest_resolved_tag TEXT NOT NULL DEFAULT '',
                digest_watch_tag TEXT NOT NULL DEFAULT '',
                digest_target_digest TEXT NOT NULL DEFAULT '',
                digest_final_image TEXT NOT NULL DEFAULT '',
                digest_provenance_source TEXT NOT NULL DEFAULT '',
                digest_provenance_confidence TEXT NOT NULL DEFAULT '',
                UNIQUE (run_id, line_no),
                FOREIGN KEY (run_id) REFERENCES update_runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_update_events_run_id
                ON update_events (run_id);
            CREATE INDEX IF NOT EXISTS idx_update_events_service_latest
                ON update_events (stack_name, service_name, id DESC);
            CREATE INDEX IF NOT EXISTS idx_snoozes_service_key_until
                ON snoozes (service_key, snoozed_until);
            CREATE INDEX IF NOT EXISTS idx_dependency_snoozes_service_key
                ON dependency_snoozes (service_key, created_at);
            CREATE INDEX IF NOT EXISTS idx_dependency_snoozes_wait_for_service_key
                ON dependency_snoozes (wait_for_service_key, created_at);
            CREATE INDEX IF NOT EXISTS idx_pending_updates_run_id
                ON pending_updates (run_id);
            CREATE INDEX IF NOT EXISTS idx_pending_updates_status
                ON pending_updates (status);
            CREATE INDEX IF NOT EXISTS idx_pending_updates_service_key_status
                ON pending_updates (service_key, status);
            CREATE INDEX IF NOT EXISTS idx_auto_update_schedule_runs_service
                ON auto_update_schedule_runs (service_key, scheduled_for);

            CREATE TABLE IF NOT EXISTS release_note_cache (
                cache_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                image_repo TEXT NOT NULL DEFAULT '',
                upstream_repo TEXT NOT NULL DEFAULT '',
                current_tag TEXT NOT NULL DEFAULT '',
                target_tag TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                release_tag TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                breaking INTEGER NOT NULL DEFAULT 0,
                breaking_reasons_json TEXT NOT NULL DEFAULT '[]',
                links_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                body TEXT NOT NULL DEFAULT '',
                target_digest TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_release_note_cache_updated_at
                ON release_note_cache (updated_at);
            CREATE INDEX IF NOT EXISTS idx_release_note_cache_identity_digest
                ON release_note_cache (
                    provider,
                    image_repo,
                    upstream_repo,
                    current_tag,
                    target_tag,
                    target_digest
                );

            CREATE TABLE IF NOT EXISTS release_notification_history (
                notification_key TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                last_attempted_at TEXT NOT NULL,
                last_sent_at TEXT NOT NULL DEFAULT '',
                send_count INTEGER NOT NULL DEFAULT 0,
                last_audit_run_id INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_release_notification_history_status
                ON release_notification_history (status, last_sent_at);

            CREATE TABLE IF NOT EXISTS tag_exclusion_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                image_repo TEXT NOT NULL,
                service_key TEXT NOT NULL DEFAULT '',
                match_type TEXT NOT NULL,
                tag TEXT NOT NULL,
                regex_fragment TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (scope, image_repo, service_key, match_type, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_tag_exclusion_rules_lookup
                ON tag_exclusion_rules (status, image_repo, service_key, match_type);

            CREATE TABLE IF NOT EXISTS web_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                created_at TEXT NOT NULL,
                password_updated_at TEXT NOT NULL,
                disabled_at TEXT
            );

            CREATE TABLE IF NOT EXISTS web_sessions (
                id_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                user_agent_hash TEXT NOT NULL DEFAULT '',
                revoked_at TEXT,
                FOREIGN KEY (user_id) REFERENCES web_users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_web_sessions_user_id
                ON web_sessions (user_id);
            CREATE INDEX IF NOT EXISTS idx_web_sessions_expires_at
                ON web_sessions (expires_at);

            CREATE TABLE IF NOT EXISTS web_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.executescript(_SECURITY_SCAN_CACHE_SCHEMA_SQL)
        _validate_schema(conn)
        _backfill_schema_migrations(conn, SCHEMA_VERSION)
        conn.execute(  # nosemgrep: PRAGMA needs a literal internal version.
            "PRAGMA user_version = 14"
        )



def utc_timestamp() -> str:
    """Return a stable UTC timestamp format for SQLite text comparisons."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _user_version(conn: sqlite3.Connection) -> int:
    with closing(conn.execute("PRAGMA user_version")) as cursor:
        row = cursor.fetchone()
    return int(row[0])


def _validate_existing_schema_objects(conn: sqlite3.Connection) -> None:
    for table_name, expected_columns in EXPECTED_SCHEMA.items():
        object_type = _sqlite_object_type(conn, table_name)
        if object_type is None:
            continue
        if object_type != "table":
            raise DatabaseError(
                f"Expected {table_name} to be a table, found {object_type}"
            )
        _validate_table_columns(conn, table_name, expected_columns)


def _validate_schema(
    conn: sqlite3.Connection,
    *,
    expected_schema: dict[str, tuple[ColumnSchema, ...]] | None = None,
) -> None:
    schema = expected_schema or EXPECTED_SCHEMA
    for table_name, expected_columns in schema.items():
        object_type = _sqlite_object_type(conn, table_name)
        if object_type is None:
            raise DatabaseError(f"Missing expected table: {table_name}")
        if object_type != "table":
            raise DatabaseError(
                f"Expected {table_name} to be a table, found {object_type}"
            )
        _validate_table_columns(conn, table_name, expected_columns)


def _validate_table_columns(
    conn: sqlite3.Connection,
    table_name: str,
    expected_columns: tuple[ColumnSchema, ...],
) -> None:
    actual_columns = _table_columns(conn, table_name)
    actual_names = tuple(column[0] for column in actual_columns)
    expected_names = tuple(column[0] for column in expected_columns)
    if actual_names != expected_names:
        raise DatabaseError(
            f"Unexpected columns for table {table_name}: "
            f"expected {_format_column_names(expected_names)}, "
            f"found {_format_column_names(actual_names)}"
        )
    if actual_columns != expected_columns:
        raise DatabaseError(f"Unexpected column definition for table {table_name}")


def _table_columns(
    conn: sqlite3.Connection,
    table_name: str,
) -> tuple[ColumnSchema, ...]:
    with closing(
        conn.execute(
            """
            SELECT name, type, "notnull", dflt_value, pk
            FROM pragma_table_info(?)
            ORDER BY cid
            """,
            (table_name,),
        )
    ) as cursor:
        return tuple(
            (
                str(row[0]),
                str(row[1]).upper(),
                int(row[2]),
                None if row[3] is None else str(row[3]),
                int(row[4]),
            )
            for row in cursor.fetchall()
        )


def _sqlite_object_type(conn: sqlite3.Connection, name: str) -> str | None:
    with closing(conn.execute(
        """
        SELECT type
        FROM sqlite_master
        WHERE name = ?
        LIMIT 1
        """,
        (name,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        return None
    return str(row[0])


def _quote_identifier(value: str) -> str:
    if value not in _SCHEMA_IDENTIFIERS:
        raise DatabaseError(f"Unexpected schema identifier: {value}")
    return (  # nosemgrep: value is schema-allowlisted before quoting.
        '"' + value.replace('"', '""') + '"'
    )


def _format_column_names(names: tuple[str, ...]) -> str:
    if not names:
        return "<none>"
    return ", ".join(names)


MIGRATION_NAMES = {
    1: "create update state tables",
    2: "add pending update records",
    3: "add tag exclusion rules",
    4: "add web auth state",
    5: "add release note cache",
    6: "add auto update schedules",
    7: "add digest tag provenance columns",
    8: "add dependency snoozes",
    9: "add security scan cache",
    10: "add security scan findings",
    11: "add release notification history",
    12: "add release note body cache",
    14: "index recent service events",
}


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    object_type = _sqlite_object_type(conn, "schema_migrations")
    if object_type is not None:
        if object_type != "table":
            raise DatabaseError(
                f"Expected schema_migrations to be a table, found {object_type}"
            )
        _validate_table_columns(
            conn,
            "schema_migrations",
            EXPECTED_SCHEMA["schema_migrations"],
        )
        return
    with conn:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )


def _backfill_schema_migrations(conn: sqlite3.Connection, version: int) -> None:
    _ensure_schema_migrations(conn)
    with conn:
        for migration_version in range(1, version + 1):
            name = MIGRATION_NAMES.get(
                migration_version,
                f"schema version {migration_version}",
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (migration_version, name, utc_timestamp()),
            )


def _record_schema_migration(conn: sqlite3.Connection, version: int) -> None:
    name = MIGRATION_NAMES.get(version, f"schema version {version}")
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (version, name, applied_at)
            VALUES (?, ?, ?)
            """,
            (version, name, utc_timestamp()),
        )


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    object_type = _sqlite_object_type(conn, "pending_updates")
    if object_type is not None:
        if object_type != "table":
            raise DatabaseError(
                f"Expected pending_updates to be a table, found {object_type}"
            )
        _validate_table_columns(conn, "pending_updates", EXPECTED_SCHEMA["pending_updates"])
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                line_no INTEGER NOT NULL,
                raw TEXT NOT NULL,
                image TEXT NOT NULL,
                target_digest TEXT NOT NULL DEFAULT '',
                desired_tag TEXT NOT NULL DEFAULT '',
                service_key TEXT NOT NULL DEFAULT '',
                stack_name TEXT NOT NULL DEFAULT '',
                service_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                status_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (run_id, line_no),
                FOREIGN KEY (run_id) REFERENCES update_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_pending_updates_run_id
                ON pending_updates (run_id);
            CREATE INDEX IF NOT EXISTS idx_pending_updates_status
                ON pending_updates (status);
            CREATE INDEX IF NOT EXISTS idx_pending_updates_service_key_status
                ON pending_updates (service_key, status);
            """
        )
        conn.execute("PRAGMA user_version = 2")
    _record_schema_migration(conn, 2)


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    object_type = _sqlite_object_type(conn, "tag_exclusion_rules")
    if object_type is not None:
        if object_type != "table":
            raise DatabaseError(
                f"Expected tag_exclusion_rules to be a table, found {object_type}"
            )
        _validate_table_columns(
            conn,
            "tag_exclusion_rules",
            EXPECTED_SCHEMA["tag_exclusion_rules"],
        )
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tag_exclusion_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                image_repo TEXT NOT NULL,
                service_key TEXT NOT NULL DEFAULT '',
                match_type TEXT NOT NULL,
                tag TEXT NOT NULL,
                regex_fragment TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (scope, image_repo, service_key, match_type, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_tag_exclusion_rules_lookup
                ON tag_exclusion_rules (status, image_repo, service_key, match_type);
            """
        )
        conn.execute("PRAGMA user_version = 3")
    _record_schema_migration(conn, 3)


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    for table_name in ("web_users", "web_sessions", "web_settings"):
        object_type = _sqlite_object_type(conn, table_name)
        if object_type is not None:
            if object_type != "table":
                raise DatabaseError(
                    f"Expected {table_name} to be a table, found {object_type}"
                )
            _validate_table_columns(conn, table_name, EXPECTED_SCHEMA[table_name])
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS web_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                created_at TEXT NOT NULL,
                password_updated_at TEXT NOT NULL,
                disabled_at TEXT
            );

            CREATE TABLE IF NOT EXISTS web_sessions (
                id_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                user_agent_hash TEXT NOT NULL DEFAULT '',
                revoked_at TEXT,
                FOREIGN KEY (user_id) REFERENCES web_users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_web_sessions_user_id
                ON web_sessions (user_id);
            CREATE INDEX IF NOT EXISTS idx_web_sessions_expires_at
                ON web_sessions (expires_at);

            CREATE TABLE IF NOT EXISTS web_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute("PRAGMA user_version = 4")
    _record_schema_migration(conn, 4)


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    object_type = _sqlite_object_type(conn, "release_note_cache")
    if object_type is not None:
        if object_type != "table":
            raise DatabaseError(
                f"Expected release_note_cache to be a table, found {object_type}"
            )
        _validate_table_columns(
            conn,
            "release_note_cache",
            EXPECTED_SCHEMA_V11["release_note_cache"],
        )
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS release_note_cache (
                cache_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                image_repo TEXT NOT NULL DEFAULT '',
                upstream_repo TEXT NOT NULL DEFAULT '',
                current_tag TEXT NOT NULL DEFAULT '',
                target_tag TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                release_tag TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                breaking INTEGER NOT NULL DEFAULT 0,
                breaking_reasons_json TEXT NOT NULL DEFAULT '[]',
                links_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_release_note_cache_updated_at
                ON release_note_cache (updated_at);
            """
        )
        conn.execute("PRAGMA user_version = 5")
    _record_schema_migration(conn, 5)


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    _validate_table_columns(
        conn,
        "service_policy",
        EXPECTED_SCHEMA_V5["service_policy"],
    )
    object_type = _sqlite_object_type(conn, "auto_update_schedule_runs")
    if object_type is not None:
        if object_type != "table":
            raise DatabaseError(
                f"Expected auto_update_schedule_runs to be a table, found {object_type}"
            )
        _validate_table_columns(
            conn,
            "auto_update_schedule_runs",
            EXPECTED_SCHEMA["auto_update_schedule_runs"],
        )
    with conn:
        conn.executescript(
            """
            ALTER TABLE service_policy
                ADD COLUMN auto_update_time TEXT;
            ALTER TABLE service_policy
                ADD COLUMN auto_update_days_json TEXT NOT NULL DEFAULT '[]';

            CREATE TABLE IF NOT EXISTS auto_update_schedule_runs (
                schedule_key TEXT PRIMARY KEY,
                service_key TEXT NOT NULL,
                scheduled_for TEXT NOT NULL,
                run_id INTEGER,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (run_id) REFERENCES update_runs(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_auto_update_schedule_runs_service
                ON auto_update_schedule_runs (service_key, scheduled_for);
            """
        )
        conn.execute("PRAGMA user_version = 6")
    _record_schema_migration(conn, 6)


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    for table_name in ("pending_updates", "update_events", "known_images"):
        _validate_table_columns(conn, table_name, EXPECTED_SCHEMA_V6[table_name])
    with conn:
        for table_name in ("pending_updates", "update_events", "known_images"):
            for column in DIGEST_PROVENANCE_SQL_COLUMNS:
                statement = " ".join(
                    (
                        "ALTER TABLE",
                        _quote_identifier(table_name),
                        "ADD COLUMN",
                        _quote_identifier(column),
                        "TEXT NOT NULL DEFAULT ''",
                    )
                )
                conn.execute(  # nosemgrep: allowlisted tables, fixed columns.
                    statement
                )
        conn.execute("PRAGMA user_version = 7")
    _record_schema_migration(conn, 7)


def _migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    object_type = _sqlite_object_type(conn, "dependency_snoozes")
    if object_type is not None:
        if object_type != "table":
            raise DatabaseError(
                f"Expected dependency_snoozes to be a table, found {object_type}"
            )
        _validate_table_columns(
            conn,
            "dependency_snoozes",
            EXPECTED_SCHEMA["dependency_snoozes"],
        )
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS dependency_snoozes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_key TEXT NOT NULL,
                wait_for_service_key TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_dependency_snoozes_service_key
                ON dependency_snoozes (service_key, created_at);
            CREATE INDEX IF NOT EXISTS idx_dependency_snoozes_wait_for_service_key
                ON dependency_snoozes (wait_for_service_key, created_at);
            """
        )
        conn.execute("PRAGMA user_version = 8")
    _record_schema_migration(conn, 8)


def _migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    object_type = _sqlite_object_type(conn, "security_scan_cache")
    if object_type is not None:
        if object_type != "table":
            raise DatabaseError(
                f"Expected security_scan_cache to be a table, found {object_type}"
            )
        _validate_table_columns(
            conn,
            "security_scan_cache",
            EXPECTED_SCHEMA_V9["security_scan_cache"],
        )
    with conn:
        conn.executescript(_SECURITY_SCAN_CACHE_SCHEMA_V9_SQL)
        conn.execute("PRAGMA user_version = 9")
    _record_schema_migration(conn, 9)


def _migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    _validate_table_columns(
        conn,
        "security_scan_cache",
        EXPECTED_SCHEMA_V9["security_scan_cache"],
    )
    with conn:
        conn.execute(
            """
            ALTER TABLE security_scan_cache
                ADD COLUMN findings_json TEXT NOT NULL DEFAULT '[]'
            """
        )
        conn.execute("PRAGMA user_version = 10")
    _record_schema_migration(conn, 10)


def _migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
    object_type = _sqlite_object_type(conn, "release_notification_history")
    if object_type is not None:
        if object_type != "table":
            raise DatabaseError(
                f"Expected release_notification_history to be a table, found {object_type}"
            )
        _validate_table_columns(
            conn,
            "release_notification_history",
            EXPECTED_SCHEMA["release_notification_history"],
        )
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS release_notification_history (
                notification_key TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                last_attempted_at TEXT NOT NULL,
                last_sent_at TEXT NOT NULL DEFAULT '',
                send_count INTEGER NOT NULL DEFAULT 0,
                last_audit_run_id INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_release_notification_history_status
                ON release_notification_history (status, last_sent_at);
            """
        )
        conn.execute("PRAGMA user_version = 11")
    _record_schema_migration(conn, 11)


def _migrate_v11_to_v12(conn: sqlite3.Connection) -> None:
    _validate_table_columns(
        conn,
        "release_note_cache",
        EXPECTED_SCHEMA_V11["release_note_cache"],
    )
    with conn:
        conn.execute(
            """
            ALTER TABLE release_note_cache
                ADD COLUMN body TEXT NOT NULL DEFAULT ''
            """
        )
        conn.execute("PRAGMA user_version = 12")
    _record_schema_migration(conn, 12)


def _migrate_v12_to_v13(conn: sqlite3.Connection) -> None:
    _validate_table_columns(
        conn,
        "release_note_cache",
        EXPECTED_SCHEMA_V12["release_note_cache"],
    )
    with conn:
        conn.execute(
            """
            ALTER TABLE release_note_cache
                ADD COLUMN target_digest TEXT NOT NULL DEFAULT ''
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_release_note_cache_identity_digest
                ON release_note_cache (
                    provider,
                    image_repo,
                    upstream_repo,
                    current_tag,
                    target_tag,
                    target_digest
                )
            """
        )
        conn.execute("PRAGMA user_version = 13")
    _record_schema_migration(conn, 13)


def _migrate_v13_to_v14(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_update_events_service_latest "
            "ON update_events (stack_name, service_name, id DESC)"
        )
        conn.execute("PRAGMA user_version = 14")
    _record_schema_migration(conn, 14)


_MIGRATIONS_BY_TARGET_VERSION: dict[int, Migration] = {
    2: _migrate_v1_to_v2,
    3: _migrate_v2_to_v3,
    4: _migrate_v3_to_v4,
    5: _migrate_v4_to_v5,
    6: _migrate_v5_to_v6,
    7: _migrate_v6_to_v7,
    8: _migrate_v7_to_v8,
    9: _migrate_v8_to_v9,
    10: _migrate_v9_to_v10,
    11: _migrate_v10_to_v11,
    12: _migrate_v11_to_v12,
    13: _migrate_v12_to_v13,
    14: _migrate_v13_to_v14,
}
