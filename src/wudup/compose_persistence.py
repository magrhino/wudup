"""Atomic Compose file persistence and backup ownership.

All Compose writers, backup creation, and guarded restoration share the same
directory lock. Source hashes, metadata copying, and temporary-file cleanup
stay inside this owner; rendering and update approval belong to callers.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .updater_models import ComposeTagRewriteError


def _compose_source_hash(compose_path: Path) -> str:
    return hashlib.sha256(compose_path.read_bytes()).hexdigest()


@contextmanager
def _compose_write_lock(compose_path: Path) -> Iterator[None]:
    """Serialize WUDup writers without a lock file that can retain a stale owner."""

    # ponytail: directory locking also serializes other Compose files here;
    # use a finer-grained lock only if that contention becomes measurable.
    fd = os.open(compose_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _atomic_replace_compose(
    compose_path: Path, rendered: str, *, prefix: str,
    expected_source_hash: str | None = None,
    written_hashes: list[str] | None = None,
) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{compose_path.name}.{prefix}.",
        dir=str(compose_path.parent),
    )
    tmp_path: Path | None = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as tmp:
            tmp.write(rendered)
        with _compose_write_lock(compose_path):
            st = compose_path.stat()
            os.chown(tmp_path, st.st_uid, st.st_gid)
            os.chmod(tmp_path, st.st_mode & 0o7777)
            if expected_source_hash is not None and _compose_source_hash(compose_path) != expected_source_hash:
                if prefix.startswith("tracking-"):
                    raise ComposeTagRewriteError("Compose file changed before tracking repair; preview it again.")
                raise ComposeTagRewriteError("Compose file changed before it could be rewritten; retry from a fresh state.")
            os.replace(tmp_path, compose_path)
            if written_hashes is not None:
                written_hashes.append(hashlib.sha256(rendered.encode("utf-8")).hexdigest())
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def restore_compose_backup(
    backup: Path, compose_path: Path, *, expected_source_hash: str,
) -> None:
    """Restore only the Compose version written by this operation."""

    with backup.open("r", encoding="utf-8", newline="") as source:
        _atomic_replace_compose(
            compose_path, source.read(), prefix="rollback",
            expected_source_hash=expected_source_hash,
        )


def _backup_compose(compose_path: Path) -> Path:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{compose_path.name}.backup.",
        dir=str(compose_path.parent),
    )
    os.close(fd)
    backup = Path(tmp_name)
    try:
        with _compose_write_lock(compose_path):
            shutil.copy2(compose_path, backup)
    except Exception:
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        raise
    return backup
