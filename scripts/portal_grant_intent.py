"""Durable fail-closed intent marker for AP portal grant transactions.

This is a crash *detection* barrier, not a general rollback engine. A process
interrupted after firewall application may leave an applied grant; recovery
blocks automatic service start pending operator-controlled reconciliation.
"""
from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path


class RecoveryRequired(RuntimeError):
    pass


class GrantIntent:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.path = state_dir / "authorization.pending"

    def ensure_clean(self):
        if self.path.exists() or self.path.is_symlink():
            raise RecoveryRequired("uncertain portal grant; operator recovery required")

    def begin(self):
        self.ensure_clean()
        token = uuid.uuid4().hex
        temp = self.state_dir / ("authorization.pending." + token)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(json.dumps({"schema": 1, "status": "uncertain"}).encode() + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            self._fsync_dir()
        finally:
            temp.unlink(missing_ok=True)

    def finish(self):
        # Call only after success OR complete proved compensation.
        if not self.path.is_file() or self.path.is_symlink():
            raise RecoveryRequired("missing or unsafe grant journal")
        if stat.S_IMODE(self.path.stat().st_mode) & 0o077:
            raise RecoveryRequired("unsafe grant journal permissions")
        self.path.unlink()
        self._fsync_dir()

    def _fsync_dir(self):
        fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
