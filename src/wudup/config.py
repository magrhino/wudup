"""Defaults and environment parsing for the Python updater."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .naming import DB_FILENAME

DEFAULT_UPDATE_MODE = "stop"
DEFAULT_MAX_WAIT = 180
DEFAULT_LOCK_TIMEOUT = 30
DEFAULT_LOG_DIR = "./logs"
DEFAULT_TIMEZONE = "UTC"
COMPOSE_IGNORE_PATHS_ENV = "WUD_COMPOSE_IGNORE_PATHS"
DEFAULT_COMPOSE_IGNORE_PATHS: tuple[Path, ...] = ()
DIGEST_PIN_UPDATES_ENV = "WUD_DIGEST_PIN_UPDATES"
DEFAULT_DIGEST_PIN_UPDATES = False
VALID_UPDATE_MODES = frozenset({"pause", "stop", "live"})


class ConfigError(ValueError):
    """Raised when environment-derived configuration is invalid."""


@dataclass(frozen=True)
class UpdaterConfig:
    docker_base: Path
    wud_out_file: Path
    log_dir: Path
    db_path: Path
    update_mode: str
    max_wait: int
    lock_timeout: int
    timezone_name: str
    compose_ignore_paths: tuple[Path, ...]
    digest_pin_updates: bool
    out_uid: int | None
    out_gid: int | None


def _parse_non_negative_int(name: str, value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer number of seconds") from exc
    if parsed < 0:
        raise ConfigError(f"{name} must be zero or greater")
    return parsed


def _parse_lock_timeout(value: str | None) -> int:
    lock_timeout = _parse_non_negative_int(
        "WUD_LOCK_TIMEOUT",
        value,
        DEFAULT_LOCK_TIMEOUT,
    )
    # docker-update-from-wud reads the raw value with the same digits-only rule,
    # so reject padded or signed values here instead of failing every apply job.
    if value and not (value.isascii() and value.isdigit()):
        raise ConfigError("WUD_LOCK_TIMEOUT must be an integer number of seconds")
    return lock_timeout


def _parse_optional_numeric_id(name: str, value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise ConfigError(f"{name} must be numeric") from exc
    if parsed < 0:
        raise ConfigError(f"{name} must be zero or greater")
    return parsed


def parse_bool_env(
    name: str,
    value: str | None,
    *,
    default: bool = False,
) -> bool:
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _parse_timezone_name(value: str | None) -> str:
    name = value or DEFAULT_TIMEZONE
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            "WUD_TIMEZONE must be an IANA timezone name such as America/Chicago"
        ) from exc
    return name


def parse_compose_ignore_paths(
    value: str | None,
    *,
    name: str = COMPOSE_IGNORE_PATHS_ENV,
    default: tuple[Path, ...] = DEFAULT_COMPOSE_IGNORE_PATHS,
) -> tuple[Path, ...]:
    if value is None:
        return default
    if value == "":
        return ()

    paths: list[Path] = []
    seen: set[tuple[str, ...]] = set()
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            raise ConfigError(
                f"{name} must be a comma-separated list of non-empty relative paths"
            )
        if item.startswith("/"):
            raise ConfigError(f"{name} entries must be relative paths")

        parts = tuple(item.split("/"))
        if any(part in {"", ".", ".."} for part in parts):
            raise ConfigError(
                f"{name} entries cannot contain empty, '.', or '..' path components"
            )

        if parts not in seen:
            seen.add(parts)
            paths.append(Path(*parts))
    return tuple(paths)


def format_compose_ignore_paths(paths: tuple[Path, ...]) -> str:
    return ", ".join(path.as_posix() for path in paths)


def _env_or_default(env: Mapping[str, str], name: str, default: str) -> str:
    return env.get(name) or default


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    home: str | Path | None = None,
) -> UpdaterConfig:
    """Parse updater defaults from environment variables.

    This intentionally does not read config files or command-line arguments.
    """

    env = os.environ if environ is None else environ
    home_dir = Path(
        home if home is not None else _env_or_default(env, "HOME", str(Path.home()))
    )

    docker_base = Path(_env_or_default(env, "DOCKER_BASE", str(home_dir / "docker")))
    wud_out_file = Path(
        _env_or_default(
            env,
            "WUD_OUT_FILE",
            str(docker_base / "wud" / "out" / "images.todo"),
        )
    )
    log_dir = Path(_env_or_default(env, "WUD_LOG_DIR", DEFAULT_LOG_DIR))
    db_path = Path(
        _env_or_default(
            env,
            "WUD_DB_PATH",
            str(log_dir / DB_FILENAME),
        )
    )

    update_mode = _env_or_default(env, "WUD_UPDATE_MODE", DEFAULT_UPDATE_MODE)
    if update_mode not in VALID_UPDATE_MODES:
        raise ConfigError("WUD_UPDATE_MODE must be pause, stop, or live")

    max_wait = _parse_non_negative_int(
        "WUD_MAX_WAIT",
        env.get("WUD_MAX_WAIT"),
        DEFAULT_MAX_WAIT,
    )
    lock_timeout = _parse_lock_timeout(env.get("WUD_LOCK_TIMEOUT"))
    timezone_name = _parse_timezone_name(env.get("WUD_TIMEZONE"))
    compose_ignore_paths = parse_compose_ignore_paths(
        env.get(COMPOSE_IGNORE_PATHS_ENV)
    )
    digest_pin_updates = parse_bool_env(
        DIGEST_PIN_UPDATES_ENV,
        env.get(DIGEST_PIN_UPDATES_ENV),
        default=DEFAULT_DIGEST_PIN_UPDATES,
    )

    out_uid = _parse_optional_numeric_id("OUT_UID", env.get("OUT_UID"))
    out_gid_value = env.get("OUT_GID") or env.get("OUT_GUID")
    out_gid = _parse_optional_numeric_id("OUT_GID/OUT_GUID", out_gid_value)
    if (out_uid is None) != (out_gid is None):
        raise ConfigError("OUT_UID and OUT_GID/OUT_GUID must be set together")

    return UpdaterConfig(
        docker_base=docker_base,
        wud_out_file=wud_out_file,
        log_dir=log_dir,
        db_path=db_path,
        update_mode=update_mode,
        max_wait=max_wait,
        lock_timeout=lock_timeout,
        timezone_name=timezone_name,
        compose_ignore_paths=compose_ignore_paths,
        digest_pin_updates=digest_pin_updates,
        out_uid=out_uid,
        out_gid=out_gid,
    )
