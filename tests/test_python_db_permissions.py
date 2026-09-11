from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wudup.db import connect_db, init_db, open_db
from wudup.file_ops import OwnerConfig
from wudup.updater_audit import apply_sqlite_owner


class DatabasePermissionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wudup-db-permissions.")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "state" / "wud.sqlite"

    def state_paths(self) -> tuple[Path, ...]:
        return tuple(Path(f"{self.path}{suffix}") for suffix in ("", "-wal", "-shm"))

    def assert_private(self, paths: tuple[Path, ...]) -> None:
        for path in paths:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)

    def foreign_owner(self, *paths: Path):
        real_lstat = Path.lstat
        foreign_uid = os.getuid() + 10000

        def owned_lstat(path):
            metadata = real_lstat(path)
            if path in paths:
                fields = list(metadata)
                fields[4] = foreign_uid
                return os.stat_result(fields)
            return metadata

        return patch.object(Path, "lstat", owned_lstat)

    def test_foreign_0755_ancestor_rejected_before_creating_private_child(self) -> None:
        ancestor = self.root / "foreign"
        ancestor.mkdir(mode=0o755)
        path = ancestor / "private" / "wud.sqlite"
        with self.foreign_owner(ancestor), patch("wudup.db.sqlite3.connect") as connect:
            with self.assertRaisesRegex(OSError, "ancestors are owned"):
                connect_db(path)
            connect.assert_not_called()
        self.assertFalse(path.parent.exists())
        self.assertEqual(stat.S_IMODE(ancestor.stat().st_mode), 0o755)

    def test_foreign_ancestor_above_existing_private_directory_stops_before_chmod(self) -> None:
        self.path.parent.mkdir(mode=0o700)
        self.path.write_bytes(b"keep")
        self.path.chmod(0o644)
        with self.foreign_owner(self.root), patch("wudup.db.sqlite3.connect") as connect:
            with self.assertRaisesRegex(OSError, "ancestors are owned"):
                connect_db(self.path)
            connect.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"keep")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_foreign_alias_and_chained_target_cannot_hide_ownership(self) -> None:
        self.path.parent.mkdir()
        foreign = self.root / "foreign-alias"
        foreign.symlink_to(self.path.parent)
        trusted_alias = self.root / "trusted-alias"
        trusted_alias.symlink_to(foreign)
        for alias in (foreign, trusted_alias):
            with self.subTest(alias=alias.name), self.foreign_owner(foreign):
                with self.assertRaisesRegex(OSError, "untrusted or looping"):
                    connect_db(alias / self.path.name)
        self.assertFalse(self.path.exists())

    def test_foreign_alias_target_and_dotdot_prefix_are_rejected(self) -> None:
        foreign = self.root / "foreign"
        foreign.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(foreign)
        for path in (alias / self.path.name, foreign / ".." / "state" / self.path.name):
            with self.subTest(path=str(path)), self.foreign_owner(foreign):
                with self.assertRaisesRegex(OSError, "ancestors are owned"):
                    connect_db(path)
        self.assertFalse(self.path.exists())

    def test_mkdir_race_revalidates_foreign_directory_or_alias(self) -> None:
        real_mkdir = Path.mkdir
        destination = self.root / "destination"
        destination.mkdir()
        for alias in (False, True):
            with self.subTest(alias=alias):
                def racing_mkdir(path, *args, alias=alias, **kwargs):
                    if path == self.path.parent and alias:
                        path.symlink_to(destination)
                    else:
                        real_mkdir(path, *args, **kwargs)

                with (
                    patch.object(Path, "mkdir", racing_mkdir),
                    self.foreign_owner(self.path.parent),
                    patch("wudup.db.sqlite3.connect") as connect,
                ):
                    with self.assertRaisesRegex(OSError, "Could not protect"):
                        connect_db(self.path)
                    connect.assert_not_called()
                self.assertFalse(self.path.exists())
                if alias:
                    self.path.parent.unlink()
                else:
                    self.path.parent.rmdir()

    def test_configured_owner_is_trusted_for_directories_aliases_and_files(self) -> None:
        with open_db(self.path) as conn:
            init_db(conn)
            alias = self.root / "alias"
            alias.symlink_to(self.path.parent)
            with self.foreign_owner(self.path.parent, alias, *self.state_paths()):
                with self.assertRaises(OSError):
                    connect_db(alias / self.path.name)
                with open_db(alias / self.path.name, owner_uid=os.getuid() + 10000) as other:
                    other.execute("INSERT INTO web_settings VALUES ('test', 'private', 'now')")
                    other.commit()
                self.assertEqual(
                    conn.execute("SELECT value FROM web_settings").fetchone()[0], "private"
                )
            self.assert_private(self.state_paths())

    def test_foreign_state_file_owner_rejected_before_permissions_change(self) -> None:
        with open_db(self.path) as conn:
            init_db(conn)
            for path in self.state_paths():
                with self.subTest(path=path.name):
                    path.chmod(0o644)
                    with self.foreign_owner(path), patch("wudup.db.sqlite3.connect") as connect:
                        with self.assertRaisesRegex(OSError, "Could not protect"):
                            connect_db(self.path)
                        connect.assert_not_called()
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
                    path.chmod(0o600)

    def test_programmatic_web_settings_propagate_configured_owner(self) -> None:
        from wudup.web import create_app

        self.path.parent.mkdir()
        with self.foreign_owner(self.path.parent):
            app = create_app(environ={
                "WUD_DB_PATH": str(self.path),
                "OUT_UID": str(os.getuid() + 10000),
                "OUT_GID": str(os.getgid()),
            })
        self.assertTrue(app.state.web_setup_claim)
        with open_db(self.path) as conn:
            self.assertGreater(conn.execute("SELECT count(*) FROM web_settings").fetchone()[0], 0)

    def test_parent_alias_loop_fails_with_actionable_error(self) -> None:
        alias = self.root / "loop"
        alias.symlink_to(alias)
        with self.assertRaisesRegex(OSError, "untrusted or looping"):
            connect_db(alias / self.path.name)

    def test_permissive_umask_creates_private_files_before_secret_persistence(self) -> None:
        # Keep process-wide umask changes out of the threaded test process too.
        script = """
import os, sys
from pathlib import Path
from unittest.mock import patch
from wudup.db import open_db, init_db
os.umask(int(sys.argv[2], 8))
path = Path(sys.argv[1])
with patch('wudup.db.os.umask', side_effect=AssertionError('global umask change')):
    for _ in range(2):
        with open_db(path) as conn:
            init_db(conn)
            conn.execute("INSERT OR REPLACE INTO web_settings VALUES (?, ?, ?)",
                         ('discord.release_webhook_url', 'example-only-secret', 'now'))
            conn.commit()
            for suffix in ('', '-wal', '-shm'):
                assert Path(f'{path}{suffix}').stat().st_mode & 0o777 == 0o600
            assert conn.execute('SELECT value FROM web_settings').fetchone()[0] == 'example-only-secret'
assert path.parent.stat().st_mode & 0o777 == 0o700
"""
        for mask in ("000", "022", "077"):
            with self.subTest(umask=mask):
                path = self.root / mask / "nested" / "state" / "wud.sqlite"
                subprocess.run([sys.executable, "-c", script, str(path), mask], check=True)
                for parent in (path.parent.parent, path.parent.parent.parent):
                    self.assertEqual(parent.stat().st_mode & 0o022, 0)

    def test_existing_database_and_active_sidecars_are_private_before_connect(self) -> None:
        with open_db(self.path) as first:
            init_db(first)
            first.execute("INSERT INTO web_settings VALUES ('test', 'secret', 'now')")
            first.commit()
            paths = self.state_paths()
            owners = [(p.stat().st_uid, p.stat().st_gid) for p in paths]
            for path in paths:
                path.chmod(0o666)
            self.path.parent.chmod(0o755)
            sibling = self.path.parent / "unrelated.log"
            sibling.write_text("keep")
            sibling.chmod(0o644)
            real_connect = sqlite3.connect

            def checked_connect(*args, **kwargs):
                self.assert_private(paths)
                return real_connect(*args, **kwargs)

            with patch("wudup.db.sqlite3.connect", side_effect=checked_connect):
                with open_db(self.path) as second:
                    self.assertEqual(
                        second.execute("SELECT value FROM web_settings").fetchone()[0],
                        "secret",
                    )
            self.assertEqual([(p.stat().st_uid, p.stat().st_gid) for p in paths], owners)
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(sibling.stat().st_mode), 0o644)

    def test_existing_rollback_journal_is_protected_before_sqlite_opens(self) -> None:
        self.path.parent.mkdir()
        self.path.touch(mode=0o644)
        journal = Path(f"{self.path}-journal")
        journal.write_bytes(b"legacy journal")
        journal.chmod(0o644)
        with patch("wudup.db.sqlite3.connect", return_value=Mock()):
            connect_db(self.path)
        self.assert_private((self.path, journal))
        self.assertEqual(journal.read_bytes(), b"legacy journal")

    def test_rejects_links_and_nonregular_files_without_changing_targets(self) -> None:
        self.path.parent.mkdir()
        target = self.root / "unrelated"
        target.write_text("keep")
        target.chmod(0o644)
        for suffix in ("", "-wal", "-shm", "-journal"):
            for kind in ("symlink", "hardlink", "directory", "fifo"):
                with self.subTest(suffix=suffix, kind=kind):
                    candidate = Path(f"{self.path}{suffix}")
                    if candidate.exists():
                        candidate.unlink()
                    if kind == "symlink":
                        candidate.symlink_to(target)
                    elif kind == "hardlink":
                        os.link(target, candidate)
                    elif kind == "directory":
                        candidate.mkdir()
                    else:
                        os.mkfifo(candidate)
                    with patch("wudup.db.sqlite3.connect") as connect:
                        with self.assertRaisesRegex(OSError, "Could not protect"):
                            connect_db(self.path)
                        connect.assert_not_called()
                    if kind == "directory":
                        candidate.rmdir()
                    else:
                        candidate.unlink()
                    self.assertEqual(target.read_text(), "keep")
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_permissions_failure_stops_before_sqlite_opens(self) -> None:
        self.path.parent.mkdir()
        self.path.touch()
        self.path.chmod(0o644)
        for failure in (PermissionError("denied"), None):
            with self.subTest(failure=failure):
                with (
                    patch.object(Path, "chmod", side_effect=failure),
                    patch("wudup.db.sqlite3.connect") as connect,
                ):
                    with self.assertRaisesRegex(OSError, "owner-only permissions"):
                        connect_db(self.path)
                    connect.assert_not_called()

    def test_database_create_failure_stops_before_sqlite_opens(self) -> None:
        with (
            patch("wudup.db.os.open", side_effect=PermissionError("denied")),
            patch("wudup.db.sqlite3.connect") as connect,
        ):
            with self.assertRaisesRegex(OSError, "Could not protect"):
                connect_db(self.path)
            connect.assert_not_called()

    def test_sidecar_cleanup_and_private_recreation_during_inspection(self) -> None:
        self.path.parent.mkdir()
        sidecar = Path(f"{self.path}-wal")
        real_lstat = Path.lstat
        for action in ("unlinked", "recreated", "unsafe_recreation"):
            with self.subTest(action=action):
                sidecar.touch(mode=0o600)
                inspected = []

                def racing_lstat(path, action=action, inspected=inspected):
                    metadata = real_lstat(path)
                    if path == sidecar and not inspected:
                        inspected.append(path)
                        sidecar.unlink()
                        if action == "unlinked":
                            fields = list(metadata)
                            fields[3] = 0  # st_nlink can be zero during unlink.
                            return os.stat_result(fields)
                        sidecar.touch(mode=0o600)
                        if action == "unsafe_recreation":
                            sidecar.chmod(0o644)
                    return metadata

                with (
                    patch.object(Path, "lstat", racing_lstat),
                    patch("wudup.db.sqlite3.connect", return_value=Mock()) as connect,
                ):
                    if action == "unsafe_recreation":
                        with self.assertRaisesRegex(OSError, "Could not protect"):
                            connect_db(self.path)
                        connect.assert_not_called()
                    else:
                        connect_db(self.path)
                        connect.assert_called_once()
                self.assertEqual(inspected, [sidecar])
                sidecar.unlink(missing_ok=True)

    def test_rejects_shared_writable_directories_without_chmod(self) -> None:
        self.path.parent.mkdir()
        for directory in (self.path.parent, self.root):
            for mode in (0o775, 0o777):
                with self.subTest(directory=directory.name, mode=mode):
                    directory.chmod(mode)
                    try:
                        with self.assertRaisesRegex(OSError, "Set WUD_DB_PATH"):
                            connect_db(self.path)
                        self.assertFalse(self.path.exists())
                        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), mode)
                    finally:
                        directory.chmod(0o700)

    def test_resolves_parent_alias_and_preserves_configured_owner_handoff(self) -> None:
        self.path.parent.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.path.parent, target_is_directory=True)
        owner = OwnerConfig(os.getuid(), os.getgid())
        with open_db(alias / self.path.name) as conn:
            init_db(conn)
            apply_sqlite_owner(self.path, owner, chown_parent=True)
            self.assert_private(self.state_paths())
            for path in (self.path.parent, *self.state_paths()):
                self.assertEqual((path.stat().st_uid, path.stat().st_gid), (owner.uid, owner.gid))
            # The existing root/container handoff still targets only state files.
            with patch("wudup.file_ops.os.chown") as chown:
                apply_sqlite_owner(self.path, OwnerConfig(12345, 12345), chown_parent=True)
            self.assertEqual(
                [call.args for call in chown.call_args_list],
                [(path, 12345, 12345) for path in (self.path.parent, *self.state_paths())],
            )
        with open_db(self.path) as reopened:
            self.assertEqual(reopened.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_second_connection_does_not_release_first_connections_file_locks(self) -> None:
        script = """
import fcntl, sys
with open(sys.argv[1], 'r+b') as file:
    try:
        fcntl.lockf(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    sys.exit('database lock was lost')
"""
        with open_db(self.path) as first:
            init_db(first)
            first.execute("BEGIN IMMEDIATE")
            with open_db(self.path):
                subprocess.run([sys.executable, "-c", script, str(self.path)], check=True)
            first.rollback()

    def test_failed_pragma_closes_connection(self) -> None:
        conn = Mock()
        conn.execute.side_effect = sqlite3.OperationalError("pragma failed")
        with patch("wudup.db.sqlite3.connect", return_value=conn):
            with self.assertRaisesRegex(sqlite3.OperationalError, "pragma failed"):
                connect_db(":memory:")
        conn.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
