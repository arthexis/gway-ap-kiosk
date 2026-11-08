#!/usr/bin/env python3
"""Read-only AP Kiosk activation evidence; fail closed on missing provenance."""
import hashlib
import json
import os
import stat
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


def trusted(path, *, directory=False):
    """Reject symlinks, untrusted owners and group/world writable evidence."""
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    return expected(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022


def process_start(pid):
    """Kernel process start ticks, used as stable identity across observations."""
    raw = (Path('/proc') / str(pid) / 'stat').read_text()
    return int(raw.rsplit(') ', 1)[1].split()[19])


def verify(marker, releases, installed, *, pid, cmdline, started_at):
    """Only immutable staged bytes matching installed executables prove provenance."""
    try:
        if not trusted(marker) or not trusted(releases, directory=True):
            return False
        revision = marker.read_text(encoding="ascii").strip()
        if not SHA.fullmatch(revision) or pid <= 0:
            return False
        release = releases / revision
        if not trusted(release, directory=True):
            return False
        revfile = release / "REVISION"
        if not trusted(revfile) or revfile.read_text(encoding="ascii").strip() != revision:
            return False
        argv = [part for part in cmdline.split(b"\0") if part]
        if len(argv) < 2 or argv[1] != b"/usr/local/bin/gway-kiosk-status":
            return False
        for relative, target in FILES.items():
            source = release / relative
            dest = installed / target.lstrip("/")
            if not trusted(source) or not trusted(dest):
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
        start_ticks = process_start(pid)
        hz = os.sysconf("SC_CLK_TCK")
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        started_at = time.time() - uptime + start_ticks / hz
        valid = verify(Path("/var/lib/gway-ap-kiosk/installed-revision"),
                       Path("/opt/gway-ap-kiosk/releases"), Path("/"),
                       pid=pid, cmdline=cmdline, started_at=started_at)
        # A recycled PID or replaced process invalidates the observation.
        valid = valid and process_start(pid) == start_ticks and (Path("/proc") / str(pid) / "cmdline").read_bytes() == cmdline
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        valid = False
    print(json.dumps({"activation_verified": valid}))
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
