#!/usr/bin/env python3
"""Run local device modes selected by RFID transition paths.

The runner watches the Arthexis passive RFID scan lockfile and interprets
~/.config/rfid-modes/cards as a small state machine:

* top-level entries must be directories
* scanning a card with a matching child directory moves into that directory
* if a matching .sh/.py sidecar script also exists, it runs while entering the directory
* a hold.sh/hold.py script in the current directory runs after the current card is fully read and confirmed still present
* a new.sh/new.py script in the current directory runs once for an unrecognized next card
* a new.sh/new.py script can declare RFID_MODE_NEW_BEFORE_RESOLVE=1 to inspect all next-card scans before normal resolution
* a new.sh/new.py script can declare RFID_MODE_NEW_REQUIRES_HOLD=1 to require its own card to be held before creation
* a new.sh/new.py script can exit 75 to let normal chain resolution continue
* a new.sh/new.py script can declare RFID_MODE_CHAIN_POLICY=terminate to break chains
* scanning a card with a matching shell or Python script at the current level executes it
* scanning an unknown card resets navigation to the base directory
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "rfid-modes"
LOCAL_SCAN_FILE = Path.home() / ".local" / "state" / "rfid-mode" / "rfid-scan.json"
LEGACY_ARTHEXIS_SCAN_FILE = Path("/home/arthe/arthexis/.locks/rfid-scan.json")
LOCAL_LCD_LOCK_DIR = Path.home() / ".local" / "state" / "lcd-lockfiles"
LEGACY_LCD_LOCK_DIR = Path("/home/arthe/arthexis/.locks")
SCAN_FILE_CONFIG_NAME = "scan-file"
LOCAL_HARDWARE_PYTHON = Path.home() / ".local" / "share" / "rfid-mode" / ".venv" / "bin" / "python"
LEGACY_ARTHEXIS_HARDWARE_PYTHON = Path("/home/arthe/arthexis/.venv/bin/python")
CARD_ID_RE = re.compile(r"[^A-Z0-9_.-]+")
CHAIN_POLICY_RE = re.compile(r"^\s*#\s*RFID_MODE_CHAIN_POLICY\s*=\s*([A-Za-z0-9_.-]+)\s*$", re.MULTILINE)
NEW_BEFORE_RESOLVE_RE = re.compile(
    r"^\s*#\s*RFID_MODE_NEW_BEFORE_RESOLVE\s*=\s*(?:1|true|yes)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
NEW_REQUIRES_HOLD_RE = re.compile(
    r"^\s*#\s*RFID_MODE_NEW_REQUIRES_HOLD\s*=\s*(?:1|true|yes)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
RESET_NAVIGATION_ON_EXIT_RE = re.compile(
    r"^\s*#\s*RFID_MODE_RESET_NAVIGATION_ON_EXIT\s*=\s*(?:1|true|yes)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
SCRIPT_SUFFIXES = ("", ".sh", ".py")
HOLD_SCRIPT_NAMES = ("hold.sh", "hold.py")
NEW_SCRIPT_NAMES = ("new.sh", "new.py")
LABEL_FILE_NAME = "label.txt"
EXPANSION_DIR_NAME = ".cleave"
RFID_TRAIT_ENV_RE = re.compile(r"[^A-Z0-9]+")
DEFAULT_HOLD_CONFIRM_SECONDS = 0.75
DEFAULT_HOLD_FRESH_SECONDS = 5.0
DEFAULT_ONE_SHOT_TIMEOUT = 30.0
DEFAULT_LABEL_EVENT_SECONDS = 30
NEW_SCRIPT_CONTINUE_EXIT = 75
RFID_SERVICE_UNIT = os.environ.get("RFID_MODE_RFID_SERVICE_UNIT", "")
LCD_SERVICE_UNIT = os.environ.get("RFID_MODE_LCD_SERVICE_UNIT", "")
SOUND_HOOK = Path(os.path.expandvars(os.path.expanduser(os.environ.get("RFID_MODE_SOUND_HOOK", "/home/arthe/.local/bin/codex-sound-hook"))))
RUN_SOUND = os.environ.get("RFID_MODE_SOUND_RUN", "attention")
COPY_COMPLETE_SOUND = os.environ.get("RFID_MODE_SOUND_COPY_COMPLETE", "ok")
FACTORY_KEY_BYTES = [0xFF] * 6
LCD_LINE_BYTES = 16
LCD_EVENT_LINE_BYTES = 64


def expand_path(raw: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw))))


def configured_scan_file(config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    env_value = os.environ.get("RFID_MODE_SCAN_FILE")
    if env_value:
        return expand_path(env_value)

    config_path = expand_path(
        os.environ.get("RFID_MODE_SCAN_FILE_CONFIG", str(config_dir / SCAN_FILE_CONFIG_NAME))
    )
    try:
        configured = config_path.read_text(encoding="utf-8").strip()
    except OSError:
        configured = ""
    if configured:
        return expand_path(configured)

    for candidate in (LOCAL_SCAN_FILE, LEGACY_ARTHEXIS_SCAN_FILE):
        if candidate.exists():
            return candidate
    return LOCAL_SCAN_FILE


def configured_lcd_lock_dir() -> Path:
    for env_name in ("RFID_MODE_LCD_LOCK_DIR", "LCD_LOCK_DIR"):
        env_value = os.environ.get(env_name)
        if env_value:
            return expand_path(env_value)

    lock_dirs = os.environ.get("LCD_LOCK_DIRS", "")
    for raw in lock_dirs.split(":"):
        if raw.strip():
            return expand_path(raw.strip())

    for candidate in (LOCAL_LCD_LOCK_DIR, LEGACY_LCD_LOCK_DIR):
        if candidate.exists():
            return candidate
    return LOCAL_LCD_LOCK_DIR


def configured_hardware_python() -> Path:
    env_value = os.environ.get("RFID_MODE_HARDWARE_PYTHON")
    if env_value:
        return expand_path(env_value)
    for candidate in (LOCAL_HARDWARE_PYTHON, LEGACY_ARTHEXIS_HARDWARE_PYTHON):
        if candidate.exists():
            return candidate
    return Path(sys.executable)


DEFAULT_SCAN_FILE = configured_scan_file()
DEFAULT_LCD_LOCK_DIR = configured_lcd_lock_dir()
HARDWARE_PYTHON = configured_hardware_python()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_card_id(raw: Any) -> str:
    value = str(raw or "").strip().upper()
    value = value.replace(":", "").replace(" ", "")
    return CARD_ID_RE.sub("_", value)


def normalize_trait_env_name(raw: Any) -> str:
    value = str(raw or "").strip().upper()
    value = RFID_TRAIT_ENV_RE.sub("_", value).strip("_")
    return f"RFID_{value}" if value else "RFID_TRAIT"


def clean_label_text(raw: Any) -> str:
    text = "" if raw is None else str(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)


def label_lines(raw: Any, *, limit: int) -> tuple[str, str]:
    text = clean_label_text(raw)
    parts = text.split("\n", 1)
    line_1 = parts[0].encode("ascii", errors="ignore").decode("ascii")[:limit]
    line_2 = ""
    if len(parts) > 1:
        line_2 = parts[1].split("\n", 1)[0].encode("ascii", errors="ignore").decode("ascii")[:limit]
    return line_1, line_2


def normalize_card_label(raw: Any) -> str:
    line_1, line_2 = label_lines(raw, limit=LCD_LINE_BYTES)
    return "\n".join((line_1, line_2)).rstrip("\n")


def normalize_lcd_event_label(raw: Any) -> tuple[str, str]:
    line_1, line_2 = label_lines(raw, limit=LCD_EVENT_LINE_BYTES)
    return line_1, line_2


def encode_card_label(raw: Any) -> dict[int, list[int]]:
    line_1, line_2 = label_lines(raw, limit=LCD_LINE_BYTES)
    return {
        1: list(line_1.encode("ascii", errors="ignore").ljust(LCD_LINE_BYTES, b"\x00")),
        2: list(line_2.encode("ascii", errors="ignore").ljust(LCD_LINE_BYTES, b"\x00")),
    }


def trait_environment(payload: dict[str, Any]) -> dict[str, str]:
    raw_traits = payload.get("traits")
    if not isinstance(raw_traits, dict):
        return {}
    env: dict[str, str] = {}
    for key, record in raw_traits.items():
        if isinstance(record, dict):
            value = record.get("value", "")
        else:
            value = record
        env[normalize_trait_env_name(key)] = str(value or "")
    return env


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
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


def proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


@dataclass
class Scan:
    card_id: str
    scanned_at: str
    key: str
    payload: dict[str, Any]


@dataclass
class Resolution:
    action: str
    path: Path | None = None
    next_path: Path | None = None
    reason: str = ""


class ModeRunner:
    def __init__(
        self,
        scan_file: Path,
        config_dir: Path,
        poll_seconds: float,
        stop_timeout: float,
        hold_confirm_seconds: float = DEFAULT_HOLD_CONFIRM_SECONDS,
        hold_fresh_seconds: float = DEFAULT_HOLD_FRESH_SECONDS,
        one_shot_timeout: float = DEFAULT_ONE_SHOT_TIMEOUT,
        dry_run: bool = False,
    ) -> None:
        self.scan_file = scan_file
        self.config_dir = config_dir
        self.cards_dir = config_dir / "cards"
        self.state_dir = config_dir / "state"
        self.log_dir = config_dir / "logs"
        self.mode_log_dir = self.log_dir / "modes"
        self.state_file = self.state_dir / "state.json"
        self.event_log = self.log_dir / "events.ndjson"
        self.poll_seconds = poll_seconds
        self.stop_timeout = stop_timeout
        self.hold_confirm_seconds = hold_confirm_seconds
        self.hold_fresh_seconds = hold_fresh_seconds
        self.one_shot_timeout = one_shot_timeout
        self.dry_run = dry_run
        self.current_proc: subprocess.Popen[str] | None = None
        self.state: dict[str, Any] = {}
        self.running = True

    def setup(self) -> None:
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.mode_log_dir.mkdir(parents=True, exist_ok=True)
        self.state = read_json(self.state_file) or {}
        if "state_path" not in self.state:
            self.write_state(state_path=[], state_dir=str(self.cards_dir))
        self._clear_dead_stored_process()

    def log(self, event: str, **fields: Any) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": now_iso(), "event": event, **fields}
        with self.event_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def write_state(self, **updates: Any) -> None:
        self.state.update(updates)
        self.state["updated_at"] = now_iso()
        atomic_write_json(self.state_file, self.state)

    def emit_sound(self, event: str, sound: str, detail: str = "") -> None:
        if not sound or not SOUND_HOOK.exists():
            return
        try:
            subprocess.Popen(
                [str(SOUND_HOOK), event, sound],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.log("sound_emit_failed", event_name=event, sound=sound, detail=detail, error=str(exc))

    def state_path_parts(self) -> list[str]:
        raw = self.state.get("state_path") or []
        if not isinstance(raw, list):
            return []
        return [normalize_card_id(part) for part in raw if normalize_card_id(part)]

    def path_from_parts(self, parts: list[str]) -> Path:
        path = self.cards_dir
        for part in parts:
            path = path / part
        return path

    def current_level(self) -> tuple[Path, list[str]]:
        parts = self.state_path_parts()
        path = self.path_from_parts(parts)
        try:
            path.resolve().relative_to(self.cards_dir.resolve())
        except ValueError:
            self.reset_navigation("state_path_outside_cards")
            return self.cards_dir, []
        if not path.is_dir():
            self.reset_navigation("state_path_missing")
            return self.cards_dir, []
        return path, parts

    def reset_navigation(self, reason: str, scan: Scan | None = None) -> None:
        self.log(
            "navigation_reset",
            reason=reason,
            card_id=scan.card_id if scan else None,
            scan_key=scan.key if scan else None,
        )
        updates: dict[str, Any] = {
            "state_path": [],
            "state_dir": str(self.cards_dir),
            "new_hold_armed_card": None,
            "new_hold_armed_presence_id": None,
            "new_hold_armed_state_path": None,
            "new_hold_armed_script": None,
            "new_hold_armed_at": None,
        }
        if scan is not None:
            updates.update(
                last_scan_key=scan.key,
                last_scan_card=scan.card_id,
                last_scan_seen_at=now_iso(),
            )
        self.write_state(**updates)

    def read_scan(self) -> Scan | None:
        payload = read_json(self.scan_file)
        if not payload:
            return None
        card_id = normalize_card_id(
            payload.get("rfid")
            or payload.get("card_id")
            or payload.get("uid")
            or payload.get("id")
        )
        if not card_id:
            return None
        scanned_at = str(payload.get("scanned_at") or payload.get("timestamp") or "")
        key = f"{card_id}|{scanned_at or self.scan_file.stat().st_mtime_ns}"
        return Scan(card_id=card_id, scanned_at=scanned_at, key=key, payload=payload)

    def script_candidates(self, level: Path, card_id: str) -> list[Path]:
        candidates = [level / f"{card_id}{suffix}" for suffix in SCRIPT_SUFFIXES]
        return [candidate for candidate in candidates if candidate.is_file()]

    def hold_script_candidates(self, level: Path) -> list[Path]:
        return [level / name for name in HOLD_SCRIPT_NAMES if (level / name).is_file()]

    def new_script_candidates(self, level: Path) -> list[Path]:
        return [level / name for name in NEW_SCRIPT_NAMES if (level / name).is_file()]

    def script_command(self, script: Path) -> list[str]:
        if script.suffix.lower() == ".py":
            return [sys.executable, str(script)]
        return [str(script)]

    def script_is_runnable(self, script: Path) -> bool:
        return script.suffix.lower() == ".py" or os.access(script, os.X_OK)

    def script_resets_navigation_on_exit(self, script: Path) -> bool:
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return bool(RESET_NAVIGATION_ON_EXIT_RE.search(text))

    def one_shot_mode_presence_already_handled(
        self, scan: Scan, level: Path, path_parts: list[str]
    ) -> bool:
        if path_parts:
            return False
        child_dir = level / scan.card_id
        scripts = self.script_candidates(level, scan.card_id)
        if not child_dir.is_dir() or len(scripts) != 1:
            return False
        script = scripts[0]
        if not self.script_resets_navigation_on_exit(script):
            return False
        return (
            self.state.get("last_one_shot_mode_presence_id") == self.scan_presence_id(scan)
            and self.state.get("last_one_shot_mode_card") == scan.card_id
            and self.state.get("last_one_shot_mode_script") == str(script)
        )

    def label_file(self, level: Path) -> Path:
        return level / LABEL_FILE_NAME

    def read_label_file(self, level: Path) -> str | None:
        path = self.label_file(level)
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.log("label_file_read_failed", path=str(path), error=str(exc))
            return None
        text = clean_label_text(raw).strip("\n")
        return text if text.strip() else None

    def lcd_lock_dir(self) -> Path:
        return DEFAULT_LCD_LOCK_DIR

    def lcd_service_pid(self) -> int | None:
        lock_dir = self.lcd_lock_dir()
        pid_file = lock_dir / "lcd.pid"
        if pid_file.exists():
            try:
                value = int(pid_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                value = 0
            if value > 1:
                return value
        return None

    def signal_lcd_service(self) -> None:
        pid = self.lcd_service_pid()
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError as exc:
            self.log("lcd_signal_failed", pid=pid, error=str(exc))

    def send_lcd_label_event(
        self,
        label: str,
        *,
        scan: Scan,
        path_parts: list[str],
        reason: str,
    ) -> None:
        line_1, line_2 = normalize_lcd_event_label(label)
        if not line_1 and not line_2:
            return
        presence_id = self.scan_presence_id(scan)
        signature = f"{'/'.join(path_parts)}|{line_1}\n{line_2}|{presence_id}|{reason}"
        if self.state.get("last_label_event_signature") == signature:
            return
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_LABEL_EVENT_SECONDS)
        payload = f"{line_1}\n{line_2}\n{expires_at.isoformat()}\n"
        target = self.lcd_lock_dir() / "lcd-event-0.lck"
        self.log(
            "label_lcd_event",
            card_id=scan.card_id,
            state_path=path_parts,
            label=line_1 + ("\n" + line_2 if line_2 else ""),
            reason=reason,
            dry_run=self.dry_run,
        )
        if not self.dry_run:
            try:
                target.write_text(payload, encoding="utf-8")
                self.signal_lcd_service()
            except OSError as exc:
                self.log("label_lcd_event_failed", path=str(target), error=str(exc))
                return
        self.write_state(
            last_label_event_signature=signature,
            last_label_event_path="/".join(path_parts),
            last_label_event_at=now_iso(),
        )

    def systemctl(self, action: str, unit: str) -> bool:
        if not unit:
            return False
        command = ["systemctl", action, unit]
        if os.geteuid() != 0:
            command = ["sudo", "-n", *command]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            self.log("systemctl_failed", action=action, unit=unit, error=str(exc))
            return False
        if result.returncode != 0:
            self.log(
                "systemctl_failed",
                action=action,
                unit=unit,
                returncode=result.returncode,
                stderr=(result.stderr or "").strip(),
            )
            return False
        return True

    def service_is_active(self, unit: str) -> bool:
        if not unit:
            return False
        try:
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        return result.returncode == 0 and (result.stdout or "").strip() == "active"

    def _read_mfrc_block(self, mfrc: Any, block: int, uid: list[int]) -> list[int] | None:
        auth = mfrc.MFRC522_Auth(mfrc.PICC_AUTHENT1A, block, FACTORY_KEY_BYTES, uid)
        if auth != mfrc.MI_OK:
            return None
        result = mfrc.MFRC522_Read(block)
        if isinstance(result, tuple):
            status, data = result
        else:
            status, data = mfrc.MI_OK, result
        if status != mfrc.MI_OK or data is None:
            return None
        return list(data)[:LCD_LINE_BYTES]

    def _write_mfrc_block(self, mfrc: Any, block: int, uid: list[int], data: list[int]) -> bool:
        auth = mfrc.MFRC522_Auth(mfrc.PICC_AUTHENT1A, block, FACTORY_KEY_BYTES, uid)
        if auth != mfrc.MI_OK:
            return False
        write_status = mfrc.MFRC522_Write(block, list(data)[:LCD_LINE_BYTES])
        if isinstance(write_status, tuple):
            write_status = write_status[0]
        if write_status == mfrc.MI_OK:
            return True
        if write_status is None:
            readback = self._read_mfrc_block(mfrc, block, uid)
            return readback == list(data)[:LCD_LINE_BYTES]
        return False

    def write_presented_card_label_subprocess(self, scan: Scan, label: str, *, timeout: float) -> dict[str, Any]:
        python = HARDWARE_PYTHON if HARDWARE_PYTHON.exists() else Path(sys.executable)
        helper = r'''
import json
import os
import time

card_id = os.environ["RFID_MODE_WRITE_CARD_ID"]
label = os.environ["RFID_MODE_WRITE_LABEL"]
timeout = float(os.environ.get("RFID_MODE_WRITE_TIMEOUT", "10"))
line_bytes = 16
factory = [0xFF] * 6

def lines(raw):
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = text.split("\n", 1)
    one = parts[0].encode("ascii", errors="ignore").decode("ascii")[:line_bytes]
    two = ""
    if len(parts) > 1:
        two = parts[1].split("\n", 1)[0].encode("ascii", errors="ignore").decode("ascii")[:line_bytes]
    return one, two

def payloads(raw):
    one, two = lines(raw)
    return {
        1: list(one.encode("ascii").ljust(line_bytes, b"\x00")),
        2: list(two.encode("ascii").ljust(line_bytes, b"\x00")),
    }

def read_block(mfrc, block, uid):
    auth = mfrc.MFRC522_Auth(mfrc.PICC_AUTHENT1A, block, factory, uid)
    if auth != mfrc.MI_OK:
        return None
    result = mfrc.MFRC522_Read(block)
    if isinstance(result, tuple):
        status, data = result
    else:
        status, data = mfrc.MI_OK, result
    if status != mfrc.MI_OK or data is None:
        return None
    return list(data)[:line_bytes]

def write_block(mfrc, block, uid, data):
    auth = mfrc.MFRC522_Auth(mfrc.PICC_AUTHENT1A, block, factory, uid)
    if auth != mfrc.MI_OK:
        return False
    status = mfrc.MFRC522_Write(block, list(data)[:line_bytes])
    if isinstance(status, tuple):
        status = status[0]
    if status == mfrc.MI_OK:
        return True
    if status is None:
        return read_block(mfrc, block, uid) == list(data)[:line_bytes]
    return False

try:
    try:
        import RPi.GPIO as GPIO
        GPIO.setwarnings(False)
    except Exception:
        pass
    from mfrc522 import MFRC522

    mfrc = MFRC522(bus=0, device=0, pin_mode=11, pin_rst=25)
    deadline = time.monotonic() + timeout
    wanted = payloads(label)
    try:
        while time.monotonic() < deadline:
            status, _tag_type = mfrc.MFRC522_Request(mfrc.PICC_REQIDL)
            if status != mfrc.MI_OK:
                time.sleep(0.05)
                continue
            status, uid = mfrc.MFRC522_Anticoll()
            if status != mfrc.MI_OK or not uid:
                time.sleep(0.05)
                continue
            try:
                mfrc.MFRC522_SelectTag(uid)
            except Exception:
                pass
            found = "".join(f"{int(value) & 0xFF:02X}" for value in uid)
            if found != card_id:
                print(json.dumps({"ok": False, "error": "card_changed", "rfid": found}))
                raise SystemExit(2)
            errors = []
            readback = {}
            for block, data in wanted.items():
                if not write_block(mfrc, block, uid, data):
                    errors.append(f"block {block}")
                    continue
                value = read_block(mfrc, block, uid)
                if value != data:
                    errors.append(f"readback {block}")
                elif value is not None:
                    readback[str(block)] = value
            if errors:
                print(json.dumps({"ok": False, "error": "write_failed", "errors": errors, "rfid": found}))
                raise SystemExit(1)
            print(json.dumps({"ok": True, "rfid": found, "readback": readback}))
            raise SystemExit(0)
        print(json.dumps({"ok": False, "error": "card_not_detected", "rfid": card_id}))
        raise SystemExit(1)
    finally:
        try:
            mfrc.MFRC522_StopCrypto1()
        except Exception:
            pass
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc), "rfid": card_id}))
    raise SystemExit(1)
'''
        env = os.environ.copy()
        env.update(
            {
                "RFID_MODE_WRITE_CARD_ID": scan.card_id,
                "RFID_MODE_WRITE_LABEL": label,
                "RFID_MODE_WRITE_TIMEOUT": str(timeout),
            }
        )
        try:
            result = subprocess.run(
                [str(python), "-c", helper],
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc), "rfid": scan.card_id}
        for line in reversed((result.stdout or "").splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload.setdefault("returncode", result.returncode)
            return payload
        return {
            "ok": False,
            "error": (result.stderr or result.stdout or "label writer produced no JSON").strip(),
            "returncode": result.returncode,
            "rfid": scan.card_id,
        }

    def write_presented_card_label(self, scan: Scan, label: str, *, timeout: float = 10.0) -> dict[str, Any]:
        if self.dry_run:
            return {"ok": True, "dry_run": True, "rfid": scan.card_id}

        was_active = self.service_is_active(RFID_SERVICE_UNIT)
        if was_active:
            self.systemctl("stop", RFID_SERVICE_UNIT)
            time.sleep(0.5)
        try:
            if HARDWARE_PYTHON.exists() and HARDWARE_PYTHON != Path(sys.executable):
                return self.write_presented_card_label_subprocess(scan, label, timeout=timeout)
            try:
                import RPi.GPIO as GPIO  # type: ignore

                GPIO.setwarnings(False)
            except Exception:
                pass
            from mfrc522 import MFRC522  # type: ignore

            mfrc = MFRC522(bus=0, device=0, pin_mode=11, pin_rst=25)
            payloads = encode_card_label(label)
            deadline = time.monotonic() + timeout
            try:
                while time.monotonic() < deadline:
                    status, _tag_type = mfrc.MFRC522_Request(mfrc.PICC_REQIDL)
                    if status != mfrc.MI_OK:
                        time.sleep(0.05)
                        continue
                    status, uid = mfrc.MFRC522_Anticoll()
                    if status != mfrc.MI_OK or not uid:
                        time.sleep(0.05)
                        continue
                    try:
                        mfrc.MFRC522_SelectTag(uid)
                    except Exception:
                        pass
                    card_id = "".join(f"{int(value) & 0xFF:02X}" for value in uid)
                    if normalize_card_id(card_id) != scan.card_id:
                        return {"ok": False, "error": "card_changed", "rfid": card_id}
                    errors: list[str] = []
                    readback: dict[str, list[int]] = {}
                    for block, data in payloads.items():
                        if not self._write_mfrc_block(mfrc, block, uid, data):
                            errors.append(f"block {block}")
                            continue
                        read_data = self._read_mfrc_block(mfrc, block, uid)
                        if read_data != data:
                            errors.append(f"readback {block}")
                        elif read_data is not None:
                            readback[str(block)] = read_data
                    if errors:
                        return {"ok": False, "error": "write_failed", "errors": errors, "rfid": card_id}
                    return {"ok": True, "rfid": card_id, "readback": readback}
                return {"ok": False, "error": "card_not_detected", "rfid": scan.card_id}
            finally:
                try:
                    mfrc.MFRC522_StopCrypto1()
                except Exception:
                    pass
        except Exception as exc:
            return {"ok": False, "error": str(exc), "rfid": scan.card_id}
        finally:
            if was_active:
                self.systemctl("start", RFID_SERVICE_UNIT)

    def resolve_transition(self, level: Path, card_id: str) -> Resolution:
        child_dir = level / card_id
        scripts = self.script_candidates(level, card_id)

        if len(scripts) > 1:
            return Resolution("reset", reason="multiple_scripts_for_card")
        if level == self.cards_dir and scripts and not child_dir.is_dir():
            return Resolution("reset", reason="top_level_script_not_allowed")
        if level.name == card_id and scripts:
            return Resolution("reset", reason="directory_contains_same_rfid_script")
        if child_dir.is_dir() and scripts:
            return Resolution("execute_and_navigate", path=scripts[0], next_path=child_dir)
        if child_dir.is_dir():
            return Resolution("navigate", path=child_dir)
        if scripts:
            return Resolution("execute", path=scripts[0])
        return Resolution("reset", reason="no_matching_transition")

    def process_alive_matches_state(self) -> bool:
        pid = int(self.state.get("pid") or 0)
        script = str(self.state.get("current_script") or "")
        if not pid or not script or not pid_exists(pid):
            return False
        cmdline = proc_cmdline(pid)
        return any(part == script or script in part for part in cmdline)

    def _clear_dead_stored_process(self) -> None:
        if self.state.get("pid") and not self.process_alive_matches_state():
            self.log(
                "state_cleared_stale_process",
                pid=self.state.get("pid"),
                current_card=self.state.get("current_card"),
                current_script=self.state.get("current_script"),
            )
            self.write_state(
                current_card=None,
                current_script=None,
                current_mode_kind=None,
                current_presence_id=None,
                pid=None,
                mode_started_at=None,
                mode_log=None,
                hold_card=None,
                hold_script=None,
                hold_presence_id=None,
            )

    def current_is_alive(self) -> bool:
        if self.current_proc is not None:
            return self.current_proc.poll() is None
        return self.process_alive_matches_state()

    def reap_current_if_exited(self) -> None:
        if self.current_proc is None:
            return
        status = self.current_proc.poll()
        if status is None:
            return
        self.log(
            "mode_exited",
            card_id=self.state.get("current_card"),
            script=self.state.get("current_script"),
            pid=self.current_proc.pid,
            returncode=status,
        )
        current_card = self.state.get("current_card")
        current_script = str(self.state.get("current_script") or "")
        current_presence_id = str(self.state.get("current_presence_id") or "")
        reset_navigation = (
            bool(current_script)
            and self.script_resets_navigation_on_exit(Path(current_script))
        )
        self.current_proc = None
        updates: dict[str, Any] = dict(
            current_card=None,
            current_script=None,
            current_mode_kind=None,
            current_presence_id=None,
            pid=None,
            mode_started_at=None,
            mode_log=None,
            hold_card=None,
            hold_script=None,
            hold_presence_id=None,
        )
        if reset_navigation:
            updates.update(
                state_path=[],
                state_dir=str(self.cards_dir),
                last_one_shot_mode_card=current_card,
                last_one_shot_mode_script=current_script,
                last_one_shot_mode_presence_id=current_presence_id,
                last_one_shot_mode_reset_at=now_iso(),
            )
            self.log(
                "mode_exit_navigation_reset",
                card_id=current_card,
                script=current_script,
                presence_id=current_presence_id,
            )
        self.write_state(**updates)

    def stop_current(self, reason: str, next_card: str | None = None) -> None:
        pid = int(self.state.get("pid") or 0)
        script = str(self.state.get("current_script") or "")
        if not pid:
            if self.state.get("current_card") or self.state.get("current_script"):
                self.write_state(
                    current_card=None,
                    current_script=None,
                    current_mode_kind=None,
                    current_presence_id=None,
                    pid=None,
                    mode_started_at=None,
                    mode_log=None,
                    hold_card=None,
                    hold_script=None,
                    hold_presence_id=None,
                )
            return
        if not self.process_alive_matches_state():
            self.log("mode_stop_skipped_stale_pid", pid=pid, script=script, reason=reason)
            self.write_state(
                current_card=None,
                current_script=None,
                current_mode_kind=None,
                current_presence_id=None,
                pid=None,
                mode_started_at=None,
                mode_log=None,
                hold_card=None,
                hold_script=None,
                hold_presence_id=None,
            )
            return

        self.log(
            "mode_stop",
            card_id=self.state.get("current_card"),
            script=script,
            pid=pid,
            reason=reason,
            next_card=next_card,
            dry_run=self.dry_run,
        )
        if not self.dry_run:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + self.stop_timeout
            while pid_exists(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if pid_exists(pid):
                self.log("mode_kill", pid=pid, script=script, reason=reason)
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.current_proc = None
        self.write_state(
            current_card=None,
            current_script=None,
            current_mode_kind=None,
            current_presence_id=None,
            pid=None,
            mode_started_at=None,
            mode_log=None,
            hold_card=None,
            hold_script=None,
            hold_presence_id=None,
        )

    def start_mode(
        self,
        scan: Scan,
        script: Path,
        level: Path,
        path_parts: list[str],
        next_level: Path | None = None,
        next_path_parts: list[str] | None = None,
        mode_kind: str = "mode",
        extra_env: dict[str, str] | None = None,
        extra_state: dict[str, Any] | None = None,
    ) -> None:
        state_level = next_level or level
        state_path_parts = next_path_parts or path_parts
        mode_path_parts = next_path_parts or (path_parts + [scan.card_id])
        if not self.script_is_runnable(script):
            self.log("script_not_executable", card_id=scan.card_id, script=str(script))
            self.write_state(
                last_scan_key=scan.key,
                last_scan_card=scan.card_id,
                last_scan_seen_at=now_iso(),
            )
            return

        current_script = str(self.state.get("current_script") or "")
        if current_script == str(script) and self.current_is_alive():
            self.log(
                "scan_current_mode_unchanged",
                card_id=scan.card_id,
                script=str(script),
                scan_key=scan.key,
                state_path=path_parts,
            )
            updates: dict[str, Any] = {
                "last_scan_key": scan.key,
                "last_scan_card": scan.card_id,
                "last_scan_seen_at": now_iso(),
                "state_path": state_path_parts,
                "state_dir": str(state_level),
            }
            if extra_state:
                updates.update(extra_state)
            self.write_state(**updates)
            return

        if self.current_is_alive():
            self.stop_current("mode_switch", next_card=scan.card_id)

        started_at = now_iso()
        safe_time = re.sub(r"[^0-9A-Za-z_.-]", "-", started_at)
        mode_log = self.mode_log_dir / f"{safe_time}_{'_'.join(mode_path_parts)}.log"
        env = os.environ.copy()
        env.update(trait_environment(scan.payload))
        env.update(
            {
                "RFID_MODE_CARD_ID": scan.card_id,
                "RFID_MODE_SCANNED_AT": scan.scanned_at,
                "RFID_MODE_SCAN_FILE": str(self.scan_file),
                "RFID_MODE_LOCK_DIR": str(self.scan_file.parent),
                "RFID_MODE_LCD_LOCK_DIR": str(self.lcd_lock_dir()),
                "RFID_MODE_SCRIPT": str(script),
                "RFID_MODE_KIND": mode_kind,
                "RFID_MODE_CONFIG_DIR": str(self.config_dir),
                "RFID_MODE_LOG_DIR": str(self.log_dir),
                "RFID_MODE_STATE_DIR": str(state_level),
                "RFID_MODE_STATE_PATH": "/".join(state_path_parts),
                "RFID_MODE_TRANSITION_FROM_STATE_DIR": str(level),
                "RFID_MODE_TRANSITION_FROM_STATE_PATH": "/".join(path_parts),
                "RFID_MODE_PREVIOUS_CARD_ID": str(self.state.get("current_card") or ""),
            }
        )
        if extra_env:
            env.update(extra_env)

        self.log(
            "mode_start",
            card_id=scan.card_id,
            script=str(script),
            scan_key=scan.key,
            state_path=state_path_parts,
            mode_log=str(mode_log),
            dry_run=self.dry_run,
        )
        if self.dry_run:
            updates = {
                "current_card": scan.card_id,
                "current_script": str(script),
                "current_mode_kind": mode_kind,
                "current_presence_id": self.scan_presence_id(scan),
                "pid": None,
                "mode_started_at": started_at,
                "mode_log": str(mode_log),
                "state_path": state_path_parts,
                "state_dir": str(state_level),
                "last_scan_key": scan.key,
                "last_scan_card": scan.card_id,
                "last_scan_seen_at": now_iso(),
                "hold_card": None,
                "hold_script": None,
                "hold_presence_id": None,
            }
            if extra_state:
                updates.update(extra_state)
            self.write_state(**updates)
            return

        log_handle = mode_log.open("a", encoding="utf-8")
        log_handle.write(
            f"# {started_at} start card={scan.card_id} state={'/'.join(state_path_parts)} script={script}\n"
        )
        log_handle.flush()
        proc = subprocess.Popen(
            self.script_command(script),
            cwd=str(level),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.emit_sound("rfid-run-triggered", RUN_SOUND, f"{scan.card_id} {mode_kind}")
        self.current_proc = proc
        updates = {
            "current_card": scan.card_id,
            "current_script": str(script),
            "current_mode_kind": mode_kind,
            "current_presence_id": self.scan_presence_id(scan),
            "pid": proc.pid,
            "mode_started_at": started_at,
            "mode_log": str(mode_log),
            "state_path": state_path_parts,
            "state_dir": str(state_level),
            "last_scan_key": scan.key,
            "last_scan_card": scan.card_id,
            "last_scan_seen_at": now_iso(),
            "hold_card": None,
            "hold_script": None,
            "hold_presence_id": None,
        }
        if extra_state:
            updates.update(extra_state)
        self.write_state(**updates)

    def scan_presence_id(self, scan: Scan) -> str:
        first_presence = str(scan.payload.get("first_presence_at") or "")
        return f"{scan.card_id}|{first_presence or scan.scanned_at or scan.key}"

    def scan_is_fully_read(self, scan: Scan) -> bool:
        deep_scan = scan.payload.get("deep_scan")
        if isinstance(deep_scan, dict):
            return deep_scan.get("status") == "ok"
        return bool(scan.payload.get("deep_read") or scan.payload.get("dump") or scan.payload.get("keys"))

    def scan_is_present(self, scan: Scan) -> bool:
        return not bool(scan.payload.get("released"))

    def scan_is_fresh(self, scan: Scan) -> bool:
        observed_at = parse_iso_datetime(
            scan.payload.get("last_presence_at")
            or scan.payload.get("scanned_at")
            or scan.payload.get("written_at")
        )
        if observed_at is None:
            return False
        age = (datetime.now(timezone.utc) - observed_at).total_seconds()
        return -60.0 <= age <= self.hold_fresh_seconds

    def confirm_resting_scan(self, scan: Scan, level: Path, path_parts: list[str], reason: str) -> Scan | None:
        if self.hold_confirm_seconds > 0:
            time.sleep(self.hold_confirm_seconds)
        confirmed = self.read_scan()
        if confirmed is None:
            self.log(f"{reason}_confirm_failed", card_id=scan.card_id, reason="scan_missing")
            return None
        if confirmed.card_id != scan.card_id or self.scan_presence_id(confirmed) != self.scan_presence_id(scan):
            self.log(
                f"{reason}_confirm_failed",
                card_id=scan.card_id,
                confirmed_card_id=confirmed.card_id,
                reason="card_changed",
            )
            return None
        current_level, current_parts = self.current_level()
        if current_level != level or current_parts != path_parts:
            self.log(f"{reason}_confirm_failed", card_id=scan.card_id, reason="state_changed")
            return None
        if (
            not self.scan_is_present(confirmed)
            or not self.scan_is_fully_read(confirmed)
            or not self.scan_is_fresh(confirmed)
        ):
            self.log(f"{reason}_confirm_failed", card_id=scan.card_id, reason="not_present_or_not_deep")
            return None
        return confirmed

    def maybe_apply_label_file(self, scan: Scan, level: Path, path_parts: list[str]) -> bool:
        raw_label = self.read_label_file(level)
        if raw_label is None:
            return False

        if len(path_parts) > 1:
            self.send_lcd_label_event(raw_label, scan=scan, path_parts=path_parts, reason="state_label")
            return True

        if len(path_parts) != 1 or scan.card_id != path_parts[-1]:
            return False
        desired = normalize_card_label(raw_label)
        current = normalize_card_label(scan.payload.get("lcd_label") or "")
        if desired == current:
            self.send_lcd_label_event(raw_label, scan=scan, path_parts=path_parts, reason="card_label_current")
            return False
        if (
            not self.scan_is_present(scan)
            or not self.scan_is_fully_read(scan)
            or not self.scan_is_fresh(scan)
        ):
            return False

        presence_id = self.scan_presence_id(scan)
        if (
            self.state.get("last_card_label_sync_presence_id") == presence_id
            and self.state.get("last_card_label_sync_label") == desired
            and self.state.get("last_card_label_sync_ok") is True
        ):
            return False

        confirmed = self.confirm_resting_scan(scan, level, path_parts, "label_sync")
        if confirmed is None:
            return False
        result = self.write_presented_card_label(confirmed, desired)
        ok = bool(result.get("ok"))
        self.log(
            "card_label_sync",
            card_id=scan.card_id,
            state_path=path_parts,
            desired_label=desired,
            previous_label=current,
            result=result,
        )
        self.write_state(
            last_card_label_sync_presence_id=presence_id,
            last_card_label_sync_label=desired,
            last_card_label_sync_ok=ok,
            last_card_label_sync_at=now_iso(),
        )
        if ok:
            self.emit_sound("rfid-card-copy-complete", COPY_COMPLETE_SOUND, scan.card_id)
            self.send_lcd_label_event(desired, scan=confirmed, path_parts=path_parts, reason="card_label_synced")
        return ok

    def hold_already_triggered(self, script: Path, scan: Scan, path_parts: list[str]) -> bool:
        return (
            self.state.get("last_hold_presence_id") == self.scan_presence_id(scan)
            and self.state.get("last_hold_state_path") == "/".join(path_parts)
            and self.state.get("last_hold_script") == str(script)
        )

    def maybe_stop_hold_for_scan(self, scan: Scan) -> None:
        if self.state.get("current_mode_kind") != "hold":
            return
        if (
            scan.card_id == str(self.state.get("hold_card") or "")
            and self.scan_presence_id(scan) == str(self.state.get("hold_presence_id") or "")
        ):
            return
        self.stop_current("hold_next_card", next_card=scan.card_id)

    def one_shot_already_triggered(self, script: Path, scan: Scan, path_parts: list[str]) -> bool:
        return (
            self.state.get("last_new_presence_id") == self.scan_presence_id(scan)
            and self.state.get("last_new_state_path") == "/".join(path_parts)
            and self.state.get("last_new_script") == str(script)
        )

    def top_level_dir_for_card(self, card_id: str) -> Path | None:
        target_dir = self.cards_dir / card_id
        try:
            target_dir.resolve(strict=False).relative_to(self.cards_dir.resolve())
        except ValueError:
            return None
        return target_dir if target_dir.is_dir() else None

    def new_script_chain_policy(self, script: Path) -> str:
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            self.log("chain_policy_read_failed", script=str(script), error=str(exc))
            return ""
        match = CHAIN_POLICY_RE.search(text)
        return match.group(1).strip().lower() if match else ""

    def new_script_runs_before_resolve(self, script: Path) -> bool:
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            self.log("new_script_policy_read_failed", script=str(script), error=str(exc))
            return False
        return NEW_BEFORE_RESOLVE_RE.search(text) is not None

    def new_script_requires_hold(self, script: Path) -> bool:
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            self.log("new_script_policy_read_failed", script=str(script), error=str(exc))
            return False
        return NEW_REQUIRES_HOLD_RE.search(text) is not None

    def clear_new_hold_arm_updates(self) -> dict[str, Any]:
        return {
            "new_hold_armed_card": None,
            "new_hold_armed_presence_id": None,
            "new_hold_armed_state_path": None,
            "new_hold_armed_script": None,
            "new_hold_armed_at": None,
        }

    def new_script_hold_is_armed(self, script: Path, path_parts: list[str]) -> bool:
        if not self.new_script_requires_hold(script):
            return True
        state_path = "/".join(path_parts)
        return (
            bool(path_parts)
            and self.state.get("new_hold_armed_card") == path_parts[-1]
            and self.state.get("new_hold_armed_state_path") == state_path
            and self.state.get("new_hold_armed_script") == str(script)
            and bool(self.state.get("new_hold_armed_presence_id"))
        )

    def log_new_script_hold_required_unarmed(self, scan: Scan, script: Path, path_parts: list[str]) -> None:
        state_path = "/".join(path_parts)
        signature = f"{state_path}|{script}|{scan.card_id}|{self.scan_presence_id(scan)}"
        if self.state.get("last_new_hold_unarmed_signature") == signature:
            return
        self.log(
            "new_script_hold_required_unarmed",
            card_id=scan.card_id,
            script=str(script),
            state_path=path_parts,
            scan_key=scan.key,
        )
        self.write_state(
            last_new_hold_unarmed_signature=signature,
            last_new_hold_unarmed_at=now_iso(),
        )

    def maybe_arm_new_script_for_hold(self, scan: Scan, level: Path, path_parts: list[str]) -> bool:
        if not path_parts or scan.card_id != path_parts[-1]:
            return False
        scripts = self.new_script_candidates(level)
        if not scripts:
            return False
        if len(scripts) > 1:
            self.log("new_script_conflict", card_id=scan.card_id, state_path=path_parts)
            return False
        script = scripts[0]
        if not self.new_script_requires_hold(script):
            return False
        if not self.script_is_runnable(script):
            self.log("new_script_not_executable", card_id=scan.card_id, script=str(script))
            return False

        presence_id = self.scan_presence_id(scan)
        state_path = "/".join(path_parts)
        if (
            self.state.get("new_hold_armed_presence_id") == presence_id
            and self.state.get("new_hold_armed_state_path") == state_path
            and self.state.get("new_hold_armed_script") == str(script)
        ):
            return False
        if not self.scan_is_present(scan) or not self.scan_is_fully_read(scan) or not self.scan_is_fresh(scan):
            return False

        confirmed = self.confirm_resting_scan(scan, level, path_parts, "new_hold_arm")
        if confirmed is None:
            return False

        confirmed_presence_id = self.scan_presence_id(confirmed)
        armed_at = now_iso()
        self.log(
            "new_script_hold_armed",
            card_id=confirmed.card_id,
            script=str(script),
            state_path=path_parts,
            scan_key=confirmed.key,
        )
        self.write_state(
            new_hold_armed_card=confirmed.card_id,
            new_hold_armed_presence_id=confirmed_presence_id,
            new_hold_armed_state_path=state_path,
            new_hold_armed_script=str(script),
            new_hold_armed_at=armed_at,
            last_scan_key=confirmed.key,
            last_scan_card=confirmed.card_id,
            last_scan_seen_at=now_iso(),
            state_path=path_parts,
            state_dir=str(level),
        )
        return True

    def maybe_apply_top_level_chain_policy(
        self,
        scan: Scan,
        level: Path,
        path_parts: list[str],
        top_level_dir: Path,
    ) -> bool:
        scripts = self.new_script_candidates(top_level_dir)
        if not scripts:
            return False
        if len(scripts) > 1:
            self.log("chain_policy_conflict", card_id=scan.card_id, top_level_dir=str(top_level_dir))
            return False
        policy = self.new_script_chain_policy(scripts[0])
        if policy == "terminate":
            self.log(
                "create_select_chain_terminated",
                card_id=scan.card_id,
                scan_key=scan.key,
                policy=policy,
                policy_script=str(scripts[0]),
                previous_state_path=path_parts,
                previous_state_dir=str(level),
                state_path=[scan.card_id],
                state_dir=str(top_level_dir),
            )
            self.navigate_top_level_card(scan, top_level_dir)
            return True
        if policy and policy not in {"expand"}:
            self.log(
                "chain_policy_unknown",
                card_id=scan.card_id,
                policy=policy,
                policy_script=str(scripts[0]),
            )
        return False

    def create_target_for_new_script(self, level: Path, path_parts: list[str], card_id: str) -> tuple[Path, list[str], str]:
        if len(path_parts) > 1:
            return level / card_id, path_parts + [card_id], "child"
        return self.cards_dir / card_id, [card_id], "top-level"

    def install_new_script_link(self, target_dir: Path, script: Path) -> None:
        target = target_dir / script.name
        if target.exists() or target.is_symlink():
            return
        try:
            relative = os.path.relpath(script, target_dir)
            target.symlink_to(relative)
        except OSError:
            try:
                target.write_bytes(script.read_bytes())
                target.chmod(target.stat().st_mode | 0o700)
            except OSError as exc:
                self.log(
                    "new_script_link_failed",
                    source=str(script),
                    target=str(target),
                    error=str(exc),
                )

    def migrate_exact_script_to_sidecar(self, level: Path, card_id: str) -> Path | None:
        exact = level / card_id
        if not exact.is_file():
            return None
        sidecar = level / f"{card_id}.sh"
        if sidecar.exists():
            self.log(
                "cleave_exact_script_migration_conflict",
                card_id=card_id,
                exact=str(exact),
                sidecar=str(sidecar),
            )
            return None
        exact.rename(sidecar)
        sidecar.chmod(sidecar.stat().st_mode | 0o700)
        self.log("cleave_exact_script_migrated", card_id=card_id, exact=str(exact), sidecar=str(sidecar))
        return sidecar

    def record_cleave_expansion(
        self,
        level: Path,
        *,
        scan: Scan,
        path_parts: list[str],
        child_dir: Path,
        top_level_dir: Path,
        script: Path,
        sidecar: Path | None,
    ) -> None:
        registry_dir = level / EXPANSION_DIR_NAME / "expanded"
        payload = {
            "card_id": scan.card_id,
            "created_at": now_iso(),
            "state_path": path_parts,
            "child_dir": str(child_dir),
            "top_level_dir": str(top_level_dir),
            "new_script": str(script),
            "sidecar": str(sidecar) if sidecar else None,
        }
        if self.dry_run:
            return
        try:
            registry_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(registry_dir / f"{scan.card_id}.json", payload)
        except OSError as exc:
            self.log("cleave_expansion_record_failed", card_id=scan.card_id, error=str(exc))

    def expand_existing_top_level_child(
        self,
        scan: Scan,
        level: Path,
        path_parts: list[str],
        script: Path,
        top_level_dir: Path,
    ) -> bool:
        if not path_parts or scan.card_id == path_parts[-1]:
            return False
        child_dir = level / scan.card_id
        sidecar: Path | None = None
        if child_dir.exists() and not child_dir.is_dir():
            sidecar = self.migrate_exact_script_to_sidecar(level, scan.card_id)
        elif not child_dir.exists():
            sidecar = self.migrate_exact_script_to_sidecar(level, scan.card_id)
        if child_dir.exists() and not child_dir.is_dir():
            self.log("cleave_child_conflict", card_id=scan.card_id, path=str(child_dir))
            return False
        if not self.dry_run:
            child_dir.mkdir(parents=True, exist_ok=True)
            self.install_new_script_link(child_dir, script)
            self.record_cleave_expansion(
                level,
                scan=scan,
                path_parts=path_parts,
                child_dir=child_dir,
                top_level_dir=top_level_dir,
                script=script,
                sidecar=sidecar,
            )
        self.log(
            "cleave_existing_top_level_expanded",
            card_id=scan.card_id,
            state_path=path_parts,
            child_dir=str(child_dir),
            top_level_dir=str(top_level_dir),
            new_script=str(script),
            sidecar=str(sidecar) if sidecar else None,
            dry_run=self.dry_run,
        )
        return True

    def navigate_top_level_card(self, scan: Scan, target_dir: Path) -> None:
        self.log(
            "navigation_top_level",
            card_id=scan.card_id,
            scan_key=scan.key,
            state_path=[scan.card_id],
            state_dir=str(target_dir),
        )
        self.write_state(
            state_path=[scan.card_id],
            state_dir=str(target_dir),
            last_scan_key=scan.key,
            last_scan_card=scan.card_id,
            last_scan_seen_at=now_iso(),
            **self.clear_new_hold_arm_updates(),
        )
        self.maybe_apply_label_file(scan, target_dir, [scan.card_id])
        self.maybe_arm_new_script_for_hold(scan, target_dir, [scan.card_id])

    def new_script_promotion_state(self, scan: Scan, level: Path, path_parts: list[str]) -> dict[str, Any]:
        target_dir, target_parts, scope = self.create_target_for_new_script(level, path_parts, scan.card_id)
        if not target_dir.is_dir():
            return {}
        self.log(
            "new_script_promoted_card",
            card_id=scan.card_id,
            state_path=target_parts,
            state_dir=str(target_dir),
            scope=scope,
        )
        if scope == "child":
            scripts = self.new_script_candidates(level)
            if scripts:
                self.install_new_script_link(target_dir, scripts[0])
        return {"state_path": target_parts, "state_dir": str(target_dir)}

    def run_one_shot_script(
        self,
        scan: Scan,
        script: Path,
        level: Path,
        path_parts: list[str],
        *,
        promote_on_success: bool = True,
    ) -> str:
        if not self.script_is_runnable(script):
            self.log("new_script_not_executable", card_id=scan.card_id, script=str(script))
            return "unhandled"

        presence_id = self.scan_presence_id(scan)
        state_path = "/".join(path_parts)
        scan_fully_read = self.scan_is_fully_read(scan)
        started_at = now_iso()
        safe_time = re.sub(r"[^0-9A-Za-z_.-]", "-", started_at)
        mode_log = self.mode_log_dir / f"{safe_time}_{'_'.join(path_parts + ['new', scan.card_id])}.log"
        env = os.environ.copy()
        env.update(trait_environment(scan.payload))
        create_target, create_parts, create_scope = self.create_target_for_new_script(level, path_parts, scan.card_id)
        env.update(
            {
                "RFID_MODE_CARD_ID": scan.card_id,
                "RFID_MODE_SCANNED_AT": scan.scanned_at,
                "RFID_MODE_SCAN_FILE": str(self.scan_file),
                "RFID_MODE_LOCK_DIR": str(self.scan_file.parent),
                "RFID_MODE_LCD_LOCK_DIR": str(self.lcd_lock_dir()),
                "RFID_MODE_SCRIPT": str(script),
                "RFID_MODE_KIND": "new",
                "RFID_MODE_NEW": "1",
                "RFID_MODE_NEW_PRESENCE_ID": presence_id,
                "RFID_MODE_CONFIG_DIR": str(self.config_dir),
                "RFID_MODE_LOG_DIR": str(self.log_dir),
                "RFID_MODE_STATE_DIR": str(level),
                "RFID_MODE_STATE_PATH": state_path,
                "RFID_MODE_CREATE_TARGET_DIR": str(create_target),
                "RFID_MODE_CREATE_TARGET_PATH": "/".join(create_parts),
                "RFID_MODE_CREATE_SCOPE": create_scope,
                "RFID_MODE_PREVIOUS_CARD_ID": str(self.state.get("current_card") or ""),
            }
        )
        updates = {
            "last_scan_key": scan.key,
            "last_scan_card": scan.card_id,
            "last_scan_seen_at": now_iso(),
            "state_path": path_parts,
            "state_dir": str(level),
            "last_new_presence_id": presence_id,
            "last_new_state_path": state_path,
            "last_new_script": str(script),
            "last_new_scan_fully_read": scan_fully_read,
            "last_new_triggered_at": started_at,
        }
        self.log(
            "new_script_start",
            card_id=scan.card_id,
            script=str(script),
            scan_key=scan.key,
            state_path=path_parts,
            mode_log=str(mode_log),
            dry_run=self.dry_run,
        )
        if self.dry_run:
            updates["last_new_result"] = "handled"
            updates["last_new_completed_at"] = now_iso()
            self.write_state(**updates)
            return "handled"

        status: int | None = None
        timed_out = False
        log_handle = mode_log.open("a", encoding="utf-8")
        try:
            log_handle.write(
                f"# {started_at} one-shot card={scan.card_id} state={state_path} script={script}\n"
            )
            log_handle.flush()
            proc = subprocess.Popen(
                self.script_command(script),
                cwd=str(level),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            self.emit_sound("rfid-run-triggered", RUN_SOUND, f"{scan.card_id} new")
            try:
                timeout = self.one_shot_timeout if self.one_shot_timeout > 0 else None
                status = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.log("new_script_timeout", card_id=scan.card_id, script=str(script), pid=proc.pid)
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + self.stop_timeout
                while pid_exists(proc.pid) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if pid_exists(proc.pid):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                status = proc.poll()
        except OSError as exc:
            self.log("new_script_start_failed", card_id=scan.card_id, script=str(script), error=str(exc))
            return "unhandled"
        finally:
            log_handle.close()

        self.log(
            "new_script_exit",
            card_id=scan.card_id,
            script=str(script),
            returncode=status,
            timed_out=timed_out,
        )
        result = "continue" if status == NEW_SCRIPT_CONTINUE_EXIT and not timed_out else "handled"
        updates["last_new_result"] = result
        updates["last_new_completed_at"] = now_iso()
        if status == 0 and not timed_out and promote_on_success:
            updates.update(self.new_script_promotion_state(scan, level, path_parts))
        self.write_state(**updates)
        return result

    def maybe_run_new_script_before_resolve(self, scan: Scan, level: Path, path_parts: list[str]) -> str:
        scripts = self.new_script_candidates(level)
        if not scripts:
            return "unhandled"
        if len(scripts) > 1:
            self.log("new_script_conflict", card_id=scan.card_id, state_path=path_parts)
            return "unhandled"
        script = scripts[0]
        if not self.new_script_runs_before_resolve(script):
            return "unhandled"
        if not self.new_script_hold_is_armed(script, path_parts):
            self.log_new_script_hold_required_unarmed(scan, script, path_parts)
            return "unhandled"
        if self.one_shot_already_triggered(script, scan, path_parts):
            previous_result = str(self.state.get("last_new_result") or "")
            previous_fully_read = bool(self.state.get("last_new_scan_fully_read"))
            current_fully_read = self.scan_is_fully_read(scan)
            if previous_result == "continue" and not previous_fully_read and current_fully_read:
                return self.run_one_shot_script(
                    scan,
                    script,
                    level,
                    path_parts,
                    promote_on_success=False,
                )
            if previous_result in {"continue", "handled"}:
                return previous_result
            return "handled" if current_fully_read else "continue"
        return self.run_one_shot_script(scan, script, level, path_parts, promote_on_success=False)

    def maybe_run_new_script(self, scan: Scan, level: Path, path_parts: list[str]) -> bool:
        scripts = self.new_script_candidates(level)
        if not scripts:
            return False
        if len(scripts) > 1:
            self.log("new_script_conflict", card_id=scan.card_id, state_path=path_parts)
            return False
        script = scripts[0]
        if not self.script_is_runnable(script):
            self.log("new_script_not_executable", card_id=scan.card_id, script=str(script))
            return False
        if not self.new_script_hold_is_armed(script, path_parts):
            self.log_new_script_hold_required_unarmed(scan, script, path_parts)
            return False
        if self.one_shot_already_triggered(script, scan, path_parts):
            promotion = self.new_script_promotion_state(scan, level, path_parts)
            self.write_state(
                **{
                    "last_scan_key": scan.key,
                    "last_scan_card": scan.card_id,
                    "last_scan_seen_at": now_iso(),
                    "state_path": path_parts,
                    "state_dir": str(level),
                    **promotion,
                }
            )
            return True
        return self.run_one_shot_script(scan, script, level, path_parts) == "handled"

    def start_hold(self, scan: Scan, script: Path, level: Path, path_parts: list[str]) -> None:
        presence_id = self.scan_presence_id(scan)
        state_path = "/".join(path_parts)
        triggered_at = now_iso()
        self.start_mode(
            scan,
            script,
            level,
            path_parts,
            next_level=level,
            next_path_parts=path_parts,
            mode_kind="hold",
            extra_env={
                "RFID_MODE_HOLD": "1",
                "RFID_MODE_HOLD_PRESENCE_ID": presence_id,
                "RFID_MODE_HOLD_CONFIRMED_AT": triggered_at,
            },
            extra_state={
                "hold_card": scan.card_id,
                "hold_script": str(script),
                "hold_presence_id": presence_id,
                "hold_state_path": state_path,
                "last_hold_presence_id": presence_id,
                "last_hold_state_path": state_path,
                "last_hold_script": str(script),
                "last_hold_triggered_at": triggered_at,
            },
        )

    def maybe_start_hold(self, scan: Scan, level: Path, path_parts: list[str]) -> bool:
        if not path_parts or scan.card_id != path_parts[-1]:
            return False
        scripts = self.hold_script_candidates(level)
        if not scripts:
            return False
        if len(scripts) > 1:
            self.log("hold_script_conflict", card_id=scan.card_id, state_path=path_parts)
            return False
        script = scripts[0]
        if not self.script_is_runnable(script):
            self.log("hold_script_not_executable", card_id=scan.card_id, script=str(script))
            return False
        if self.hold_already_triggered(script, scan, path_parts):
            return False
        if not self.scan_is_present(scan) or not self.scan_is_fully_read(scan) or not self.scan_is_fresh(scan):
            return False

        confirmed = self.confirm_resting_scan(scan, level, path_parts, "hold")
        if confirmed is None:
            return False

        self.log(
            "hold_start_confirmed",
            card_id=confirmed.card_id,
            script=str(script),
            state_path=path_parts,
            scan_key=confirmed.key,
        )
        self.start_hold(confirmed, script, level, path_parts)
        return True

    def handle_scan(self, scan: Scan) -> None:
        level, path_parts = self.current_level()
        self.maybe_stop_hold_for_scan(scan)
        if scan.key == self.state.get("last_scan_key"):
            self.maybe_apply_label_file(scan, level, path_parts)
            self.maybe_start_hold(scan, level, path_parts)
            self.maybe_arm_new_script_for_hold(scan, level, path_parts)
            return
        if self.one_shot_mode_presence_already_handled(scan, level, path_parts):
            self.log(
                "one_shot_mode_presence_ignored",
                card_id=scan.card_id,
                scan_key=scan.key,
                presence_id=self.scan_presence_id(scan),
            )
            self.write_state(
                last_scan_key=scan.key,
                last_scan_card=scan.card_id,
                last_scan_seen_at=now_iso(),
                state_path=[],
                state_dir=str(self.cards_dir),
            )
            return
        new_result = "unhandled"
        if path_parts and scan.card_id != path_parts[-1] and self.new_script_candidates(level):
            new_result = self.maybe_run_new_script_before_resolve(scan, level, path_parts)
            if new_result == "handled":
                return
            top_level_dir = self.top_level_dir_for_card(scan.card_id)
            if top_level_dir is not None:
                if self.maybe_apply_top_level_chain_policy(scan, level, path_parts, top_level_dir):
                    return
                scripts = self.new_script_candidates(level)
                if len(scripts) == 1:
                    if self.new_script_hold_is_armed(scripts[0], path_parts):
                        self.expand_existing_top_level_child(scan, level, path_parts, scripts[0], top_level_dir)
                    else:
                        self.log_new_script_hold_required_unarmed(scan, scripts[0], path_parts)
                else:
                    self.log("new_script_conflict", card_id=scan.card_id, state_path=path_parts)
            elif new_result != "continue" and self.maybe_run_new_script(scan, level, path_parts):
                return
        resolution = self.resolve_transition(level, scan.card_id)

        if (
            resolution.action == "reset"
            and resolution.reason == "no_matching_transition"
            and path_parts
            and scan.card_id == path_parts[-1]
        ):
            state_path = "/".join(path_parts)
            presence_id = self.scan_presence_id(scan)
            already_logged = (
                self.state.get("last_current_directory_presence_id") == presence_id
                and self.state.get("last_current_directory_state_path") == state_path
            )
            if not already_logged:
                self.log(
                    "scan_current_directory_ignored",
                    card_id=scan.card_id,
                    scan_key=scan.key,
                    state_path=path_parts,
                )
            self.write_state(
                last_scan_key=scan.key,
                last_scan_card=scan.card_id,
                last_scan_seen_at=now_iso(),
                state_path=path_parts,
                state_dir=str(level),
                last_current_directory_presence_id=presence_id,
                last_current_directory_state_path=state_path,
            )
            self.maybe_apply_label_file(scan, level, path_parts)
            self.maybe_start_hold(scan, level, path_parts)
            self.maybe_arm_new_script_for_hold(scan, level, path_parts)
            return

        if resolution.action == "navigate" and resolution.path is not None:
            next_parts = path_parts + [scan.card_id]
            self.log(
                "navigation_enter",
                card_id=scan.card_id,
                scan_key=scan.key,
                state_path=next_parts,
                state_dir=str(resolution.path),
            )
            self.write_state(
                state_path=next_parts,
                state_dir=str(resolution.path),
                last_scan_key=scan.key,
                last_scan_card=scan.card_id,
                last_scan_seen_at=now_iso(),
                **self.clear_new_hold_arm_updates(),
            )
            self.maybe_apply_label_file(scan, resolution.path, next_parts)
            self.maybe_start_hold(scan, resolution.path, next_parts)
            self.maybe_arm_new_script_for_hold(scan, resolution.path, next_parts)
            return

        if resolution.action == "execute" and resolution.path is not None:
            self.start_mode(scan, resolution.path, level, path_parts)
            return

        if (
            resolution.action == "execute_and_navigate"
            and resolution.path is not None
            and resolution.next_path is not None
        ):
            next_parts = path_parts + [scan.card_id]
            self.start_mode(
                scan,
                resolution.path,
                level,
                path_parts,
                next_level=resolution.next_path,
                next_path_parts=next_parts,
            )
            self.maybe_apply_label_file(scan, resolution.next_path, next_parts)
            self.maybe_start_hold(scan, resolution.next_path, next_parts)
            self.maybe_arm_new_script_for_hold(scan, resolution.next_path, next_parts)
            return

        if resolution.action == "reset" and resolution.reason == "no_matching_transition":
            if (
                new_result != "continue"
                and self.maybe_run_new_script(scan, level, path_parts)
            ):
                return
            top_level_dir = self.top_level_dir_for_card(scan.card_id)
            if top_level_dir is not None:
                self.navigate_top_level_card(scan, top_level_dir)
                return

        self.log(
            "scan_unmapped_reset",
            card_id=scan.card_id,
            scan_key=scan.key,
            previous_state_path=path_parts,
            reason=resolution.reason,
        )
        self.reset_navigation(resolution.reason or "unmapped", scan)

    def run_once(self) -> int:
        self.setup()
        scan = self.read_scan()
        if scan is None:
            self.log("scan_missing_or_invalid", scan_file=str(self.scan_file))
            return 1
        self.handle_scan(scan)
        self.reap_current_if_exited()
        return 0

    def run_forever(self) -> int:
        self.setup()

        def _stop(signum: int, _frame: Any) -> None:
            self.running = False
            self.log("runner_signal", signal=signum)

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        self.log("runner_start", scan_file=str(self.scan_file), config_dir=str(self.config_dir))
        while self.running:
            self.reap_current_if_exited()
            scan = self.read_scan()
            if scan is not None:
                self.handle_scan(scan)
            time.sleep(self.poll_seconds)
        self.log("runner_stop")
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local modes from RFID transition paths.")
    parser.add_argument("--scan-file", type=Path, default=DEFAULT_SCAN_FILE)
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--poll-seconds", type=float, default=0.35)
    parser.add_argument("--stop-timeout", type=float, default=5.0)
    parser.add_argument("--hold-confirm-seconds", type=float, default=DEFAULT_HOLD_CONFIRM_SECONDS)
    parser.add_argument("--hold-fresh-seconds", type=float, default=DEFAULT_HOLD_FRESH_SECONDS)
    parser.add_argument("--one-shot-timeout", type=float, default=DEFAULT_ONE_SHOT_TIMEOUT)
    parser.add_argument("--once", action="store_true", help="process the current scan once")
    parser.add_argument("--dry-run", action="store_true", help="log actions without starting/stopping scripts")
    parser.add_argument("--status", action="store_true", help="print current runner state")
    parser.add_argument("--stop-current", action="store_true", help="stop the current mode process")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = ModeRunner(
        scan_file=args.scan_file,
        config_dir=args.config_dir,
        poll_seconds=args.poll_seconds,
        stop_timeout=args.stop_timeout,
        hold_confirm_seconds=args.hold_confirm_seconds,
        hold_fresh_seconds=args.hold_fresh_seconds,
        one_shot_timeout=args.one_shot_timeout,
        dry_run=args.dry_run,
    )
    runner.setup()
    if args.status:
        print(json.dumps(runner.state, indent=2, sort_keys=True))
        return 0
    if args.stop_current:
        runner.stop_current("manual_stop")
        return 0
    if args.once:
        return runner.run_once()
    return runner.run_forever()


if __name__ == "__main__":
    sys.exit(main())
