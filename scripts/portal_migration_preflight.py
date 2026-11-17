#!/usr/bin/env python3
"""Read-only, redacted preflight for an existing-host AP portal migration.

No backup, deployment, nftables modification, or service restart is performed.
The report intentionally never includes state-file contents or MAC addresses.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

STATE_FILES = ("authorized_macs.txt", "trusted_macs.txt", "consents.jsonl", "activity.jsonl")


def command_ok(*command: str) -> bool | None:
    try:
        result = subprocess.run(command, capture_output=True, timeout=5, check=False)
        # A failed read can mean insufficient privilege, not absence.  Never
        # turn permission errors or unavailable tools into negative evidence.
        return True if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def inspect(state_dir: Path, *, command=command_ok) -> dict:
    # Report existence only; state data and even record counts are deliberately withheld.
    files = {name: (state_dir / name).is_file() for name in STATE_FILES}
    host_table = command("nft", "list", "table", "inet", "host_security")
    host_set = command("nft", "list", "set", "inet", "host_security", "ap_allowed_macs")
    portal_table = command("nft", "list", "table", "inet", "gway_ap_portal")
    old_service = command("systemctl", "cat", "arthexis-ap-portal.service")
    new_service = command("systemctl", "cat", "gway-ap-portal.service")
    return {
        "schema": 1,
        "read_only": True,
        "state_file_presence": files,
        "host_firewall_table_present": host_table,
        "host_firewall_ap_set_present": host_set,
        "portal_firewall_table_present": portal_table,
        "legacy_service_unit_present": old_service,
        "replacement_service_unit_present": new_service,
        "ready_for_automatic_migration": False,
        "reason": "Manual firewall ownership, grant provenance, backups and rollback review required",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/home/arthe/gway-ap-kiosk/.state/ap_portal"))
    args = parser.parse_args()
    print(json.dumps(inspect(args.state_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
