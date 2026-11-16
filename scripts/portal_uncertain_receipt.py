#!/usr/bin/env python3
"""Redacted, read-only receipt for operator review of uncertain AP authorization.

Never clears a pending marker, adds grants, emits MACs, reads consent payloads,
or asserts that firewall state is reconciled.
"""
from __future__ import annotations
import argparse
import json
import os
import stat
from pathlib import Path

FILES = ("authorization.pending", "authorized_macs.txt",
         "trusted_macs.txt", "consents.jsonl", "activity.jsonl")


def collect(state_dir: Path) -> dict:
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise ValueError("unsafe or missing AP state directory")
    evidence = {}
    for name in FILES:
        path = state_dir / name
        if path.is_symlink():
            evidence[name] = "unsafe_symlink"
            continue
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            evidence[name] = "missing"
            continue
        if not stat.S_ISREG(mode):
            evidence[name] = "unsafe_type"
        elif stat.S_IMODE(mode) & 0o077:
            evidence[name] = "unsafe_permissions"
        else:
            evidence[name] = "private_regular"
    pending = evidence["authorization.pending"] != "missing"
    return {
        "schema": 1,
        "mode": "read_only",
        "uncertain": pending,
        "file_status": evidence,
        "firewall_verified": False,
        "independent_grants_verified": False,
        "state_backup_verified": False,
        "operator_release_allowed": False,
        "recovery": "Hold portal and legacy sync. Under an approved window, inspect both nft sets and base chains with a separate privileged operator, verify state backup and grant provenance, and document an independently reviewed resolution. Never delete the marker based solely on this receipt.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = collect(args.state_dir)
    except (ValueError, OSError):
        parser.exit(2, "AP recovery receipt failed closed\n")
    print(json.dumps(report, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
