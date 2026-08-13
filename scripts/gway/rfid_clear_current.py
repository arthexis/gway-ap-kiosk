#!/usr/bin/env python3
# RFID_MODE_RESET_NAVIGATION_ON_EXIT=1
"""Clear local RFID mode, arm, and wait state without stopping RFID services."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HOME = Path("/home/arthe")
CONFIG_DIR = HOME / ".config/rfid-modes"
CARDS_DIR = CONFIG_DIR / "cards"
STATE_FILE = CONFIG_DIR / "state/state.json"
LOG_PATH = HOME / ".local/share/rfid-clear/events.ndjson"
LCD_LOCK_DIR = HOME / ".local/state/lcd-lockfiles"
LCD_EVENT_ID = 9
ARM_LCD_EVENT_ID = 10

STATE_CLEAR_KEYS = {
    "current_card": None,
    "current_script": None,
    "current_mode_kind": None,
    "current_presence_id": None,
    "pid": None,
    "mode_started_at": None,
    "mode_log": None,
    "hold_card": None,
    "hold_script": None,
    "hold_presence_id": None,
    "hold_state_path": None,
    "new_hold_armed_at": None,
    "new_hold_armed_card": None,
    "new_hold_armed_presence_id": None,
    "new_hold_armed_script": None,
    "new_hold_armed_state_path": None,
}

PROCESS_MARKERS = (
    "rfid-arm-next-script",
    "rfid-voice-memo-arm-next",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def log(event: str, **fields: object) -> None:
    payload = {"timestamp": now_iso(), "event": event, **fields}
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        pass


def signal_lcd() -> None:
    try:
        pid = int((LCD_LOCK_DIR / "lcd.pid").read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return
    if pid > 1:
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            pass


def lcd_event(line1: str, line2: str, *, seconds: int = 10) -> None:
    expires = (now_utc() + timedelta(seconds=seconds)).isoformat()
    try:
        LCD_LOCK_DIR.mkdir(parents=True, exist_ok=True)
        path = LCD_LOCK_DIR / f"lcd-event-{LCD_EVENT_ID}.lck"
        path.write_text(f"{line1[:16]}\n{line2[:16]}\n{expires}\n", encoding="utf-8")
        signal_lcd()
    except OSError as exc:
        log("lcd_failed", error=str(exc), line1=line1, line2=line2)


def clear_lcd_event(event_id: int) -> None:
    try:
        (LCD_LOCK_DIR / f"lcd-event-{event_id}.lck").unlink(missing_ok=True)
        signal_lcd()
    except OSError as exc:
        log("lcd_clear_failed", event_id=event_id, error=str(exc))


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(pid: int, reason: str, timeout: float = 3.0) -> bool:
    if not pid_exists(pid):
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except OSError as exc:
        log("terminate_failed", pid=pid, reason=reason, error=str(exc))
        return False

    deadline = time.monotonic() + timeout
    while pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if pid_exists(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            log("kill_failed", pid=pid, reason=reason, error=str(exc))
            return False
    log("process_group_stopped", pid=pid, reason=reason)
    return True


def stop_state_process(state: dict[str, Any]) -> bool:
    pid = int(state.get("pid") or 0)
    script = str(state.get("current_script") or "")
    if not pid or not script:
        return False
    try:
        if pid == os.getpid() or os.getpgid(pid) == os.getpgrp():
            log("state_process_stop_skipped_self", pid=pid, script=script)
            return False
    except OSError:
        return False
    return terminate_process_group(pid, "runner_state_process")


def systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


def stop_arm_units() -> list[str]:
    result = systemctl_user("list-units", "--all", "--no-legend", "--plain")
    stopped: list[str] = []
    if result.returncode != 0:
        log("list_units_failed", stderr=(result.stderr or "").strip())
        return stopped
    for line in (result.stdout or "").splitlines():
        unit = line.split(None, 1)[0] if line.split(None, 1) else ""
        lowered = unit.lower()
        if not unit.endswith(".service"):
            continue
        if "rfid-mode-runner" in lowered:
            continue
        if "rfid" not in lowered:
            continue
        if "arm" not in lowered and "wait" not in lowered:
            continue
        systemctl_user("stop", unit)
        stopped.append(unit)
    return stopped


def proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def stop_arm_processes() -> list[int]:
    stopped: list[int] = []
    self_pid = os.getpid()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        pid = int(proc.name)
        if pid == self_pid:
            continue
        cmdline = proc_cmdline(pid)
        if not cmdline:
            continue
        joined = " ".join(cmdline)
        should_stop = any(marker in joined for marker in PROCESS_MARKERS)
        should_stop = should_stop or ("rfid-register" in joined and "--wait" in cmdline)
        if should_stop and terminate_process_group(pid, "arm_or_wait_process"):
            stopped.append(pid)
    return stopped


def reset_runner_state(card_id: str) -> None:
    state = read_json(STATE_FILE)
    state.update(STATE_CLEAR_KEYS)
    state.update(
        {
            "state_path": [],
            "state_dir": str(CARDS_DIR),
            "last_clear_card": card_id,
            "last_clear_at": now_iso(),
            "updated_at": now_iso(),
        }
    )
    write_json_atomic(STATE_FILE, state)


def main() -> int:
    card_id = "".join(str(os.environ.get("RFID_MODE_CARD_ID") or "").strip().split()).upper()
    state = read_json(STATE_FILE)
    stopped_state_process = stop_state_process(state)
    stopped_units = stop_arm_units()
    stopped_processes = stop_arm_processes()
    reset_runner_state(card_id)
    clear_lcd_event(ARM_LCD_EVENT_ID)
    lcd_event("RF QUEUE", "CLEARED", seconds=20)
    payload = {
        "ok": True,
        "card_id": card_id,
        "stopped_state_process": stopped_state_process,
        "stopped_units": stopped_units,
        "stopped_processes": stopped_processes,
        "state_file": str(STATE_FILE),
    }
    log("cleared", **payload)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
