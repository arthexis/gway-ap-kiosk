#!/usr/bin/env python3
"""Prepare, but never apply, a guarded existing-host captive boot configuration.

The host's nftables.conf begins with 'flush ruleset'. The persistent boot
configuration must restore both the host-security input table and the portal's
fail-closed forward chain after that flush. Nothing here calls nft, systemctl,
NetworkManager, or changes live files.
"""
from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

HOST_SOURCE = 'include "/etc/nftables.d/host-security.nft"'
PORTAL_SOURCE = 'include "/etc/nftables.d/gway-ap-captive.nft"'
BEGIN = "# BEGIN gway-ap-kiosk #31 managed boot includes"
END = "# END gway-ap-kiosk #31 managed boot includes"
MANAGED = "\n".join((BEGIN, HOST_SOURCE, PORTAL_SOURCE, END))


class BootRulesError(RuntimeError):
    pass


def render_boot(base: str) -> str:
    """Preserve all original bytes/lines except a terminal managed block."""
    if not isinstance(base, str) or not base:
        raise BootRulesError("empty boot policy")
    lines = base.splitlines()
    meaningful = [line.strip() for line in lines
                  if line.strip() and not line.lstrip().startswith("#")]
    if meaningful.count("flush ruleset") != 1:
        raise BootRulesError("expected one full ruleset flush")
    if "flush ruleset" != meaningful[0]:
        raise BootRulesError("flush must precede every managed table")
    if any("gway_ap_portal" in line for line in lines):
        raise BootRulesError("existing AP table requires operator review")
    if any("include " in line and ("nftables.d/*" in line or
                                     "nftables.d/?" in line) for line in lines):
        raise BootRulesError("wildcard nft include cannot be adopted")
    if BEGIN in base or END in base:
        if base.rstrip().endswith(MANAGED) and base.count(BEGIN) == 1 \
                and base.count(END) == 1:
            return base
        raise BootRulesError("conflicting managed block")
    # Never adopt another component's existing sources or silently duplicate
    # the host-security table in a second base-chain declaration.
    if HOST_SOURCE in base or PORTAL_SOURCE in base:
        raise BootRulesError("existing includes require owner review")
    return base.rstrip("\n") + "\n\n" + MANAGED + "\n"


def remove_managed_boot(config: str, original: str) -> str:
    """Rollback only if the live config still equals the planned candidate."""
    if render_boot(original) != config:
        raise BootRulesError("boot config drift; refuse rollback overwrite")
    return original


def render_captive() -> str:
    from ap_portal_server import FirewallManager
    rules = FirewallManager(interface="wlan0")._render_ruleset([])
    if ("table inet gway_ap_portal {" not in rules or
            'iifname "wlan0" drop' not in rules or
            'type filter hook forward priority -5;' not in rules):
        raise BootRulesError("unexpected captive policy")
    return rules + ("\n" if not rules.endswith("\n") else "")


def _read_regular(path: Path) -> str:
    if path.is_symlink():
        raise BootRulesError("symlinked source")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024:
            raise BootRulesError("unsafe or oversized source")
        raw = os.read(fd, 512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise BootRulesError("oversized source")
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise BootRulesError("invalid source encoding") from exc
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--out-boot", type=Path, required=True)
    parser.add_argument("--out-portal", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("root-owned private preparation required")
    base = _read_regular(args.base)
    for path, payload in ((args.out_boot, render_boot(base)),
                          (args.out_portal, render_captive())):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    print("private_boot_candidates_prepared=yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
