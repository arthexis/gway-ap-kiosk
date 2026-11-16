"""Offline-only private state backup and sandbox restore verifier.

Explicitly refuses live system paths; never accesses nft, systemctl or
network. The snapshot includes the pending marker; it does not clear it.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

FILES = ("authorized_macs.txt", "trusted_macs.txt", "consents.jsonl",
         "activity.jsonl", "authorization.pending")
MAX_FILE = 8 * 1024 * 1024


class BackupError(RuntimeError):
    pass


def _directory(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise BackupError("unsafe or missing backup directory") from exc
    if os.fstat(fd).st_mode & 0o077:
        os.close(fd)
        raise BackupError("directory must be private")
    return fd


def _read(fd: int, name: str) -> bytes | None:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackupError("unsafe backup entry") from exc
    try:
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode)
                or before.st_mode & 0o077 or before.st_nlink != 1):
            raise BackupError("unsafe backup file")
        with os.fdopen(file_fd, "rb", closefd=False) as reader:
            data = reader.read(MAX_FILE + 1)
        after = os.fstat(file_fd)
        if len(data) > MAX_FILE or any(
            getattr(before, key) != getattr(after, key)
            for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise BackupError("unstable or oversized backup file")
        return data
    finally:
        os.close(file_fd)


def _write_new(fd: int, name: str, data: bytes) -> None:
    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                  0o600, dir_fd=fd)
    try:
        with os.fdopen(out, "wb", closefd=False) as file:
            file.write(data)
            file.flush()
            os.fsync(out)
    finally:
        os.close(out)


def create_snapshot(source: Path, destination: Path) -> dict:
    """Create a new private snapshot; never overwrite or delete source files."""
    source_fd = _directory(source)
    try:
        # Caller must choose an absent path on a trusted local filesystem.
        destination.mkdir(mode=0o700)
        dest_fd = _directory(destination)
        try:
            inventory = {}
            for name in FILES:
                data = _read(source_fd, name)
                if data is None:
                    inventory[name] = None
                    continue
                _write_new(dest_fd, name, data)
                inventory[name] = hashlib.sha256(data).hexdigest()
            manifest = {"schema": 1, "files": inventory,
                        "restore_approved": False,
                        "firewall_verified": False}
            _write_new(dest_fd, "manifest.json",
                       (json.dumps(manifest, sort_keys=True) + "\n").encode())
            os.fsync(dest_fd)
            return manifest
        finally:
            os.close(dest_fd)
    finally:
        os.close(source_fd)


def verify_snapshot(snapshot: Path) -> dict:
    fd = _directory(snapshot)
    try:
        raw = _read(fd, "manifest.json")
        if raw is None or len(raw) > 8192:
            raise BackupError("missing or invalid manifest")
        try:
            manifest = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise BackupError("invalid manifest") from exc
        if (not isinstance(manifest, dict)
                or set(manifest) != {"schema", "files", "restore_approved", "firewall_verified"}
                or manifest["schema"] != 1
                or manifest["restore_approved"] is not False
                or manifest["firewall_verified"] is not False
                or not isinstance(manifest["files"], dict)
                or set(manifest["files"]) != set(FILES)):
            raise BackupError("untrusted manifest schema")
        for name in FILES:
            payload = _read(fd, name)
            expected = manifest["files"][name]
            if (payload is None) != (expected is None):
                raise BackupError("snapshot file missing or unexpected")
            if payload is not None and hashlib.sha256(payload).hexdigest() != expected:
                raise BackupError("snapshot mismatch")
        return {"integrity_verified": True, "restore_approved": False,
                "firewall_verified": False, "uncertain_marker_preserved":
                manifest["files"]["authorization.pending"] is not None}
    finally:
        os.close(fd)


def restore_to_empty_sandbox(snapshot: Path, target: Path) -> dict:
    """Dry-run restoration test only. Never targets a live AP state directory."""
    # Never accept arbitrary paths or symlinked parents as a restore target.
    # Only create a new sandbox directly under the system temporary directory.
    root = Path(tempfile.gettempdir()).resolve()
    if target.parent != root or target.is_symlink() or target.exists():
        raise BackupError("restore requires a new direct temporary sandbox")
    receipt = verify_snapshot(snapshot)
    target.mkdir(mode=0o700)
    fd = _directory(snapshot)
    out_fd = _directory(target)
    try:
        for name in FILES:
            payload = _read(fd, name)
            if payload is not None:
                _write_new(out_fd, name, payload)
        os.fsync(out_fd)
    finally:
        os.close(fd)
        os.close(out_fd)
    return dict(receipt, sandbox_restore_verified=True)
