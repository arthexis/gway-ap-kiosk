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
    if path.is_symlink():
        raise CoordinationError("unsafe authorization path")
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

def reconcile(state_dir: Path, *, coordinator=None, require_consent: bool = False) -> None:
    from portal_firewall_coordinator import CoordinationError
    if not state_dir.is_dir() or state_dir.is_symlink():
        raise CoordinationError("invalid state directory")
    fd = os.open(state_dir / "authorization.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        from portal_grant_intent import GrantIntent
        GrantIntent(state_dir).ensure_clean()
        authorized = load_macs(state_dir / "authorized_macs.txt")
        trusted = load_macs(state_dir / "trusted_macs.txt")
        nft = coordinator or NftGrantCoordinator(require_captive=require_consent)
        if require_consent:
            from portal_captive_policy import eligible_grants, read_consented_macs
            grants = eligible_grants(authorized, trusted,
                                     read_consented_macs(state_dir / "consents.jsonl"))
        else:
            grants = authorized | trusted
        if grants:
            nft.add(grants)
        else:
            nft.snapshot()  # fail closed if either set is missing
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def main() -> int:
    from portal_firewall_coordinator import CoordinationError
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--broker-socket", type=Path, required=True)
    parser.add_argument("--require-consent", action="store_true",
                        help="Only reconcile grants tied to identified consent")
    args = parser.parse_args()
    if os.environ.get("GWAY_AP_CUTOVER_APPROVED") != "1":
        parser.error("coordinated cutover not approved; refusing legacy synchronization")
    try:
        from portal_nft_client import BrokerClient
        from portal_grant_intent import GrantIntent
        GrantIntent(args.state_dir).ensure_clean()
        reconcile(args.state_dir, coordinator=BrokerClient(args.broker_socket),
                  require_consent=args.require_consent)
    except (CoordinationError, OSError) as exc:
        # Never include MACs, consent records or command output in logs.
        parser.exit(1, "AP authorization reconciliation failed closed\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
