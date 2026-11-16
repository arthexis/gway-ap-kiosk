#!/usr/bin/env python3
"""Cutover-gated additive reconciliation. Never flushes or removes nft grants."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path

from portal_firewall_coordinator import NftGrantCoordinator

def load_macs(path: Path) -> set[str]:
    from portal_firewall_coordinator import MAC, CoordinationError
    if not path.is_file():
        return set()
    result = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.split("#", 1)[0].strip().split()
        if not token:
            continue
        if not MAC.fullmatch(token[0]):
            raise CoordinationError("invalid authorization record")
        result.add(token[0].lower())
    return result

def reconcile(state_dir: Path, *, coordinator=None) -> None:
    from portal_firewall_coordinator import CoordinationError
    if not state_dir.is_dir() or state_dir.is_symlink():
        raise CoordinationError("invalid state directory")
    fd = os.open(state_dir / "authorization.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        authorized = load_macs(state_dir / "authorized_macs.txt")
        trusted = load_macs(state_dir / "trusted_macs.txt")
        nft = coordinator or NftGrantCoordinator()
        if authorized or trusted:
            nft.add(authorized | trusted)
        else:
            nft.snapshot()  # fail closed if either set is missing
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def main() -> int:
    from portal_firewall_coordinator import CoordinationError
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GWAY_AP_CUTOVER_APPROVED") != "1":
        parser.error("coordinated cutover not approved; refusing legacy synchronization")
    try:
        reconcile(args.state_dir)
    except (CoordinationError, OSError) as exc:
        # Never include MACs, consent records or command output in logs.
        parser.exit(1, "AP authorization reconciliation failed closed\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
