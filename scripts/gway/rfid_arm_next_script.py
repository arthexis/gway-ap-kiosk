#!/usr/bin/env python3
"""Bind the next distinct unmapped RFID card to a local mode script."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path("/home/arthe")
CONFIG_DIR = HOME / ".config" / "rfid-modes"
CARDS_DIR = CONFIG_DIR / "cards"
STATE_FILE = CONFIG_DIR / "state" / "state.json"
SCAN_FILE = HOME / ".local" / "state" / "rfid-mode" / "rfid-scan.json"
LOG_DIR = HOME / ".local" / "share" / "rfid-arm-next"
LOG_PATH = LOG_DIR / "arm-next.log"
LCD_BASE = HOME / "arthexis"
LCD_LOCK_DIR = HOME / ".local/state/lcd-lockfiles"
LCD_EVENT_ID = 10
RUNNER_SERVICE = "rfid-mode-runner.service"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str, **fields: object) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": now_iso(), "message": message, **fields}
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def normalize_card_id(value: object) -> str:
    text = "".join(str(value or "").strip().split()).upper()
    return "".join(ch for ch in text if ch in "0123456789ABCDEF")


def read_scan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def payload_card_id(payload: dict[str, object]) -> str:
    return normalize_card_id(
        payload.get("rfid")
        or payload.get("card_id")
        or payload.get("uid")
        or payload.get("id")
    )


def scan_key(path: Path, payload: dict[str, object]) -> str:
    card_id = payload_card_id(payload)
    stamp = payload.get("scanned_at") or payload.get("timestamp") or path.stat().st_mtime_ns
    return f"{card_id}|{stamp}"


def presence_id(path: Path, payload: dict[str, object]) -> str:
    card_id = payload_card_id(payload)
    stamp = payload.get("first_presence_at") or payload.get("scanned_at") or scan_key(path, payload)
    return f"{card_id}|{stamp}"


def is_mapped(card_id: str) -> bool:
    return (
        (CARDS_DIR / card_id).exists()
        or (CARDS_DIR / f"{card_id}.sh").exists()
        or (CARDS_DIR / f"{card_id}.py").exists()
    )


def wait_for_card(path: Path, timeout: float | None) -> tuple[str, dict[str, object]]:
    try:
        initial = read_scan(path)
        initial_key = scan_key(path, initial)
        initial_card = payload_card_id(initial)
    except Exception:
        initial_key = ""
        initial_card = ""

    deadline = None if timeout is None else time.monotonic() + timeout
    log("armed", scan_file=str(path), initial_card=initial_card, timeout=timeout)
    while deadline is None or time.monotonic() < deadline:
        try:
            payload = read_scan(path)
        except Exception as exc:
            log("scan_read_failed", error=str(exc))
            time.sleep(0.25)
            continue
        if payload.get("released") or payload.get("card_present") is False:
            time.sleep(0.25)
            continue
        card_id = payload_card_id(payload)
        if not card_id:
            time.sleep(0.25)
            continue
        current_key = scan_key(path, payload)
        if current_key == initial_key or card_id == initial_card:
            time.sleep(0.25)
            continue
        if is_mapped(card_id):
            log("mapped_card_ignored", card_id=card_id)
            time.sleep(0.25)
            continue
        return card_id, payload

    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for the next unmapped RFID scan")


def systemctl_user(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


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


def write_lcd_lockfile(subject: str, body: str) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    try:
        LCD_LOCK_DIR.mkdir(parents=True, exist_ok=True)
        path = LCD_LOCK_DIR / f"lcd-event-{LCD_EVENT_ID}.lck"
        path.write_text(f"{subject[:16]}\n{body[:16]}\n{expires}\n", encoding="utf-8")
        signal_lcd()
    except OSError as exc:
        log("lcd_lockfile_failed", error=str(exc), subject=subject, body=body)


def write_lcd(subject: str, body: str) -> None:
    write_lcd_lockfile(subject, body)
    manage_py = LCD_BASE / "manage.py"
    python = LCD_BASE / ".venv" / "bin" / "python"
    if not manage_py.exists():
        log("lcd_skipped", reason="manage.py missing", subject=subject, body=body)
        return
    command = [
        str(python if python.exists() else sys.executable),
        str(manage_py),
        "lcd",
        "write",
        "--subject",
        subject[:16],
        "--body",
        body[:16],
        "--sticky",
    ]
    try:
        result = subprocess.run(command, cwd=LCD_BASE, capture_output=True, text=True, check=False, timeout=5)
    except subprocess.TimeoutExpired:
        log("lcd_timeout", subject=subject, body=body)
        return
    if result.returncode != 0:
        log("lcd_failed", returncode=result.returncode, stderr=result.stderr.strip())


def script_requests_reset(script: Path) -> bool:
    try:
        text = script.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "RFID_MODE_RESET_NAVIGATION_ON_EXIT" in text


def registered_script_path(card_id: str, script: Path) -> Path:
    suffix = script.suffix.lower() if script.suffix.lower() in {".sh", ".py"} else ".sh"
    return CARDS_DIR / f"{card_id}{suffix}"


def preserve_reset_marker(card_id: str, script: Path) -> None:
    if not script_requests_reset(script):
        return
    target = registered_script_path(card_id, script)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        log("reset_marker_read_failed", card_id=card_id, target=str(target), error=str(exc))
        return
    if "RFID_MODE_RESET_NAVIGATION_ON_EXIT" in text:
        return
    lines = text.splitlines()
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    lines.insert(insert_at, "# RFID_MODE_RESET_NAVIGATION_ON_EXIT=1")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        log("reset_marker_write_failed", card_id=card_id, target=str(target), error=str(exc))


def register_card(card_id: str, script: Path, label_lines: tuple[str, str], force: bool) -> None:
    register = HOME / ".local" / "bin" / "rfid-register"
    base_command = [str(register), "--card", card_id, "--dir"]
    script_command = [str(register), "--path", card_id, "--label", " ".join(part for part in label_lines if part)]
    if script.suffix.lower() in {".sh", ".py"}:
        script_command.extend(["--script", str(script)])
    else:
        script_command.extend(["--command", str(script)])
    if force:
        base_command.append("--force")
        script_command.append("--force")

    for command in (base_command, script_command):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "rfid-register failed").strip())
    preserve_reset_marker(card_id, script)

    label_path = CARDS_DIR / card_id / "label.txt"
    label_path.write_text("\n".join(part for part in label_lines if part) + "\n", encoding="utf-8")


def seed_runner_state(card_id: str, payload: dict[str, object], script: Path) -> None:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    now = now_iso()
    state.update(
        {
            "state_path": [],
            "state_dir": str(CARDS_DIR),
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
            "last_scan_key": scan_key(SCAN_FILE, payload),
            "last_scan_card": card_id,
            "last_scan_seen_at": now,
            "last_one_shot_mode_card": card_id,
            "last_one_shot_mode_script": str(CARDS_DIR / f"{card_id}{script.suffix if script.suffix in {'.sh', '.py'} else '.sh'}"),
            "last_one_shot_mode_presence_id": presence_id(SCAN_FILE, payload),
            "last_one_shot_mode_reset_at": now,
            "updated_at": now,
        }
    )
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--label-line1", required=True)
    parser.add_argument("--label-line2", default="")
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if not args.script.is_file():
        print(f"script not found: {args.script}", file=sys.stderr)
        return 2

    timeout = args.timeout if args.timeout is not None and args.timeout > 0 else None
    display_name = args.display_name[:16]
    write_lcd("ARMED:", display_name)

    try:
        card_id, payload = wait_for_card(SCAN_FILE, timeout)
        log("binding", card_id=card_id, script=str(args.script))
        systemctl_user("stop", RUNNER_SERVICE)
        try:
            register_card(
                card_id,
                args.script,
                (args.label_line1[:16], args.label_line2[:16]),
                args.force,
            )
            seed_runner_state(card_id, payload, args.script)
        finally:
            systemctl_user("start", RUNNER_SERVICE)
        write_lcd(display_name, "READY")
        log("bound", card_id=card_id, script=str(args.script))
        print(json.dumps({"ok": True, "card_id": card_id, "script": str(args.script)}, sort_keys=True))
        return 0
    except TimeoutError as exc:
        write_lcd("ARM TIMEOUT", display_name)
        log("timeout", error=str(exc))
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    except Exception as exc:
        systemctl_user("start", RUNNER_SERVICE)
        write_lcd("ARM FAIL", display_name)
        log("failed", error=str(exc))
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
