"""Private SQLite database storage and directory-trust hardening for WUDup."""

from __future__ import annotations

import os
import stat
from collections import deque
from pathlib import Path


def _check_database_directory(
    metadata: os.stat_result, trusted_uids: set[int], *, ancestor: bool
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in trusted_uids
        or (
            metadata.st_mode & 0o022
            and not (ancestor and metadata.st_mode & stat.S_ISVTX)
        )
    ):
        raise OSError(
            "Could not protect the database directory. Set WUD_DB_PATH to a "
            "private directory whose ancestors are owned by root, the WUDup "
            "account, or configured OUT_UID, without group/other write access "
            "except on sticky ancestors."
        )


def _stat_or_create_database_directory(path: Path, *, ancestor: bool) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError:
        pass
    mode = 0o755 if ancestor else 0o700
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        # A competing creator owns this result; leave it for normal validation.
        return path.lstat()
    try:
        created = path.lstat()
        if not stat.S_ISDIR(created.st_mode) or created.st_uid != os.geteuid():
            raise OSError("The new database directory changed during startup")
        # mkdir applies umask, which can remove even owner read/execute bits.
        # Bootstrap only our new directory before opening its descriptor.
        path.chmod(mode, follow_symlinks=False)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            actual = os.fstat(fd)
            identity = created.st_dev, created.st_ino, created.st_uid
            if (actual.st_dev, actual.st_ino, actual.st_uid) != identity:
                raise OSError("The new database directory changed during startup")
            os.fchmod(fd, mode)
            actual = os.fstat(fd)
            current = path.lstat()
            if (
                (actual.st_dev, actual.st_ino, actual.st_uid) != identity
                or (current.st_dev, current.st_ino, current.st_uid) != identity
                or stat.S_IMODE(actual.st_mode) != mode
                or stat.S_IMODE(current.st_mode) != mode
            ):
                raise OSError("The new database directory permissions could not be verified")
            return current
        finally:
            os.close(fd)
    except OSError as exc:
        raise OSError(
            "Could not protect the new database directory. Ensure the WUDup "
            "account can set directory permissions, or set WUD_DB_PATH to a "
            "private directory it owns."
        ) from exc


def _private_database_directory(
    path: Path, trusted_uids: set[int], *, owner_uid: int | None = None
) -> Path:
    # Validate from root before descending, including aliases and their targets.
    # Resolving first could hide an attacker-owned directory or chained alias.
    absolute = path.absolute()
    directory = Path(absolute.anchor)
    pending = deque(absolute.parts[1:])
    links = 0
    _check_database_directory(directory.lstat(), trusted_uids, ancestor=bool(pending))
    while pending:
        component = pending.popleft()
        if component == "..":
            directory = directory.parent
            continue
        candidate = directory / component
        metadata = _stat_or_create_database_directory(candidate, ancestor=bool(pending))
        if stat.S_ISLNK(metadata.st_mode):
            links += 1
            if metadata.st_uid not in trusted_uids or links > 40:
                raise OSError(
                    "Could not protect the database directory: an untrusted or "
                    "looping symbolic link was found. Set WUD_DB_PATH to a "
                    "directory owned by the WUDup account or configured OUT_UID."
                )
            target = Path(os.readlink(candidate))
            if target.is_absolute():
                directory = Path(target.anchor)
                pending.extendleft(reversed(target.parts[1:]))
            else:
                pending.extendleft(reversed(target.parts))
            continue
        if not pending:
            _repair_database_directory(candidate, metadata, trusted_uids, owner_uid=owner_uid)
            metadata = candidate.lstat()
        _check_database_directory(metadata, trusted_uids, ancestor=bool(pending))
        directory = candidate
    _check_database_directory(directory.lstat(), trusted_uids, ancestor=False)
    return directory


def _repair_database_directory(
    path: Path,
    metadata: os.stat_result,
    trusted_uids: set[int],
    *,
    owner_uid: int | None = None,
) -> None:
    # Only the configured database directory belongs to WUDup. Never repair
    # shared ancestors, foreign owners, or sticky directories such as /tmp.
    private_owner_handoff = (
        stat.S_IMODE(metadata.st_mode) == 0o700
        and owner_uid is not None
        and os.geteuid() == 0
        and metadata.st_uid != owner_uid
    )
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in trusted_uids
        or metadata.st_mode & stat.S_ISVTX
        or (not metadata.st_mode & 0o022 and not private_owner_handoff)
    ):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            actual = os.fstat(fd)
            if (
                (actual.st_dev, actual.st_ino) != (metadata.st_dev, metadata.st_ino)
                or actual.st_uid not in trusted_uids
            ):
                raise OSError("The database directory changed during startup")
            # Root must hand the repaired directory to the configured owner too:
            # handing off only its files would strand them behind root-only 0700.
            if owner_uid is not None and os.geteuid() == 0 and actual.st_uid != owner_uid:
                os.fchown(fd, owner_uid, -1)
                if os.fstat(fd).st_uid != owner_uid:
                    raise OSError("The database directory owner could not be verified")
            os.fchmod(fd, 0o700)
            actual = os.fstat(fd)
            _check_database_directory(actual, trusted_uids, ancestor=False)
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (actual.st_dev, actual.st_ino):
                raise OSError("The database directory changed during startup")
        finally:
            os.close(fd)
    except OSError as exc:
        raise OSError(
            "Could not protect the database directory. Ensure the WUDup account "
            "can set the configured owner and owner-only permissions (0700), "
            "or set WUD_DB_PATH to a "
            "private directory it owns."
        ) from exc


def _prepare_private_database(path: Path, *, owner_uid: int | None = None) -> Path:
    trusted_uids = {0, os.geteuid()}
    if owner_uid is not None:
        trusted_uids.add(owner_uid)
    path = _private_database_directory(path.parent, trusted_uids, owner_uid=owner_uid) / path.name
    try:
        # SQLite's Unix driver inherits the database mode for new journals,
        # WAL and SHM files. Set it before SQLite can persist any secrets.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(f"{path}{suffix}")
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                if suffix:
                    continue
                raise
            if suffix and metadata.st_nlink == 0:
                continue  # A concurrent SQLite close already unlinked this file.
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid not in trusted_uids
            ):
                raise OSError("Database files must be regular files without links")
            # Do not open/close existing database descriptors: doing so can
            # release POSIX locks held by another SQLite connection in-process.
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                try:
                    candidate.chmod(0o600, follow_symlinks=False)
                except FileNotFoundError:
                    if suffix:
                        continue  # SQLite may remove a sidecar on another close.
                    raise
            try:
                actual = candidate.lstat()
            except FileNotFoundError:
                if suffix:
                    continue
                raise
            if suffix and actual.st_nlink == 0:
                continue
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_nlink != 1
                or actual.st_uid not in trusted_uids
                or stat.S_IMODE(actual.st_mode) != 0o600
                or (
                    not suffix
                    and (actual.st_dev, actual.st_ino) != (metadata.st_dev, metadata.st_ino)
                )
            ):
                raise OSError("Database file permissions could not be verified")
    except OSError as exc:
        raise OSError(
            "Could not protect the database files. Use regular files owned by "
            "root, the WUDup account, or configured OUT_UID, without symbolic "
            "or hard links, and ensure the WUDup account can set "
            "owner-only permissions (0600)."
        ) from exc
    return path
