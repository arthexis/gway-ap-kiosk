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
from portal_host_grants import compare_source_and_live, check_file

NFT_CONF = "/etc/nftables.conf"
BROKER_LOCK = "/run/gway-ap-nft/coord.lock"
RECONCILER = "gway-ap-host-security-reconcile.service"


class ReloadError(RuntimeError):
    pass


def dynamic_eligible() -> set[str]:
    """Grant additions represented by persisted, explicitly consenting users."""
    from portal_authorization_reconcile import load_macs
    from portal_captive_policy import eligible_grants, read_consented_macs
    from portal_grant_intent import GrantIntent
    path = Path("/var/lib/gway-ap-kiosk/portal")
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise ReloadError("unsafe private portal state")
    GrantIntent(path).ensure_clean()
    authorized = load_macs(path / "authorized_macs.txt")
    trusted = load_macs(path / "trusted_macs.txt")
    consented = read_consented_macs(path / "consents.jsonl")
    return eligible_grants(authorized, trusted, consented)


def _check(result):
    if result.returncode:
        raise ReloadError("nftables reload/reconciliation failed")


def reload_with_reconcile(*, lock: Path = Path(BROKER_LOCK),
                          config: str = NFT_CONF, runner=subprocess.run, root_uid: int = 0) -> None:
    # Same root-owned lock as the broker. Serialize full-table re-creation
    # against its additive grant batches, not against portal state writes.
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != root_uid or
                info.st_mode & 0o077 or info.st_nlink != 1):
            raise ReloadError("untrusted reload coordination lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReloadError("another firewall writer active") from exc
        # Refuse the destructive flush if an independent writer has added
        # host grants not yet represented in its persistent owned source.
        live = runner(["nft", "-j", "list", "set", "inet",
                       "host_security", "ap_allowed_macs"],
                      timeout=8, capture_output=True, check=False)
        _check(live)
        compare_source_and_live(
            check_file(Path("/etc/nftables.d/host-security.nft")),
            live.stdout, consented_dynamic=dynamic_eligible())
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
    except (OSError, ReloadError, subprocess.TimeoutExpired, ValueError) as exc:
        raise SystemExit("AP nftables reload incomplete; hold access and inspect receipt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
