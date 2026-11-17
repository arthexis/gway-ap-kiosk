"""Root-owned cutover proof gate for the staged AP nft broker.

The gate records required operator assertions. It is not a substitute for
observing the actual deployed host services and nftables hooks.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

REQUIRED = (
    "legacy_portal_service_disabled",
    "legacy_sync_path_disabled",
    "legacy_sync_service_disabled",
    "host_firewall_writer_quiesced",
    "independent_grants_backed_up",
    "dedicated_portal_uid_verified",
    "uncertain_recovery_documented",
)


class CutoverDenied(RuntimeError):
    pass


def check_contract(path: Path, *, root_uid: int = 0) -> None:
    if path.is_symlink():
        raise CutoverDenied("unsafe cutover contract")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise CutoverDenied("missing or unreadable cutover contract") from exc
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != root_uid or meta.st_mode & 0o077:
            raise CutoverDenied("untrusted contract ownership")
        with os.fdopen(fd, "rb", closefd=False) as file:
            data = file.read(8193)
        if len(data) > 8192:
            raise CutoverDenied("oversize contract")
        try:
            contract = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise CutoverDenied("invalid contract") from exc
        if not isinstance(contract, dict) or set(contract) != {"schema", "assertions"}:
            raise CutoverDenied("invalid contract structure")
        assertions = contract["assertions"]
        if contract["schema"] != 1 or not isinstance(assertions, dict):
            raise CutoverDenied("invalid contract schema")
        if set(assertions) != set(REQUIRED) or any(assertions[x] is not True for x in REQUIRED):
            raise CutoverDenied("cutover assertions incomplete")
    finally:
        os.close(fd)
