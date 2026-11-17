#!/usr/bin/env python3
"""Read-only proof that host_security grants survive nftables full reloads.

The captive policy must never capture or replace independently owned grants.
This compares the EXACT static host-security grant source with the live nft set,
without printing MACs or modifying any nft table.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

from portal_firewall_coordinator import MAC, _elements, CoordinationError


class HostGrantError(RuntimeError):
    pass


def static_grants(source: str) -> set[str]:
    blocks = re.findall(r"\bset\s+ap_allowed_macs\s*\{(.*?)\n\s*\}", source, re.S)
    if len(blocks) != 1:
        raise HostGrantError("ambiguous host-security grant source")
    body = blocks[0]
    if not re.search(r"\btype\s+ether_addr\b", body):
        raise HostGrantError("unexpected grant type")
    m = re.findall(r"\belements\s*=\s*\{([^{}]*)\}", body)
    if len(m) != 1:
        raise HostGrantError("grants lack static persistence")
    values = [v.strip() for v in m[0].split(",") if v.strip()]
    if not values or any(not MAC.fullmatch(v) for v in values):
        raise HostGrantError("invalid static host-security grants")
    norm = {v.lower() for v in values}
    if len(values) != len(norm):
        raise HostGrantError("duplicate static grants")
    return norm


def compare_source_and_live(source: str, nft_json: str) -> int:
    static = static_grants(source)
    try:
        current = _elements(json.loads(nft_json), "host_security", "ap_allowed_macs")
    except (ValueError, TypeError, CoordinationError) as exc:
        raise HostGrantError("live host-security set unavailable") from exc
    if static != current:
        raise HostGrantError("dynamic host grants differ from persistent source")
    return len(static)


def check_file(path: Path) -> str:
    if path.is_symlink():
        raise HostGrantError("symlink source")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 \
                or info.st_mode & 0o022 or info.st_size > 256 * 1024:
            raise HostGrantError("unsafe host-security source")
        return os.read(fd, 256 * 1024 + 1).decode("utf-8")
    finally:
        os.close(fd)


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host-source", type=Path, required=True)
    args = p.parse_args()
    if os.geteuid() != 0:
        p.error("host-security check requires root")
    try:
        result = subprocess.run(
            ["nft", "-j", "list", "set", "inet", "host_security", "ap_allowed_macs"],
            capture_output=True, text=True, check=False, timeout=8)
        if result.returncode:
            raise HostGrantError("cannot read independent host grants")
        count = compare_source_and_live(check_file(args.host_source), result.stdout)
    except (HostGrantError, UnicodeError, OSError, subprocess.TimeoutExpired):
        p.exit(1, "independent_host_grant_proof=FAILED\n")
    print("independent_host_grant_proof=PASS count=" + str(count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
