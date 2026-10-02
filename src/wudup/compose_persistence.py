"""Atomic Compose file persistence and backup ownership.

All Compose writers, backup creation, and guarded restoration share the same
directory lock. Source hashes, metadata copying, and temporary-file cleanup
stay inside this owner; rendering and update approval belong to callers.

Durability: a rewrite syncs the temporary file (content, owner, and mode)
before it replaces the Compose file, then syncs the directory so the new entry
survives a crash or power loss. A backup is synced the same way before it is
returned. A failure before replacement leaves the Compose file unchanged. A
directory sync failure after replacement is raised only after the written
version's hash has been recorded, so callers know the file changed and can
restore it from their backup.
"""

from __future__ import annotations

import errno
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


# Filesystems that cannot sync a directory report one of these; the rename is
# then as durable as that filesystem allows, so there is nothing left to do.
_DIRECTORY_SYNC_UNSUPPORTED = frozenset(
    {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
)


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _DIRECTORY_SYNC_UNSUPPORTED:
            raise
    finally:
        os.close(fd)


def _sync_replaced_compose(compose_path: Path) -> None:
    try:
        _fsync_directory(compose_path.parent)
    except OSError as exc:
        raise ComposeTagRewriteError(
            f"The Compose file {compose_path.name} was replaced, but its folder "
            f"could not be synced to disk ({exc}). The new content is in place "
            "but may not survive a crash or power loss; check the storage for "
            "errors."
        ) from exc


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
            _fsync_path(tmp_path)
            if expected_source_hash is not None and _compose_source_hash(compose_path) != expected_source_hash:
                if prefix.startswith("tracking-"):
                    raise ComposeTagRewriteError("Compose file changed before tracking repair; preview it again.")
                raise ComposeTagRewriteError("Compose file changed before it could be rewritten; retry from a fresh state.")
            os.replace(tmp_path, compose_path)
            tmp_path = None
            # Record the written version before syncing the directory so a
            # sync failure still tells callers the Compose file changed.
            if written_hashes is not None:
                written_hashes.append(hashlib.sha256(rendered.encode("utf-8")).hexdigest())
            _sync_replaced_compose(compose_path)
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
            _fsync_path(backup)
            _fsync_directory(backup.parent)
    except Exception:
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        raise
    return backup
