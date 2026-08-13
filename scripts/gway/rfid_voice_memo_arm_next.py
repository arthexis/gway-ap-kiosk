#!/usr/bin/env python3
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
DATA_DIR = HOME / ".local" / "share" / "rfid-voice-memo"
LOG_PATH = DATA_DIR / "arm-next.log"
DEFAULT_SCAN_FILES = (
    HOME / ".local" / "state" / "rfid-mode" / "rfid-scan.json",
    HOME / "arthexis" / ".locks" / "rfid-scan.json",
)
DEFAULT_LOCK_DIR = HOME / "arthexis" / ".locks"
LCD_EVENT_ID = 7


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str, **fields: object) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": now_iso(), "message": message, **fields}
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def normalize_card_id(value: object) -> str:
    text = "".join(str(value or "").strip().split()).upper()
    if not text:
        return ""
    return "".join(ch for ch in text if ch in "0123456789ABCDEF")


def scan_file_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env_path = os.environ.get("RFID_MODE_SCAN_FILE")
    if env_path:
        return Path(env_path).expanduser()
    config = Path(os.environ.get("RFID_MODE_SCAN_FILE_CONFIG", HOME / ".config" / "rfid-modes" / "scan-file")).expanduser()
    if config.is_file():
        try:
            configured = config.read_text(encoding="utf-8").strip()
        except OSError:
            configured = ""
        if configured:
            return Path(configured).expanduser()
    for candidate in DEFAULT_SCAN_FILES:
        if candidate.exists():
            return candidate
    return DEFAULT_SCAN_FILES[0]


def read_scan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def payload_card_id(payload: dict[str, object]) -> str:
    return normalize_card_id(payload.get("rfid") or payload.get("card_id") or payload.get("uid") or payload.get("id"))


def scan_key(path: Path, payload: dict[str, object]) -> str:
    stamp = (
        payload.get("first_presence_at")
        or payload.get("scanned_at")
        or payload.get("timestamp")
        or path.stat().st_mtime_ns
    )
    return f"{payload_card_id(payload)}|{stamp}"


def signal_lcd(lock_dir: Path) -> None:
    try:
        pid = int((lock_dir / "lcd.pid").read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return
    if pid > 1:
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            pass


def lcd_event(line1: str, line2: str, *, seconds: int = 10) -> None:
    lock_dir = Path(os.environ.get("RFID_MODE_LOCK_DIR") or DEFAULT_LOCK_DIR).expanduser()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / f"lcd-event-{LCD_EVENT_ID}.lck").write_text(
            f"{line1[:16]}\n{line2[:16]}\n{expires}\n",
            encoding="utf-8",
        )
        signal_lcd(lock_dir)
    except OSError as exc:
        log("lcd_failed", error=str(exc), line1=line1, line2=line2)


def bind_card(card_id: str, *, force: bool) -> subprocess.CompletedProcess[str]:
    command = [str(HOME / ".local" / "bin" / "rfid-voice-memo"), "bind", "--card", card_id]
    if force:
        command.append("--force")
    return subprocess.run(command, capture_output=True, text=True, check=False)


def wait_for_next_card(path: Path, timeout: float | None, *, initial_key: str, initial_card: str) -> str:
    deadline = None if timeout is None else time.monotonic() + timeout
    while deadline is None or time.monotonic() < deadline:
        try:
            payload = read_scan(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log("scan_read_failed", error=str(exc), scan_file=str(path))
            time.sleep(0.25)
            continue
        if payload.get("released"):
            time.sleep(0.25)
            continue
        card_id = payload_card_id(payload)
        if not card_id:
            time.sleep(0.25)
            continue
        current_key = scan_key(path, payload)
        if current_key != initial_key and card_id != initial_card:
            return card_id
        time.sleep(0.25)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for the next distinct RFID scan")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bind the next distinct scanned RFID card to voice memo mode.")
    parser.add_argument("--timeout", type=float, default=None, help="Seconds to wait. Omit to wait forever.")
    parser.add_argument("--scan-file")
    parser.add_argument("--force", action="store_true", help="Allow replacing existing voice memo mode files for the scanned card.")
    args = parser.parse_args(argv)

    path = scan_file_path(args.scan_file)
    try:
        initial_payload = read_scan(path)
        initial_key = scan_key(path, initial_payload)
        initial_card = payload_card_id(initial_payload)
    except Exception:
        initial_key = ""
        initial_card = ""

    timeout = args.timeout if args.timeout is not None and args.timeout > 0 else None
    lcd_event("VOICE MEMO", "SCAN NEXT", seconds=60)
    log("armed", scan_file=str(path), initial_key=initial_key, initial_card=initial_card, timeout=timeout)

    try:
        card_id = wait_for_next_card(path, timeout, initial_key=initial_key, initial_card=initial_card)
    except TimeoutError as exc:
        lcd_event("VOICE MEMO", "ARM TIMEOUT", seconds=10)
        log("timeout", error=str(exc))
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2

    lcd_event("VOICE MEMO", f"BIND {card_id[-8:]}", seconds=10)
    result = bind_card(card_id, force=args.force)
    ok = result.returncode == 0
    log(
        "bound" if ok else "bind_failed",
        card_id=card_id,
        returncode=result.returncode,
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
    )
    if not ok:
        lcd_event("VOICE MEMO", "BIND FAIL", seconds=15)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        return result.returncode or 1
    lcd_event("VOICE MEMO", "CARD BOUND", seconds=15)
    print(result.stdout, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
