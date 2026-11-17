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
    def __init__(self, *, run=_run, require_captive: bool = False):
        self.run = run
        self.require_captive = require_captive

    def verify_captive(self):
        """Check live forward hook and fail-closed terminal wlan0 drop.

        Exact four-rule shape is required: registered MAC accept, two
        preauth destination exceptions limited to HTTP(S), then drop.
        Unknown/broader rules are rejected, not treated as authorized.
        """
        raw = self.run(["nft", "-j", "list", "chain", "inet",
                        "gway_ap_portal", "forward"])
        try:
            entries = json.loads(raw)["nftables"]
            chains = [x["chain"] for x in entries if "chain" in x]
            rules = [x["rule"] for x in entries if "rule" in x]
            if len(chains) != 1 or len(rules) != 4:
                raise ValueError("unexpected captive rules")
            c = chains[0]
            if not (c.get("family") == "inet" and
                    c.get("table") == "gway_ap_portal" and
                    c.get("name") == "forward" and
                    c.get("hook") == "forward" and
                    c.get("prio") == -5 and
                    c.get("type") == "filter"):
                raise ValueError("captive chain is not a forward hook")
            expected_sets = ("authorized_macs", "preauth_ipv4", "preauth_ipv6")
            for index, rule in enumerate(rules):
                if (rule.get("family") != "inet" or
                    rule.get("table") != "gway_ap_portal" or
                    rule.get("chain") != "forward"):
                    raise ValueError("unexpected captive rule location")
                expr = rule.get("expr")
                if not isinstance(expr, list) or len(expr) < (2 if index == 3 else 3):
                    raise ValueError("incomplete captive rule")
                gate = expr[0].get("match", {})
                if gate != {"op": "==", "left": {"meta": {"key": "iifname"}},
                            "right": "wlan0"}:
                    raise ValueError("captive rule is not wlan0-scoped")
                verdict = "drop" if index == 3 else "accept"
                if list(expr[-1]) != [verdict]:
                    raise ValueError("unexpected captive verdict")
                if index < 3:
                    required_set = ("authorized_macs", "preauth_ipv4", "preauth_ipv6")[index]
                    if len(expr) != (3 if index == 0 else 4):
                        raise ValueError("unexpected captive exception shape")
                    match = expr[1].get("match")
                    expected_source = {"protocol": "ether", "field": "saddr"}
                    expected_destination = {"protocol": "ip" if index == 1 else "ip6",
                                            "field": "daddr"}
                    if not isinstance(match, dict) or match.get("op") != "==" or (
                            match.get("left") != {"payload": expected_source if index == 0
                                                  else expected_destination}) or (
                            match.get("right") != {"set": "@" + required_set}):
                        raise ValueError("untrusted captive exception")
                    if index > 0:
                        port = expr[2].get("match")
                        if (not isinstance(port, dict) or port.get("op") != "==" or
                                port.get("left") != {"payload": {
                                    "protocol": "tcp", "field": "dport"}} or
                                port.get("right") != {"set": [80, 443]}):
                            raise ValueError("widened pre-auth destination ports")
                elif len(expr) != 2:
                    raise ValueError("nonterminal or conditional captive drop")
        except (KeyError, ValueError, TypeError) as exc:
            raise CoordinationError("captive forwarding policy unverified") from exc

    def snapshot(self):
        if self.require_captive:
            self.verify_captive()
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

