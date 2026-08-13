#!/usr/bin/env python3
"""Record a bounded Jabra clip from an RFID scan and archive it as a WAV file."""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HOME = Path("/home/arthe")
DEFAULT_SECONDS = 30.0
DEFAULT_LEAD_SECONDS = 5.0
DEFAULT_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_DEVICE_MATCH = "jabra"
DEFAULT_USB_FALLBACK_SOURCE_MATCHES = (
    "usb_pnp_sound_device",
    "usb pnp sound device",
    "pcm2902",
    "c-media",
    "c_media",
)
DEFAULT_TRIM_THRESHOLD_DBFS = -45.0
DEFAULT_TRIM_PADDING_SECONDS = 0.2
ROLLING_SERVICE = "gway-jabra-rolling-recorder.service"
ARCHIVE_DIR = HOME / ".local/share/gway-soundfile-archive/record-30s"
STATUS_FILE = HOME / ".local/state/rfid-record-30s/status.json"
EVENT_LOG = HOME / ".local/share/rfid-record-30s/events.ndjson"
SCAN_FILE = HOME / ".local/state/rfid-mode/rfid-scan.json"
MODE_ROOT = HOME / ".config/rfid-modes/cards"
LCD_LOCK_DIR = HOME / ".local/state/lcd-lockfiles"
LCD_EVENT_ID = 8
SAMPLE_WIDTH = 2
LCD_LINE_BYTES = 16
FACTORY_KEY_BYTES = [0xFF] * 6
RFID_SERVICE_UNIT = os.environ.get("RFID_MODE_RFID_SERVICE_UNIT", "")
HARDWARE_PYTHON = HOME / ".local/share/rfid-mode/.venv/bin/python"


class RecordError(RuntimeError):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def local_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def log_event(event: str, **fields: object) -> None:
    payload = {"timestamp": now_iso(), "event": event, **fields}
    try:
        EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        pass


def write_status(payload: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_name(f".{STATUS_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATUS_FILE)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def normalize_card_id(value: object) -> str:
    text = "".join(str(value or "").strip().split()).upper()
    return "".join(ch for ch in text if ch in "0123456789ABCDEF")


def latest_card_id() -> str:
    try:
        payload = json.loads(SCAN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordError(f"Unable to read latest RFID scan from {SCAN_FILE}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecordError(f"Latest RFID scan in {SCAN_FILE} is not a JSON object")
    card_id = normalize_card_id(payload.get("rfid") or payload.get("card_id") or payload.get("uid"))
    if not card_id:
        raise RecordError(f"Latest RFID scan in {SCAN_FILE} does not include a card id")
    return card_id


def resolve_card_id(explicit: str) -> str:
    card_id = normalize_card_id(explicit or os.environ.get("RFID_MODE_CARD_ID"))
    return card_id or latest_card_id()


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


def lcd_event(line1: str, line2: str, *, seconds: int = 5) -> None:
    expires = (now_utc() + timedelta(seconds=seconds)).isoformat()
    try:
        LCD_LOCK_DIR.mkdir(parents=True, exist_ok=True)
        path = LCD_LOCK_DIR / f"lcd-event-{LCD_EVENT_ID}.lck"
        path.write_text(f"{line1[:16]}\n{line2[:16]}\n{expires}\n", encoding="utf-8")
        signal_lcd(LCD_LOCK_DIR)
    except OSError as exc:
        log_event("lcd_failed", error=str(exc), line1=line1, line2=line2)


def progress_bar(elapsed: float, total: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round((elapsed / max(total, 0.1)) * width))))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def record_progress_line(elapsed: float, total: float) -> str:
    elapsed_seconds = max(0, min(999, int(round(elapsed))))
    return f"{elapsed_seconds:03d}s{progress_bar(elapsed, total)}"


def record_lead_in(seconds: float) -> None:
    whole_seconds = max(0, int(round(seconds)))
    for remaining in range(whole_seconds, 0, -1):
        lcd_event(f"RECORD IN {remaining}", "GET READY", seconds=2)
        time.sleep(1.0)
    lcd_event("RECORDING", "STARTED", seconds=3)


def run_text(command: list[str]) -> str:
    process = subprocess.run(command, capture_output=True, text=True, check=False)
    if process.returncode != 0:
        raise RecordError(f"{command[0]} exited {process.returncode}: {process.stderr.strip()}")
    return process.stdout


def pactl_short(kind: str) -> list[tuple[str, str]]:
    if shutil.which("pactl") is None:
        return []
    try:
        text = run_text(["pactl", "list", "short", kind])
    except RecordError:
        return []
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[1], line))
    return rows


def env_match_list(name: str, defaults: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(name, "")
    values = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return values or list(defaults)


def resolve_usb_fallback_source(sources: list[tuple[str, str]]) -> str | None:
    explicit = os.environ.get("RFID_RECORD_30_FALLBACK_SOURCE", "").strip()
    if explicit:
        for name, _line in sources:
            if name == explicit:
                return name
    for needle in env_match_list("RFID_RECORD_30_FALLBACK_DEVICE_MATCH", DEFAULT_USB_FALLBACK_SOURCE_MATCHES):
        for name, line in sources:
            lowered = name.lower()
            if ".monitor" in lowered:
                continue
            if needle in lowered or needle in line.lower():
                return name
    return None


def resolve_sink(explicit: str, match: str) -> str:
    if explicit.strip():
        return explicit.strip()
    needle = (match or DEFAULT_DEVICE_MATCH).strip().lower()
    for name, line in pactl_short("sinks"):
        if needle in name.lower() or needle in line.lower():
            return name
    bluez_outputs = [name for name, _line in pactl_short("sinks") if name.startswith("bluez_output.")]
    if len(bluez_outputs) == 1:
        return bluez_outputs[0]
    if (os.environ.get("RFID_RECORD_30_ALLOW_DEFAULT_AUDIO") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return ""
    raise RecordError("No Jabra playback sink found")


def resolve_source(explicit: str, match: str) -> str:
    if explicit.strip():
        return explicit.strip()
    needle = (match or DEFAULT_DEVICE_MATCH).strip().lower()
    sources = pactl_short("sources")
    for name, line in sources:
        lowered = name.lower()
        if ".monitor" in lowered:
            continue
        if needle in lowered or needle in line.lower():
            return name
    bluez_inputs = [name for name, _line in sources if name.startswith("bluez_input.")]
    if len(bluez_inputs) == 1:
        return bluez_inputs[0]
    fallback = resolve_usb_fallback_source(sources)
    if fallback:
        return fallback
    raise RecordError("No Jabra microphone source found and no USB fallback microphone source is available")


def systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


def user_service_active(unit: str) -> bool:
    return systemctl_user("is-active", "--quiet", unit).returncode == 0


def systemctl_system(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, check=False)


def system_service_active(unit: str) -> bool:
    if not unit:
        return False
    return systemctl_system("is-active", "--quiet", unit).returncode == 0


def set_system_service(action: str, unit: str) -> bool:
    if not unit:
        return True
    result = systemctl_system(action, unit)
    if result.returncode != 0 and shutil.which("sudo"):
        result = subprocess.run(
            ["sudo", "-n", "systemctl", action, unit],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        log_event(
            "systemctl_failed",
            action=action,
            unit=unit,
            returncode=result.returncode,
            stderr=(result.stderr or "").strip(),
        )
        return False
    return True


def capture_pcm(*, source: str, seconds: float, rate: int, channels: int) -> bytes:
    if shutil.which("parecord") is None:
        raise RecordError("parecord is not available")
    bytes_per_second = rate * channels * SAMPLE_WIDTH
    expected_bytes = int(bytes_per_second * seconds)
    command = [
        "parecord",
        "--format=s16le",
        f"--rate={rate}",
        f"--channels={channels}",
        "--raw",
        "--latency-msec=50",
        f"--device={source}",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None:
        raise RecordError("parecord stdout pipe unavailable")
    captured = bytearray()
    started = time.monotonic()
    next_lcd = 0.0
    try:
        while len(captured) < expected_bytes:
            elapsed = min(seconds, time.monotonic() - started)
            if elapsed >= next_lcd:
                lcd_event("RECORDING", record_progress_line(elapsed, seconds), seconds=3)
                next_lcd = elapsed + 1.0
            chunk = process.stdout.read(min(8192, expected_bytes - len(captured)))
            if not chunk:
                if process.poll() is not None:
                    stderr = b""
                    if process.stderr is not None:
                        stderr = process.stderr.read() or b""
                    raise RecordError(
                        f"parecord exited {process.returncode}: {stderr.decode('utf-8', 'replace').strip()}"
                    )
                time.sleep(0.05)
                continue
            captured.extend(chunk)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    lcd_event("RECORD DONE", record_progress_line(seconds, seconds), seconds=3)
    return bytes(captured[:expected_bytes])


def dbfs_to_amplitude(value: float) -> int:
    return max(1, min(32767, int(round(32768.0 * (10.0 ** (value / 20.0))))))


def trim_silence(
    pcm: bytes,
    *,
    rate: int,
    channels: int,
    threshold_dbfs: float,
    padding_seconds: float,
) -> tuple[bytes, dict[str, Any]]:
    pcm = pcm[: len(pcm) - (len(pcm) % SAMPLE_WIDTH)]
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    threshold = dbfs_to_amplitude(threshold_dbfs)
    active = [idx for idx, value in enumerate(samples) if abs(value) >= threshold]
    original_frames = len(samples) // max(channels, 1)
    if not active:
        return pcm, {
            "trimmed": False,
            "trim_reason": "no_audio_above_threshold",
            "threshold_dbfs": threshold_dbfs,
            "threshold_amplitude": threshold,
            "original_duration_seconds": round(original_frames / max(rate, 1), 3),
            "trimmed_duration_seconds": round(original_frames / max(rate, 1), 3),
        }

    padding_samples = int(max(0.0, padding_seconds) * rate * channels)
    start_sample = max(0, active[0] - padding_samples)
    end_sample = min(len(samples), active[-1] + padding_samples + 1)
    start_byte = start_sample * SAMPLE_WIDTH
    end_byte = end_sample * SAMPLE_WIDTH
    trimmed = pcm[start_byte:end_byte]
    trimmed_frames = len(trimmed) // max(SAMPLE_WIDTH * channels, 1)
    return trimmed, {
        "trimmed": True,
        "threshold_dbfs": threshold_dbfs,
        "threshold_amplitude": threshold,
        "trim_start_seconds": round(start_sample / max(rate * channels, 1), 3),
        "trim_end_seconds": round(end_sample / max(rate * channels, 1), 3),
        "original_duration_seconds": round(original_frames / max(rate, 1), 3),
        "trimmed_duration_seconds": round(trimmed_frames / max(rate, 1), 3),
    }


def write_wav(path: Path, pcm: bytes, *, rate: int, channels: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm)


def archive_paths(card_id: str, archive_dir: Path, record_id: str) -> tuple[Path, Path]:
    day_dir = archive_dir / datetime.now().astimezone().strftime("%Y-%m-%d")
    stem = f"record-30s-{local_stamp()}-{card_id[-8:]}-{record_id[:12]}"
    return day_dir / f"{stem}.wav", day_dir / f"{stem}.json"


def latest_path(archive_dir: Path, card_id: str) -> Path:
    return archive_dir / "latest" / f"{card_id}.json"


def index_log_path(archive_dir: Path) -> Path:
    return archive_dir / "records.ndjson"


def write_archive_indexes(payload: dict[str, Any], archive_dir: Path) -> None:
    write_json_atomic(latest_path(archive_dir, str(payload["card_id"])), payload)
    index_path = index_log_path(archive_dir)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def usable_record(payload: dict[str, Any] | None, card_id: str) -> dict[str, Any] | None:
    if not payload or normalize_card_id(payload.get("card_id")) != card_id:
        return None
    wav_path = Path(str(payload.get("wav_path") or ""))
    if not wav_path.is_file():
        return None
    return payload


def find_latest_record(card_id: str, archive_dir: Path) -> dict[str, Any] | None:
    current = usable_record(read_json(latest_path(archive_dir, card_id)), card_id)
    if current is not None:
        return current

    candidates: list[tuple[float, dict[str, Any]]] = []
    for path in archive_dir.glob("*/*.json"):
        payload = usable_record(read_json(path), card_id)
        if payload is None:
            continue
        wav_path = Path(str(payload["wav_path"]))
        try:
            mtime = wav_path.stat().st_mtime
        except OSError:
            mtime = path.stat().st_mtime
        candidates.append((mtime, payload))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def uuid_label(record_id: str) -> str:
    clean = "".join(ch for ch in record_id.upper() if ch in "0123456789ABCDEF")[:32].ljust(32, "0")
    return f"{clean[:16]}\n{clean[16:32]}"


def write_mode_label_file(card_id: str, label: str) -> dict[str, Any]:
    path = MODE_ROOT / card_id / "label.txt"
    try:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(label.rstrip("\n") + "\n", encoding="utf-8")
        tmp.replace(path)
        return {"ok": True, "path": str(path)}
    except OSError as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}


def write_presented_card_label(card_id: str, label: str, *, timeout: float) -> dict[str, Any]:
    if timeout <= 0:
        return {"ok": False, "skipped": True, "reason": "timeout_disabled", "rfid": card_id}

    python = HARDWARE_PYTHON if HARDWARE_PYTHON.exists() else Path(sys.executable)
    helper = r'''
import json
import os
import time

card_id = os.environ["RFID_RECORD_30_WRITE_CARD_ID"]
label = os.environ["RFID_RECORD_30_WRITE_LABEL"]
timeout = float(os.environ.get("RFID_RECORD_30_WRITE_TIMEOUT", "10"))
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
            "RFID_RECORD_30_WRITE_CARD_ID": card_id,
            "RFID_RECORD_30_WRITE_LABEL": label,
            "RFID_RECORD_30_WRITE_TIMEOUT": str(timeout),
        }
    )

    was_active = system_service_active(RFID_SERVICE_UNIT)
    if was_active:
        if not set_system_service("stop", RFID_SERVICE_UNIT):
            return {"ok": False, "error": "rfid_service_stop_failed", "rfid": card_id}
        time.sleep(0.5)
    try:
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
            return {"ok": False, "error": str(exc), "rfid": card_id}
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
            "rfid": card_id,
        }
    finally:
        if was_active:
            set_system_service("start", RFID_SERVICE_UNIT)


def play_audio_file(wav_path: Path, *, sink: str, dry_run: bool) -> list[str]:
    if shutil.which("pw-play"):
        command = ["pw-play", "--latency", "50ms"]
        if sink:
            command.extend(["--target", sink])
        command.append(str(wav_path))
    elif shutil.which("paplay"):
        command = ["paplay"]
        if sink:
            command.append(f"--device={sink}")
        command.append(str(wav_path))
    elif shutil.which("aplay"):
        command = ["aplay", "-q"]
        if sink:
            command.extend(["-D", sink])
        command.append(str(wav_path))
    else:
        raise RecordError("Neither pw-play, paplay, nor aplay is available")
    if dry_run:
        return command
    process = subprocess.run(command, check=False)
    if process.returncode != 0:
        raise RecordError(f"Playback exited with status {process.returncode}")
    return command


def play_latest(args: argparse.Namespace) -> dict[str, Any]:
    card_id = resolve_card_id(args.card)
    record = find_latest_record(card_id, args.archive_dir)
    if record is None:
        payload = {
            "ok": False,
            "action": "play",
            "card_id": card_id,
            "error": "no archived sound bite for card",
            "finished_at": now_iso(),
        }
        lcd_event("NO RECORDING", f"CARD {card_id[-8:]}", seconds=20)
        log_event("play_missing", **payload)
        return payload

    wav_path = Path(str(record["wav_path"]))
    sink = resolve_sink(args.playback_device, args.match)
    duration = float(record.get("trimmed_duration_seconds") or record.get("original_duration_seconds") or 0.0)
    lcd_event("PLAY RECORD", f"CARD {card_id[-8:]}", seconds=max(10, int(round(duration)) + 5))
    command = play_audio_file(wav_path, sink=sink, dry_run=args.dry_run)
    payload = {
        "ok": True,
        "action": "play",
        "card_id": card_id,
        "wav_path": str(wav_path),
        "metadata_path": record.get("metadata_path", ""),
        "record_uuid": record.get("record_uuid", ""),
        "record_id": record.get("record_id", ""),
        "playback_device": sink,
        "playback_command": command,
        "dry_run": bool(args.dry_run),
        "finished_at": now_iso(),
    }
    log_event("play_done" if not args.dry_run else "play_dry_run", **payload)
    return payload


def record_once(args: argparse.Namespace) -> dict[str, Any]:
    card_id = resolve_card_id(args.card)
    seconds = max(1.0, min(float(args.seconds), 300.0))
    rate = int(args.rate)
    channels = int(args.channels)
    if rate < 8000 or channels not in {1, 2}:
        raise RecordError("invalid rate/channels settings")
    source = resolve_source(args.source, args.match)
    rolling_was_active = user_service_active(ROLLING_SERVICE)
    record_uuid = uuid.uuid4()
    record_id = record_uuid.hex.upper()
    label = uuid_label(record_id)
    payload: dict[str, Any] = {
        "ok": False,
        "action": "record",
        "card_id": card_id,
        "record_uuid": str(record_uuid),
        "record_id": record_id,
        "card_label": label,
        "source": source,
        "seconds_requested": seconds,
        "rate": rate,
        "channels": channels,
        "rolling_was_active": rolling_was_active,
        "started_at": now_iso(),
    }
    write_status(payload)
    log_event("record_start", **payload)

    if rolling_was_active:
        lcd_event("RECORD 30s", "PAUSE ROLLING", seconds=3)
        systemctl_user("stop", ROLLING_SERVICE)
        time.sleep(0.5)

    try:
        lead_seconds = max(0.0, min(float(args.lead_seconds), 30.0))
        payload["lead_seconds"] = lead_seconds
        write_status(payload)
        if lead_seconds:
            record_lead_in(lead_seconds)
        else:
            lcd_event("RECORDING", "STARTED", seconds=3)
        pcm = capture_pcm(source=source, seconds=seconds, rate=rate, channels=channels)
        trimmed_pcm, trim_info = trim_silence(
            pcm,
            rate=rate,
            channels=channels,
            threshold_dbfs=args.trim_threshold_dbfs,
            padding_seconds=args.trim_padding_seconds,
        )
        wav_path, meta_path = archive_paths(card_id, args.archive_dir, record_id)
        write_wav(wav_path, trimmed_pcm, rate=rate, channels=channels)
        mode_label = write_mode_label_file(card_id, label)
        card_label_write: dict[str, Any]
        if args.no_card_label_write:
            card_label_write = {"ok": False, "skipped": True, "reason": "disabled"}
        else:
            lcd_event("WRITE UUID", "KEEP CARD", seconds=5)
            card_label_write = write_presented_card_label(card_id, label, timeout=float(args.label_write_timeout))
        payload.update(
            {
                "ok": True,
                "wav_path": str(wav_path),
                "metadata_path": str(meta_path),
                "card_label_file": mode_label,
                "card_label_write": card_label_write,
                "raw_bytes": len(pcm),
                "saved_bytes": wav_path.stat().st_size,
                "finished_at": now_iso(),
                **trim_info,
            }
        )
        meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            write_archive_indexes(payload, args.archive_dir)
        except OSError as exc:
            log_event("index_write_failed", error=str(exc), card_id=card_id, record_id=record_id)
        lcd_event("RECORD SAVED", wav_path.name[:16], seconds=20)
        log_event("record_saved", **payload)
        return payload
    finally:
        if rolling_was_active:
            systemctl_user("start", ROLLING_SERVICE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        choices=("record", "play"),
        default=os.environ.get("RFID_RECORD_30_ACTION", "record"),
    )
    parser.add_argument("--card", default="")
    parser.add_argument("--seconds", type=float, default=float(os.environ.get("RFID_RECORD_30_SECONDS", DEFAULT_SECONDS)))
    parser.add_argument(
        "--lead-seconds",
        type=float,
        default=float(os.environ.get("RFID_RECORD_30_LEAD_SECONDS", DEFAULT_LEAD_SECONDS)),
    )
    parser.add_argument("--source", default=os.environ.get("RFID_RECORD_30_SOURCE", ""))
    parser.add_argument("--playback-device", default=os.environ.get("RFID_RECORD_30_PLAYBACK_DEVICE", ""))
    parser.add_argument("--match", default=os.environ.get("RFID_RECORD_30_DEVICE_MATCH", DEFAULT_DEVICE_MATCH))
    parser.add_argument("--rate", type=int, default=int(os.environ.get("RFID_RECORD_30_RATE", DEFAULT_RATE)))
    parser.add_argument("--channels", type=int, default=int(os.environ.get("RFID_RECORD_30_CHANNELS", DEFAULT_CHANNELS)))
    parser.add_argument(
        "--trim-threshold-dbfs",
        type=float,
        default=float(os.environ.get("RFID_RECORD_30_TRIM_THRESHOLD_DBFS", DEFAULT_TRIM_THRESHOLD_DBFS)),
    )
    parser.add_argument(
        "--trim-padding-seconds",
        type=float,
        default=float(os.environ.get("RFID_RECORD_30_TRIM_PADDING_SECONDS", DEFAULT_TRIM_PADDING_SECONDS)),
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=Path(os.environ.get("RFID_RECORD_30_ARCHIVE_DIR", str(ARCHIVE_DIR))),
    )
    parser.add_argument(
        "--label-write-timeout",
        type=float,
        default=float(os.environ.get("RFID_RECORD_30_LABEL_WRITE_TIMEOUT", "10")),
    )
    parser.add_argument("--no-card-label-write", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Resolve playback without playing audio.")
    parser.add_argument("--status", action="store_true", help="Print the latest status and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.status:
        try:
            print(STATUS_FILE.read_text(encoding="utf-8"), end="")
        except FileNotFoundError:
            print(json.dumps({"ok": False, "error": f"No status file at {STATUS_FILE}"}, indent=2))
        return 0
    try:
        if args.action == "play":
            payload = play_latest(args)
        else:
            payload = record_once(args)
    except Exception as exc:
        payload = {
            "ok": False,
            "action": getattr(args, "action", "record"),
            "error": str(exc),
            "finished_at": now_iso(),
        }
        write_status(payload)
        log_event(f"{payload['action']}_failed", **payload)
        lcd_event("PLAY FAIL" if payload["action"] == "play" else "RECORD FAIL", str(exc)[:16], seconds=20)
        print(f"rfid-record-30s: {exc}", file=sys.stderr)
        return 1
    write_status(payload)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
