#!/usr/bin/env python3
"""Publish RFID scans to local lockfiles without the Arthexis runtime."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HOME = Path.home()
DEFAULT_STATE_DIR = HOME / ".local" / "state" / "rfid-mode"
DEFAULT_SCAN_FILE = DEFAULT_STATE_DIR / "rfid-scan.json"
DEFAULT_LOG_FILE = DEFAULT_STATE_DIR / "rfid-scans.ndjson"
DEFAULT_STATUS_FILE = DEFAULT_STATE_DIR / "rfid-scan-publisher.json"
DEFAULT_LCD_LOCK_DIR = HOME / ".local" / "state" / "lcd-lockfiles"
DEFAULT_SOUND_HOOK = HOME / ".local" / "bin" / "codex-sound-hook"
DEFAULT_CARDS_FILE = HOME / "cards.txt"

SCHEMA = "arthexis.rfid.scan.v1"
FACTORY_KEY = [0xFF] * 6
COMMON_KEYS = (
    "FFFFFFFFFFFF",
    "A0A1A2A3A4A5",
    "B0B1B2B3B4B5",
    "000000000000",
    "D3F7D3F7D3F7",
    "AABBCCDDEEFF",
    "1A2B3C4D5E6F",
    "4D3A99C351DD",
    "123456789ABC",
    "ABCDEF123456",
)
SENSITIVE_LOG_KEYS = {"keys"}
MATCH_PREFIX_LENGTH = 8

STOP = False


def quiet_mfrc522_logs() -> None:
    for logger_name in ("mfrc522Logger", "mfrc522"):
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)


def handle_signal(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"", "0", "false", "no", "off"}


def expand_path(raw: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw))))


def clean_ascii(raw: object, *, limit: int = 64) -> str:
    text = "" if raw is None else str(raw)
    text = re.sub(r"[\x00-\x1F\x7F]", " ", text)
    text = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def normalize_code(value: object) -> str:
    return "".join(str(value or "").split()).upper()


def reverse_uid(value: object) -> str:
    normalized = normalize_code(value)
    if not normalized:
        return ""
    if len(normalized) % 2:
        return normalized[::-1]
    parts = [normalized[index : index + 2] for index in range(0, len(normalized), 2)]
    parts.reverse()
    return "".join(parts)


def normalize_card_id(raw: object) -> str:
    value = normalize_code(raw).replace(":", "")
    return re.sub(r"[^A-Z0-9_.-]+", "_", value)


def uid_to_rfid(uid: list[int]) -> str:
    return "".join(f"{int(value) & 0xFF:02X}" for value in uid)


def key_to_bytes(raw: str) -> list[int]:
    normalized = normalize_code(raw)
    if len(normalized) != 12:
        return FACTORY_KEY
    try:
        return [int(normalized[index : index + 2], 16) for index in range(0, 12, 2)]
    except ValueError:
        return FACTORY_KEY


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def sound_enabled() -> bool:
    return not parse_bool(os.environ.get("RFID_SCAN_SOUND_DISABLED"), default=False)


def play_sound(hook: Path, event: str, sound: str, detail: str = "") -> None:
    del detail
    if not sound_enabled() or not sound or not hook.exists():
        return
    env = os.environ.copy()
    env.setdefault("CODEX_SOUND_HOOK_VOLUME", os.environ.get("RFID_SCAN_SOUND_VOLUME", "0.21"))
    try:
        subprocess.Popen(
            [str(hook), event, sound],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        return


def write_lcd_event(lock_dir: Path, event_id: int, line1: str, line2: str, *, seconds: int) -> None:
    if seconds <= 0:
        return
    expires = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    path = lock_dir / f"lcd-event-{event_id}.lck"
    text = f"{clean_ascii(line1, limit=64)}\n{clean_ascii(line2, limit=64)}\n{expires.isoformat()}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def decode_fixed_ascii(values: list[int]) -> str:
    raw = bytes((int(value) & 0xFF) for value in values)
    return raw.rstrip(b"\x00 ").decode("ascii", errors="ignore")


def decode_lcd_label(blocks: dict[int, list[int]]) -> str:
    block1 = blocks.get(1)
    block2 = blocks.get(2)
    if not block1:
        return ""
    line1 = decode_fixed_ascii(block1[:16])
    line2 = decode_fixed_ascii((block2 or [])[:16])
    return "\n".join((line1, line2)).rstrip("\n")


def generated_label_for_rfid(rfid: str) -> str:
    value = normalize_card_id(rfid)
    return f"RFID {value[-6:]}" if value else "RFID"


def name_key_for_rfid(rfid: str) -> str:
    value = normalize_card_id(rfid)
    return value.lower() if value else ""


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    current = now_iso()
    normalized.setdefault("schema", SCHEMA)
    normalized.setdefault("service_mode", "local")
    normalized.setdefault("written_at", current)
    normalized.setdefault("scanned_at", current)
    rfid = normalize_card_id(normalized.get("rfid", ""))
    if rfid:
        normalized["rfid"] = rfid
        normalized.setdefault("name_key", name_key_for_rfid(rfid))
        normalized.setdefault("generated_label", generated_label_for_rfid(rfid))
        normalized.setdefault("display_label", normalized.get("lcd_label") or normalized["generated_label"])
    return normalized


def log_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = normalize_payload(payload)
    for key in SENSITIVE_LOG_KEYS:
        result.pop(key, None)
    return result


def load_card_codes(path: Path) -> list[str]:
    if not path.exists():
        return []
    codes: list[str] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        raw_code = line.split(",", 1)[0] if "," in line else line.split(maxsplit=1)[0]
        code = normalize_code(raw_code)
        if code:
            codes.append(code)
    return codes


def matches_existing_card(candidate: str, existing_codes: list[str]) -> bool:
    normalized = normalize_code(candidate)
    if not normalized:
        return True
    alternate = reverse_uid(normalized)
    checks = [normalized]
    if alternate and alternate != normalized:
        checks.append(alternate)
    if any(code in checks for code in existing_codes):
        return True
    prefixes = {check[: min(len(check), MATCH_PREFIX_LENGTH)] for check in checks if check}
    return any(any(code.startswith(prefix) for prefix in prefixes if prefix) for code in existing_codes)


def append_card_if_new(path: Path | None, rfid: str) -> bool:
    if path is None or not rfid:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# One RFID idTag per line. Add an optional label after a comma.\n"
            "# Lines beginning with # are ignored.\n",
            encoding="utf-8",
        )
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            import fcntl
        except ModuleNotFoundError:
            fcntl = None
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        existing = load_card_codes(path)
        if matches_existing_card(rfid, existing):
            return False
        with path.open("a", encoding="utf-8") as cards_file:
            cards_file.write(f"{rfid}\n")
        return True


@dataclass
class ScanObservation:
    rfid: str
    uid: list[int]


class HardwareReader:
    def __init__(self, *, bus: int, device: int, pin_mode: int, pin_rst: int) -> None:
        self.bus = bus
        self.device = device
        self.pin_mode = pin_mode
        self.pin_rst = pin_rst
        self._mfrc: Any | None = None

    def open(self) -> None:
        try:
            import RPi.GPIO as GPIO  # type: ignore

            GPIO.setwarnings(False)
        except Exception:
            pass
        from mfrc522 import MFRC522  # type: ignore

        self._mfrc = MFRC522(
            bus=self.bus,
            device=self.device,
            pin_mode=self.pin_mode,
            pin_rst=self.pin_rst,
        )

    @property
    def mfrc(self) -> Any:
        if self._mfrc is None:
            self.open()
        return self._mfrc

    def close_crypto(self) -> None:
        if self._mfrc is None:
            return
        try:
            self._mfrc.MFRC522_StopCrypto1()
        except Exception:
            pass

    def read_fast(self) -> ScanObservation | None:
        mfrc = self.mfrc
        status, _tag_type = mfrc.MFRC522_Request(mfrc.PICC_REQIDL)
        if status != mfrc.MI_OK:
            return None
        status, uid = mfrc.MFRC522_Anticoll()
        if status != mfrc.MI_OK or not uid:
            return None
        try:
            mfrc.MFRC522_SelectTag(uid)
        except Exception:
            pass
        return ScanObservation(rfid=uid_to_rfid(list(uid)), uid=list(uid))

    def read_block(self, block: int, uid: list[int], *, key_type: str, key_bytes: list[int]) -> list[int] | None:
        mfrc = self.mfrc
        auth_mode = mfrc.PICC_AUTHENT1B if key_type == "B" else mfrc.PICC_AUTHENT1A
        status = mfrc.MFRC522_Auth(auth_mode, block, key_bytes, uid)
        if status != mfrc.MI_OK:
            return None
        result = mfrc.MFRC522_Read(block)
        if isinstance(result, tuple):
            read_status, data = result
        else:
            read_status, data = mfrc.MI_OK, result
        if read_status != mfrc.MI_OK or data is None:
            return None
        return [int(value) & 0xFF for value in list(data)[:16]]

    def read_deep(self, uid: list[int], *, max_blocks: int) -> dict[str, Any]:
        dump: list[dict[str, Any]] = []
        block_map: dict[int, list[int]] = {}
        verified_keys: dict[str, Any] = {"a": "", "a_verified": False, "b": "", "b_verified": False}
        started = now_iso()
        for block in range(max(0, max_blocks)):
            for key_type in ("A", "B"):
                for key_value in COMMON_KEYS:
                    data = self.read_block(block, uid, key_type=key_type, key_bytes=key_to_bytes(key_value))
                    if data is None:
                        continue
                    dump.append({"block": block, "data": data, "key": key_type})
                    block_map[block] = data
                    lower = key_type.lower()
                    verified_keys[lower] = key_value
                    verified_keys[f"{lower}_verified"] = True
                    break
                if block in block_map:
                    break
        label = decode_lcd_label(block_map)
        payload: dict[str, Any] = {
            "deep_read": bool(dump),
            "deep_scan": {
                "automatic": True,
                "attempted_at": started,
                "completed_at": now_iso(),
                "status": "ok" if dump else "no-deep-data",
                "blocks": len(dump),
            },
            "dump": dump,
            "keys": verified_keys,
        }
        if label:
            payload["lcd_label"] = label
            payload["card_name"] = label.split("\n", 1)[0]
            payload["display_label"] = label
        return payload


class Publisher:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.reader = HardwareReader(
            bus=args.spi_bus,
            device=args.spi_device,
            pin_mode=args.gpio_pin_mode,
            pin_rst=args.rst_pin,
        )
        self.current_rfid = ""
        self.current_uid: list[int] = []
        self.first_presence_at = ""
        self.presence_started_monotonic = 0.0
        self.last_seen_monotonic = 0.0
        self.last_emit_monotonic = 0.0
        self.deep_attempted = False
        self.release_emitted = True
        self.current_payload: dict[str, Any] | None = None

    def payload_is_deep_complete(self, payload: dict[str, Any] | None = None) -> bool:
        payload = payload if payload is not None else self.current_payload
        if not isinstance(payload, dict):
            return False
        deep_scan = payload.get("deep_scan")
        if isinstance(deep_scan, dict):
            return deep_scan.get("status") == "ok"
        return bool(payload.get("deep_read") or payload.get("dump") or payload.get("keys"))

    def status(self, **updates: Any) -> None:
        payload = {
            "updated_at": now_iso(),
            "running": not STOP,
            "current_rfid": self.current_rfid or None,
            **updates,
        }
        atomic_write_json(self.args.status_file, payload)

    def emit(self, payload: dict[str, Any], *, log: bool = True) -> dict[str, Any]:
        normalized = normalize_payload(payload)
        atomic_write_json(self.args.scan_file, normalized)
        if log:
            append_jsonl(self.args.log_file, log_payload(normalized))
        if self.args.cards_file and normalized.get("rfid") and not normalized.get("released"):
            added = append_card_if_new(self.args.cards_file, str(normalized["rfid"]))
            if added:
                append_jsonl(self.args.event_log, {"timestamp": now_iso(), "event": "cards_file_append", "rfid": normalized["rfid"], "path": str(self.args.cards_file)})
        self.current_payload = normalized
        self.status(last_emit=normalized)
        return normalized

    def base_payload(self, observation: ScanObservation, observed_at: str, observed_monotonic: float) -> dict[str, Any]:
        duration = max(0.0, observed_monotonic - self.presence_started_monotonic)
        return {
            "schema": SCHEMA,
            "service_mode": "local",
            "rfid": observation.rfid,
            "scanned_at": observed_at,
            "written_at": observed_at,
            "first_presence_at": self.first_presence_at or observed_at,
            "last_presence_at": observed_at,
            "presence_duration_seconds": round(duration, 3),
            "card_present": True,
            "reader": {
                "bus": self.args.spi_bus,
                "device": self.args.spi_device,
                "rst_pin": self.args.rst_pin,
            },
        }

    def start_presence(self, observation: ScanObservation, observed_at: str, observed_monotonic: float) -> None:
        self.current_rfid = observation.rfid
        self.current_uid = list(observation.uid)
        self.first_presence_at = observed_at
        self.presence_started_monotonic = observed_monotonic
        self.last_seen_monotonic = observed_monotonic
        self.last_emit_monotonic = observed_monotonic
        self.deep_attempted = False
        self.release_emitted = False
        payload = self.emit(self.base_payload(observation, observed_at, observed_monotonic))
        append_jsonl(self.args.event_log, {"timestamp": now_iso(), "event": "scan", "rfid": observation.rfid})
        play_sound(self.args.sound_hook, "rfid-scan", self.args.normal_sound, observation.rfid)
        write_lcd_event(self.args.lcd_lock_dir, self.args.lcd_event_id, "RFID SCAN", payload.get("display_label") or observation.rfid, seconds=self.args.lcd_event_seconds)

    def refresh_presence(self, observation: ScanObservation, observed_at: str, observed_monotonic: float) -> None:
        self.last_seen_monotonic = observed_monotonic
        base = self.base_payload(observation, observed_at, observed_monotonic)
        if self.current_payload:
            enriched = dict(self.current_payload)
            for key, value in base.items():
                if key in {"deep_read", "deep_scan", "dump", "keys", "lcd_label", "card_name", "display_label"}:
                    continue
                enriched[key] = value
            payload = enriched
        else:
            payload = base
        if self.payload_is_deep_complete(payload):
            self.current_payload = normalize_payload(payload)
            self.status(last_seen=self.current_payload)
            return
        if observed_monotonic - self.last_emit_monotonic >= self.args.refresh_seconds:
            self.emit(payload)
            self.last_emit_monotonic = observed_monotonic

    def maybe_deep_read(self, observation: ScanObservation, observed_at: str, observed_monotonic: float) -> None:
        if self.deep_attempted:
            return
        if observed_monotonic - self.presence_started_monotonic < self.args.deep_hold_seconds:
            return
        self.deep_attempted = True
        base = self.base_payload(observation, observed_at, observed_monotonic)
        try:
            deep = self.reader.read_deep(observation.uid, max_blocks=self.args.deep_blocks)
        finally:
            self.reader.close_crypto()
        payload = dict(self.current_payload or base)
        payload.update(base)
        payload.update(deep)
        emitted = self.emit(payload)
        append_jsonl(
            self.args.event_log,
            {
                "timestamp": now_iso(),
                "event": "deep_read",
                "rfid": observation.rfid,
                "status": (emitted.get("deep_scan") or {}).get("status"),
                "blocks": len(emitted.get("dump") or []),
            },
        )
        if (emitted.get("deep_scan") or {}).get("status") == "ok":
            play_sound(self.args.sound_hook, "rfid-deep-read-complete", self.args.deep_sound, observation.rfid)
            write_lcd_event(self.args.lcd_lock_dir, self.args.lcd_event_id, "RFID DEEP OK", emitted.get("display_label") or observation.rfid, seconds=self.args.lcd_event_seconds)
        self.last_seen_monotonic = time.monotonic()
        self.last_emit_monotonic = self.last_seen_monotonic

    def maybe_release(self, observed_at: str, observed_monotonic: float) -> None:
        if not self.current_rfid or self.release_emitted:
            return
        if observed_monotonic - self.last_seen_monotonic < self.args.release_gap_seconds:
            return
        payload = dict(self.current_payload or {})
        payload.update(
            {
                "schema": SCHEMA,
                "service_mode": "local",
                "rfid": self.current_rfid,
                "scanned_at": observed_at,
                "written_at": observed_at,
                "last_presence_at": payload.get("last_presence_at") or observed_at,
                "released_at": observed_at,
                "released": True,
                "card_present": False,
            }
        )
        self.emit(payload)
        append_jsonl(self.args.event_log, {"timestamp": now_iso(), "event": "release", "rfid": self.current_rfid})
        self.release_emitted = True
        self.current_rfid = ""
        self.current_uid = []
        self.first_presence_at = ""
        self.current_payload = None

    def handle_observation(self, observation: ScanObservation) -> None:
        observed_at = now_iso()
        observed_monotonic = time.monotonic()
        new_presence = (
            observation.rfid != self.current_rfid
            or not self.current_rfid
        )
        if new_presence:
            self.start_presence(observation, observed_at, observed_monotonic)
        else:
            self.refresh_presence(observation, observed_at, observed_monotonic)
        self.maybe_deep_read(observation, observed_at, observed_monotonic)

    def run_once(self) -> int:
        self.status(started_at=now_iso(), mode="once")
        deadline = time.monotonic() + self.args.once_timeout
        while not STOP and time.monotonic() < deadline:
            observation = self.reader.read_fast()
            if observation:
                self.handle_observation(observation)
                print(json.dumps(self.current_payload or {}, indent=2, sort_keys=True))
                return 0
            time.sleep(self.args.poll_seconds)
        print("No RFID card detected", file=sys.stderr)
        return 1

    def run_forever(self) -> int:
        self.status(started_at=now_iso(), mode="service")
        append_jsonl(self.args.event_log, {"timestamp": now_iso(), "event": "publisher_start"})
        while not STOP:
            try:
                observation = self.reader.read_fast()
            except Exception as exc:
                self.status(error=str(exc))
                append_jsonl(self.args.event_log, {"timestamp": now_iso(), "event": "reader_error", "error": str(exc)})
                time.sleep(self.args.error_sleep_seconds)
                continue
            if observation:
                self.handle_observation(observation)
            else:
                self.maybe_release(now_iso(), time.monotonic())
            time.sleep(self.args.poll_seconds)
        append_jsonl(self.args.event_log, {"timestamp": now_iso(), "event": "publisher_stop"})
        self.status(stopped_at=now_iso(), running=False)
        return 0


def default_cards_file() -> Path | None:
    configured = os.environ.get("RFID_SCAN_CARDS_FILE")
    if configured:
        return expand_path(configured)
    return DEFAULT_CARDS_FILE if DEFAULT_CARDS_FILE.exists() else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone local RFID scan publisher.")
    parser.add_argument("--scan-file", type=Path, default=expand_path(os.environ.get("RFID_SCAN_FILE", DEFAULT_SCAN_FILE)))
    parser.add_argument("--log-file", type=Path, default=expand_path(os.environ.get("RFID_SCAN_LOG_FILE", DEFAULT_LOG_FILE)))
    parser.add_argument("--event-log", type=Path, default=expand_path(os.environ.get("RFID_SCAN_EVENT_LOG", DEFAULT_STATE_DIR / "events.ndjson")))
    parser.add_argument("--status-file", type=Path, default=expand_path(os.environ.get("RFID_SCAN_STATUS_FILE", DEFAULT_STATUS_FILE)))
    parser.add_argument("--lcd-lock-dir", type=Path, default=expand_path(os.environ.get("RFID_SCAN_LCD_LOCK_DIR", DEFAULT_LCD_LOCK_DIR)))
    parser.add_argument("--lcd-event-id", type=int, default=int(os.environ.get("RFID_SCAN_LCD_EVENT_ID", "4")))
    parser.add_argument("--lcd-event-seconds", type=int, default=int(os.environ.get("RFID_SCAN_LCD_EVENT_SECONDS", "10")))
    parser.add_argument("--sound-hook", type=Path, default=expand_path(os.environ.get("RFID_SCAN_SOUND_HOOK", DEFAULT_SOUND_HOOK)))
    parser.add_argument("--normal-sound", default=os.environ.get("RFID_SCAN_NORMAL_SOUND", "notice"))
    parser.add_argument("--deep-sound", default=os.environ.get("RFID_SCAN_DEEP_SOUND", "ok"))
    parser.add_argument("--cards-file", type=Path, default=default_cards_file())
    parser.add_argument("--spi-bus", type=int, default=int(os.environ.get("RFID_SPI_BUS", "0")))
    parser.add_argument("--spi-device", type=int, default=int(os.environ.get("RFID_SPI_DEVICE", "0")))
    parser.add_argument("--gpio-pin-mode", type=int, default=int(os.environ.get("RFID_GPIO_PIN_MODE", "11")))
    parser.add_argument("--rst-pin", type=int, default=int(os.environ.get("RFID_RST_PIN", "25")))
    parser.add_argument("--poll-seconds", type=float, default=float(os.environ.get("RFID_SCAN_POLL_SECONDS", "0.10")))
    parser.add_argument("--refresh-seconds", type=float, default=float(os.environ.get("RFID_SCAN_REFRESH_SECONDS", "1.0")))
    parser.add_argument("--presence-gap-seconds", type=float, default=float(os.environ.get("RFID_SCAN_PRESENCE_GAP_SECONDS", "8.0")))
    parser.add_argument("--release-gap-seconds", type=float, default=float(os.environ.get("RFID_SCAN_RELEASE_GAP_SECONDS", "1.2")))
    parser.add_argument("--deep-hold-seconds", type=float, default=float(os.environ.get("RFID_SCAN_DEEP_HOLD_SECONDS", "2.0")))
    parser.add_argument("--deep-blocks", type=int, default=int(os.environ.get("RFID_SCAN_DEEP_BLOCKS", "64")))
    parser.add_argument("--error-sleep-seconds", type=float, default=float(os.environ.get("RFID_SCAN_ERROR_SLEEP_SECONDS", "5.0")))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--once-timeout", type=float, default=float(os.environ.get("RFID_SCAN_ONCE_TIMEOUT", "5.0")))
    return parser


def main() -> int:
    quiet_mfrc522_logs()
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    args = build_parser().parse_args()
    for name in ("scan_file", "log_file", "event_log", "status_file", "lcd_lock_dir", "sound_hook"):
        value = getattr(args, name)
        if isinstance(value, Path):
            setattr(args, name, value.expanduser())
    if args.cards_file is not None:
        args.cards_file = args.cards_file.expanduser()
    publisher = Publisher(args)
    if args.once:
        return publisher.run_once()
    return publisher.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
