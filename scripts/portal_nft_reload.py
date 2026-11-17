#!/usr/bin/env python3
"""Serialized nftables reload with nonblocking AP grant reconciliation.

Activated only through an explicitly approved systemd drop-in. If the
reconciler cannot be queued after an nftables flush, the service reports
failure rather than claiming access was restored. Never clears UNCERTAIN.
"""
from __future__ import annotations

import fcntl
import os
import stat
import subprocess
from pathlib import Path

NFT_CONF = "/etc/nftables.conf"
BROKER_LOCK = "/run/gway-ap-nft/coord.lock"
RECONCILER = "gway-ap-host-security-reconcile.service"


class ReloadError(RuntimeError):
    pass


def _check(result):
    if result.returncode:
        raise ReloadError("nftables reload/reconciliation failed")


def reload_with_reconcile(*, lock: Path = Path(BROKER_LOCK),
                          config: str = NFT_CONF, runner=subprocess.run) -> None:
    # Same root-owned lock as the broker. Serialize full-table re-creation
    # against its additive grant batches, not against portal state writes.
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                info.st_mode & 0o077 or info.st_nlink != 1):
            raise ReloadError("untrusted reload coordination lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReloadError("another firewall writer active") from exc
        # Refuse the destructive flush if an independent writer has added
        # host grants not yet represented in its persistent owned source.
        from portal_host_grants import compare_source_and_live, check_file
        live = runner(["nft", "-j", "list", "set", "inet",
                       "host_security", "ap_allowed_macs"],
                      timeout=8, capture_output=True, check=False)
        _check(live)
        compare_source_and_live(
            check_file(Path("/etc/nftables.d/host-security.nft")),
            live.stdout)
        _check(runner(["nft", "-c", "-f", config], timeout=15,
                      capture_output=True, check=False))
        _check(runner(["nft", "-f", config], timeout=15,
                      capture_output=True, check=False))
    finally:
        os.close(fd)
    # Must be outside the broker lock: reconciliation needs the same lock.
    _check(runner(["systemctl", "--no-block", "start", RECONCILER],
                  timeout=8, capture_output=True, check=False))


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("reload requires root")
    try:
        reload_with_reconcile()
    except (OSError, ReloadError, subprocess.TimeoutExpired, ValueError):
        raise SystemExit("AP nftables reload incomplete; hold access and inspect receipt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
