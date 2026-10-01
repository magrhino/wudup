"""Atomic WUD file rewrites and metadata helpers."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_NUMERIC_ID_RE = re.compile(r"^\d+$", re.ASCII)
_OWNER_PAIR_ERROR = "OUT_UID and OUT_GID/OUT_GUID must be set together"


class OwnerConfigError(ValueError):
    """Raised when OUT_UID/OUT_GID configuration is invalid."""


@dataclass(frozen=True)
class OwnerConfig:
    uid: int | None = None
    gid: int | None = None

    @property
    def configured(self) -> bool:
        return self.uid is not None or self.gid is not None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> OwnerConfig:
        return cls.from_values(
            env.get("OUT_UID"),
            env.get("OUT_GID"),
            env.get("OUT_GUID"),
        )

    @classmethod
    def from_values(
        cls,
        out_uid: int | str | None = None,
        out_gid: int | str | None = None,
        out_guid: int | str | None = None,
    ) -> OwnerConfig:
        gid_value = out_gid if _present(out_gid) else out_guid

        if _present(out_uid) or _present(gid_value):
            if not _present(out_uid) or not _present(gid_value):
                raise OwnerConfigError(_OWNER_PAIR_ERROR)
            return cls(
                uid=_parse_numeric_id("OUT_UID", out_uid),
                gid=_parse_numeric_id("OUT_GID/OUT_GUID", gid_value),
            )

        return cls()


@dataclass(frozen=True)
class FileMetadata:
    mode: int
    uid: int
    gid: int


def read_metadata(path: str | Path) -> FileMetadata:
    st = Path(path).stat()
    return FileMetadata(
        mode=stat.S_IMODE(st.st_mode),
        uid=st.st_uid,
        gid=st.st_gid,
    )


def desired_metadata(
    path: str | Path,
    *,
    owner: OwnerConfig | None = None,
    default_mode: int = 0o660,
) -> FileMetadata:
    target = Path(path)
    owner = owner or OwnerConfig()

    if target.exists():
        metadata = read_metadata(target)
    else:
        uid = os.getuid()
        gid = os.getgid()
        if uid == 0:
            uid = 1000
            gid = 1000
        metadata = FileMetadata(mode=default_mode, uid=uid, gid=gid)

    if owner.configured:
        if owner.uid is None or owner.gid is None:
            raise OwnerConfigError(_OWNER_PAIR_ERROR)
        metadata = FileMetadata(mode=metadata.mode, uid=owner.uid, gid=owner.gid)

    return metadata


def apply_configured_owner(path: str | Path, owner: OwnerConfig | None = None) -> None:
    owner = owner or OwnerConfig()
    if not owner.configured:
        return
    if owner.uid is None or owner.gid is None:
        raise OwnerConfigError(_OWNER_PAIR_ERROR)

    target = Path(path)
    st = target.stat()
    if st.st_uid != owner.uid or st.st_gid != owner.gid:
        os.chown(target, owner.uid, owner.gid)


def apply_metadata(path: str | Path, metadata: FileMetadata) -> None:
    target = Path(path)
    st = target.stat()
    if st.st_uid != metadata.uid or st.st_gid != metadata.gid:
        os.chown(target, metadata.uid, metadata.gid)
    os.chmod(target, metadata.mode)

    actual = read_metadata(target)
    if actual != metadata:
        raise OSError(
            "Metadata verification failed for "
            f"{target}: wanted {metadata.mode:o} {metadata.uid}:{metadata.gid}, "
            f"got {actual.mode:o} {actual.uid}:{actual.gid}"
        )


def atomic_rewrite(
    path: str | Path,
    content: str,
    *,
    owner: OwnerConfig | None = None,
    metadata_source: str | Path | None = None,
    default_mode: int = 0o660,
    create_parent: bool = False,
    encoding: str = "utf-8",
) -> None:
    """Replace a file through a temporary file in the target directory."""

    target = Path(path)
    if create_parent:
        target.parent.mkdir(parents=True, exist_ok=True)

    source = Path(metadata_source) if metadata_source is not None else target
    metadata = desired_metadata(source, owner=owner, default_mode=default_mode)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=str(target.parent),
    )
    tmp_path: Path | None = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as tmp:
            tmp.write(content)
        apply_metadata(tmp_path, metadata)
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _present(value: int | str | None) -> bool:
    return value is not None and str(value) != ""


def _parse_numeric_id(name: str, value: int | str | None) -> int:
    text = str(value)
    if _NUMERIC_ID_RE.fullmatch(text) is None:
        raise OwnerConfigError(f"{name} must be numeric")
    return int(text, 10)
