"""Fail-closed nftables grant coordination for the AP portal migration.

This module is NOT invoked by the existing portal until the host firewall
ownership contract is reviewed. Never resets a table or an independent grant.
"""
from __future__ import annotations

import json
import re
import subprocess

MAC = re.compile(r"(?i)^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
SETS = (("host_security", "ap_allowed_macs"),
        ("gway_ap_portal", "authorized_macs"))


class CoordinationError(RuntimeError):
    pass


def _run(argv, *, input=None):
    try:
        p = subprocess.run(argv, input=input, text=True, capture_output=True,
                           timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoordinationError("nft operation unavailable") from exc
    if p.returncode:
        raise CoordinationError("nft operation failed")
    return p.stdout


def _elements(document, table, name):
    try:
        entries = document["nftables"]
        sets = [entry["set"] for entry in entries if "set" in entry]
        matching = [item for item in sets if item.get("family") == "inet"
                    and item.get("table") == table and item.get("name") == name
                    and item.get("type") == "ether_addr"]
        if len(matching) != 1:
            raise ValueError("expected one ether_addr set")
        values = matching[0].get("elem", [])
        # nft JSON represents ether_addr elements as strings.
        if not isinstance(values, list) or any(not isinstance(v, str) or
                                                 not MAC.fullmatch(v) for v in values):
            raise ValueError("unexpected nft set elements")
        return {v.lower() for v in values}
    except (KeyError, TypeError, ValueError) as exc:
        raise CoordinationError("missing or incompatible nft set") from exc


class NftGrantCoordinator:
    """Coordinated additive grants; caller must serialize other writers.

    A single nft -f transaction ensures neither set is partially updated
    by a failed batch. No deletion API is exposed: foreign grants must
    never be flushed, replaced or revoked by portal failure recovery.
    """
    def __init__(self, *, run=_run):
        self.run = run

    def snapshot(self):
        result = {}
        for table, name in SETS:
            raw = self.run(["nft", "-j", "list", "set", "inet", table, name])
            try:
                data = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise CoordinationError("invalid nft JSON") from exc
            result[(table, name)] = _elements(data, table, name)
        return result

    def add(self, macs):
        grant = {str(m).lower() for m in macs}
        if not grant or any(not MAC.fullmatch(m) for m in grant):
            raise CoordinationError("invalid or empty MAC grant request")
        before = self.snapshot()
        delta = {key: grant - before[key] for key in SETS}
        commands = [f"add element inet {table} {name} {{ {', '.join(sorted(delta[(table, name)]))} }}"
                    for table, name in SETS if delta[(table, name)]]
        if commands:
            self.run(["nft", "-f", "-"], input="\n".join(commands) + "\n")
        return delta

