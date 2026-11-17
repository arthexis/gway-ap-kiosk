"""Restricted legacy AP state import for a reviewed, QUIESCENT cutover.

This creates a new private destination; never modifies source, nftables, or
services. Root operator must separately establish exclusive writer ownership,
verify firewall backup and approve cutover. An incomplete destination is NOT
valid and must be quarantined by an operator.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

FILES = ("authorized_macs.txt", "trusted_macs.txt", "consents.jsonl",
         "activity.jsonl", "authorization.pending")
MAX_FILE = 8 * 1024 * 1024


class ImportDenied(RuntimeError):
    pass


def _open_directory(path: Path, *, private: bool) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        meta = os.fstat(fd)
        if not stat.S_ISDIR(meta.st_mode) or meta.st_mode & 0o022:
            raise ImportDenied("unsafe directory permissions")
        if private and meta.st_mode & 0o077:
            raise ImportDenied("destination is not private")
        return fd
    except BaseException:
        if "fd" in locals():
            os.close(fd)
        raise


def _read_legacy(fd: int, name: str, *, source_uid: int) -> bytes | None:
    try:
        f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ImportDenied("unsafe legacy entry") from exc
    try:
        before = os.fstat(f)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != source_uid
                or before.st_nlink != 1 or before.st_mode & 0o022):
            raise ImportDenied("untrusted legacy file")
        with os.fdopen(f, "rb", closefd=False) as stream:
            data = stream.read(MAX_FILE + 1)
        after = os.fstat(f)
        if len(data) > MAX_FILE or any(getattr(before, x) != getattr(after, x)
                for x in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
            raise ImportDenied("unstable legacy file")
        return data
    finally:
        os.close(f)


def import_quiescent_legacy(source: Path, destination: Path, *,
                            source_uid: int, target_uid: int,
                            target_gid: int, change_owner: bool = True) -> dict:
    """Import into an absent path, never as implicit approval to activate.

    The read-twice check catches observed changes but cannot establish writer
    quiescence, power-loss atomicity or a firewall snapshot.
    """
    if source_uid < 0 or target_uid <= 0 or target_gid <= 0:
        raise ImportDenied("invalid identity")
    if destination.exists() or destination.is_symlink():
        raise ImportDenied("destination must not exist")
    # Prevent a privileged import from writing through an untrusted parent.
    parent = _open_directory(destination.parent, private=False)
    try:
        parent_meta = os.fstat(parent)
        if parent_meta.st_uid != source_uid:
            raise ImportDenied("untrusted destination parent")
    finally:
        os.close(parent)
    src = _open_directory(source, private=False)
    try:
        if os.fstat(src).st_uid != source_uid:
            raise ImportDenied("untrusted legacy directory owner")
        destination.mkdir(mode=0o700)
        dst = _open_directory(destination, private=True)
        try:
            evidence = {}
            for name in FILES:
                data = _read_legacy(src, name, source_uid=source_uid)
                evidence[name] = None if data is None else hashlib.sha256(data).hexdigest()
                if data is None:
                    continue
                out = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                              0o600, dir_fd=dst)
                try:
                    with os.fdopen(out, "wb", closefd=False) as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(out)
                    if change_owner:
                        os.fchown(out, target_uid, target_gid)
                finally:
                    os.close(out)
            for name in FILES:
                current = _read_legacy(src, name, source_uid=source_uid)
                digest = None if current is None else hashlib.sha256(current).hexdigest()
                if digest != evidence[name]:
                    raise ImportDenied("legacy source changed during import")
            os.fsync(dst)
            if change_owner:
                os.fchown(dst, target_uid, target_gid)
            # Receipt excludes file digests and private payloads on purpose.
            return {"schema": 1, "imported": True, "ready_for_activation": False,
                    "file_presence": {name: evidence[name] is not None for name in FILES},
                    "source_unchanged_observed": True,
                    "firewall_backup_verified": False,
                    "writer_quiescence_verified": False}
        finally:
            os.close(dst)
    finally:
        os.close(src)


def main() -> int:
    import argparse
    import json
    import pwd
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--destination", type=Path, required=True)
    p.add_argument("--target-user", default="gwayap")
    args = p.parse_args()
    if os.geteuid() != 0:
        p.error("privileged reviewed migration required")
    account = pwd.getpwnam(args.target_user)
    if account.pw_uid == 0 or account.pw_gid == 0 or args.target_user != "gwayap":
        p.error("dedicated non-root gwayap account required")
    result = import_quiescent_legacy(
        args.source, args.destination, source_uid=0,
        target_uid=account.pw_uid, target_gid=account.pw_gid)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
