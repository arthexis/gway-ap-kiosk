#!/usr/bin/env python3
"""Read-only AP Kiosk activation evidence; fail closed on missing provenance."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

FILES = {
    "bin/gway-kiosk-status": "/usr/local/bin/gway-kiosk-status",
    "bin/kiosk-mode": "/usr/local/bin/kiosk-mode",
    "sbin/gway-hdmi-kiosk-session": "/usr/local/sbin/gway-hdmi-kiosk-session",
    "sbin/gway-hdmi-kiosk-layout": "/usr/local/sbin/gway-hdmi-kiosk-layout",
    "libexec/gway-boot-stagger-until": "/usr/local/libexec/gway-boot-stagger-until",
    "sbin/kiosk-ap": "/usr/local/sbin/kiosk-ap",
}
SHA = re.compile(r"[a-f0-9]{40}\Z")


def digest(path):
    return hashlib.sha256(path.read_bytes()).digest()


def verify(marker, releases, installed, *, pid, cmdline, started_at):
    """Only immutable staged bytes matching installed executables prove provenance."""
    try:
        revision = marker.read_text(encoding="ascii").strip()
        if not SHA.fullmatch(revision) or pid <= 0:
            return False
        release = releases / revision
        if release.is_symlink() or not release.is_dir():
            return False
        revfile = release / "REVISION"
        if revfile.is_symlink() or revfile.read_text(encoding="ascii").strip() != revision:
            return False
        if b"/usr/local/bin/gway-kiosk-status" not in cmdline.split(b"\0"):
            return False
        for relative, target in FILES.items():
            source = release / relative
            dest = installed / target.lstrip("/")
            if source.is_symlink() or dest.is_symlink() or not source.is_file() or not dest.is_file():
                return False
            if digest(source) != digest(dest) or dest.stat().st_mtime > started_at:
                return False
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def main():
    try:
        result = subprocess.run(
            ["systemctl", "show", "gway-kiosk-status.service", "--property=MainPID", "--value"],
            capture_output=True, text=True, check=True, timeout=3)
        pid = int(result.stdout.strip())
        if pid <= 0:
            raise ValueError("no active process")
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        stat = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
        start_ticks = int(stat[19])
        hz = __import__("os").sysconf("SC_CLK_TCK")
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        started_at = time.time() - uptime + start_ticks / hz
        valid = verify(Path("/var/lib/gway-ap-kiosk/installed-revision"),
                       Path("/opt/gway-ap-kiosk/releases"), Path("/"),
                       pid=pid, cmdline=cmdline, started_at=started_at)
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        valid = False
    print(json.dumps({"activation_verified": valid}))
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
