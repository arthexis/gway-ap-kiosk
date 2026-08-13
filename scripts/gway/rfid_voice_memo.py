#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

HOME = Path("/home/arthe")
DATA_DIR = HOME / ".local" / "share" / "rfid-voice-memo"
DB_PATH = DATA_DIR / "voice-memos.sqlite3"
EVENT_LOG = DATA_DIR / "events.ndjson"
MODE_ROOT = HOME / ".config" / "rfid-modes" / "cards"
DEFAULT_SCAN_FILES = (
    HOME / ".local" / "state" / "rfid-mode" / "rfid-scan.json",
    HOME / "arthexis" / ".locks" / "rfid-scan.json",
)
DEFAULT_LOCK_DIR = HOME / "arthexis" / ".locks"
DEFAULT_LCD_LOCK_DIR = HOME / ".local" / "state" / "lcd-lockfiles"
DEFAULT_MAX_SECONDS = 30.0
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2
DEFAULT_DEVICE_MATCH = "jabra"
DEFAULT_USB_FALLBACK_SOURCE_MATCHES = (
    "usb_pnp_sound_device",
    "usb pnp sound device",
    "pcm2902",
    "c-media",
    "c_media",
)
LCD_EVENT_ID = 7
DEFAULT_STATUS_SECONDS = 30


class VoiceMemoError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapturedAudio:
    wav_bytes: bytes
    duration_seconds: float
    sample_rate: int
    channels: int
    capture_device: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, **fields: object) -> None:
    payload = {"timestamp": now_iso(), "event": event, **fields}
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        pass


def normalize_card_id(value: str | None) -> str:
    text = "".join(str(value or "").strip().split()).upper()
    if not text:
        return ""
    if not all(ch in "0123456789ABCDEF" for ch in text):
        raise VoiceMemoError(f"Invalid RFID card id: {value!r}")
    return text


def scan_file_path() -> Path:
    explicit = os.environ.get("RFID_MODE_SCAN_FILE")
    if explicit:
        return Path(explicit).expanduser()
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


def latest_card_id() -> str:
    env_card = normalize_card_id(os.environ.get("RFID_MODE_CARD_ID"))
    if env_card:
        return env_card
    path = scan_file_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VoiceMemoError(f"Unable to read latest RFID scan from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VoiceMemoError(f"Latest RFID scan in {path} is not a JSON object")
    for key in ("rfid", "card_id", "uid"):
        card_id = normalize_card_id(payload.get(key))
        if card_id:
            return card_id
    raise VoiceMemoError(f"Latest RFID scan in {path} does not include a card id")


def resolve_card_id(args: argparse.Namespace) -> str:
    card_id = normalize_card_id(getattr(args, "card", "") or "")
    if card_id:
        return card_id
    return latest_card_id()


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_memos (
            card_id TEXT PRIMARY KEY,
            audio_data BLOB NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'audio/wav',
            duration_seconds REAL NOT NULL,
            sample_rate INTEGER NOT NULL,
            channels INTEGER NOT NULL,
            byte_size INTEGER NOT NULL,
            capture_device TEXT NOT NULL DEFAULT '',
            playback_device TEXT NOT NULL DEFAULT '',
            recorded_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT NOT NULL,
            action TEXT NOT NULL,
            ok INTEGER NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    return conn


def record_event_row(card_id: str, action: str, ok: bool, detail: str = "") -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO events(card_id, action, ok, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (card_id, action, 1 if ok else 0, detail[:500], now_iso()),
            )
    except sqlite3.Error:
        pass


def pactl_short(kind: str) -> list[tuple[str, str]]:
    if shutil.which("pactl") is None:
        return []
    process = subprocess.run(
        ["pactl", "list", "short", kind],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        return []
    rows: list[tuple[str, str]] = []
    for line in process.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[1], line))
    return rows


def pactl_records(kind: str) -> list[tuple[str, str]]:
    if shutil.which("pactl") is None:
        return []
    process = subprocess.run(
        ["pactl", "list", kind],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        return []
    records: list[tuple[str, str]] = []
    current: list[str] = []
    for line in process.stdout.splitlines():
        if line.strip():
            current.append(line)
            continue
        if current:
            name = pactl_record_name(current)
            if name:
                records.append((name, "\n".join(current)))
            current = []
    if current:
        name = pactl_record_name(current)
        if name:
            records.append((name, "\n".join(current)))
    return records


def pactl_record_name(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Name: "):
            return stripped.removeprefix("Name: ").strip()
    return ""


def list_devices() -> dict[str, list[str]]:
    return {
        "sources": [name for name, _line in pactl_short("sources")],
        "sinks": [name for name, _line in pactl_short("sinks")],
    }


def device_match_text() -> str:
    return (os.environ.get("RFID_VOICE_MEMO_DEVICE_MATCH") or DEFAULT_DEVICE_MATCH).strip().lower()


def env_match_list(name: str, defaults: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(name, "")
    values = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return values or list(defaults)


def allow_default_audio() -> bool:
    return (os.environ.get("RFID_VOICE_MEMO_ALLOW_DEFAULT_AUDIO") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def record_has_match(name: str, record: str, match: str) -> bool:
    if not match:
        return False
    return match in name.lower() or match in record.lower()


def record_has_profile(record: str, profile: str) -> bool:
    needle = f"{profile}:"
    return any(line.strip().startswith(needle) for line in record.splitlines())


def record_active_profile(record: str) -> str:
    for line in record.splitlines():
        stripped = line.strip()
        if stripped.startswith("Active Profile: "):
            return stripped.removeprefix("Active Profile: ").strip()
    return ""


def ensure_capture_profile() -> None:
    match = device_match_text()
    for name, record in pactl_records("cards"):
        if not record_has_match(name, record, match):
            continue
        for profile in ("headset-head-unit", "headset-head-unit-cvsd"):
            if not record_has_profile(record, profile):
                continue
            if record_active_profile(record) == profile:
                return
            process = subprocess.run(["pactl", "set-card-profile", name, profile], check=False)
            if process.returncode == 0:
                time.sleep(1.0)
                return


def resolve_usb_fallback_capture_device(records: list[tuple[str, str]], rows: list[tuple[str, str]]) -> str | None:
    explicit = os.environ.get("RFID_VOICE_MEMO_FALLBACK_CAPTURE_DEVICE", "").strip()
    if explicit:
        for name, _record in records:
            if name == explicit:
                return name
        for name, _line in rows:
            if name == explicit:
                return name
    for needle in env_match_list("RFID_VOICE_MEMO_FALLBACK_CAPTURE_DEVICE_MATCH", DEFAULT_USB_FALLBACK_SOURCE_MATCHES):
        for name, record in records:
            lowered_name = name.lower()
            if ".monitor" in lowered_name:
                continue
            if needle in lowered_name or needle in record.lower():
                return name
        for name, line in rows:
            lowered_name = name.lower()
            if ".monitor" in lowered_name:
                continue
            if needle in lowered_name or needle in line.lower():
                return name
    return None


def resolve_pulse_device(kind: str, explicit: str | None, env_name: str) -> str:
    value = (explicit or os.environ.get(env_name) or "").strip()
    if value:
        return value
    match = device_match_text()
    records = pactl_records(kind)
    for name, record in records:
        lowered_name = name.lower()
        lowered_record = record.lower()
        if kind == "sources" and ".monitor" in lowered_name:
            continue
        if match and (match in lowered_name or match in lowered_record):
            return name
    rows = pactl_short(kind)
    for name, line in rows:
        lowered_name = name.lower()
        lowered_line = line.lower()
        if kind == "sources" and ".monitor" in lowered_name:
            continue
        if match and match in lowered_line:
            return name
    if kind == "sources":
        fallback = resolve_usb_fallback_capture_device(records, rows)
        if fallback:
            return fallback
    if allow_default_audio():
        return ""
    label = "capture source" if kind == "sources" else "playback sink"
    raise VoiceMemoError(
        f"No Jabra Bluetooth {label} found. Pair/connect the Jabra device or set "
        f"{env_name} to the exact PulseAudio/PipeWire device name."
    )


def signal_lcd(lock_dir: Path) -> None:
    pid_path = lock_dir / "lcd.pid"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return
    if pid > 1:
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            pass


def lcd_lock_dir() -> Path:
    for env_name in ("RFID_MODE_LCD_LOCK_DIR", "LCD_LOCK_DIR"):
        env_value = os.environ.get(env_name)
        if env_value:
            return Path(env_value).expanduser()

    lock_dirs = os.environ.get("LCD_LOCK_DIRS", "")
    for raw in lock_dirs.split(":"):
        if raw.strip():
            return Path(raw.strip()).expanduser()

    if DEFAULT_LCD_LOCK_DIR.exists():
        return DEFAULT_LCD_LOCK_DIR
    return Path(os.environ.get("RFID_MODE_LOCK_DIR") or DEFAULT_LOCK_DIR).expanduser()


def lcd_event(line1: str, line2: str, *, seconds: int = 5) -> None:
    lock_dir = lcd_lock_dir()
    event_file = lock_dir / f"lcd-event-{LCD_EVENT_ID}.lck"
    expires = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        event_file.write_text(f"{line1[:16]}\n{line2[:16]}\n{expires}\n", encoding="utf-8")
        signal_lcd(lock_dir)
    except OSError as exc:
        log_event("lcd_write_failed", error=str(exc), line1=line1, line2=line2)


def progress_bar(elapsed: float, total: float, width: int = 10) -> str:
    total = max(total, 0.1)
    filled = max(0, min(width, int(round((elapsed / total) * width))))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def wav_bytes_from_pcm(pcm: bytes, *, sample_rate: int, channels: int) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(DEFAULT_SAMPLE_WIDTH)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


def run_recorder(command: list[str], output_path: Path, *, max_seconds: float, progress: Callable[[float], None]) -> None:
    process = subprocess.Popen(command)
    start = time.monotonic()
    try:
        while True:
            elapsed = min(max_seconds, time.monotonic() - start)
            progress(elapsed)
            if elapsed >= max_seconds or process.poll() is not None:
                break
            time.sleep(0.5)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    progress(max_seconds)
    if process.returncode not in (0, -signal.SIGTERM):
        raise VoiceMemoError(f"Recorder exited with status {process.returncode}")
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise VoiceMemoError("Recorder produced no audio samples")


def capture_audio(*, seconds: float, capture_device: str | None, card_id: str) -> CapturedAudio:
    max_seconds = max(1.0, min(float(seconds), DEFAULT_MAX_SECONDS))
    sample_rate = int(os.environ.get("RFID_VOICE_MEMO_SAMPLE_RATE") or DEFAULT_SAMPLE_RATE)
    channels = int(os.environ.get("RFID_VOICE_MEMO_CHANNELS") or DEFAULT_CHANNELS)
    ensure_capture_profile()
    source = resolve_pulse_device("sources", capture_device, "RFID_VOICE_MEMO_CAPTURE_DEVICE")
    with tempfile.TemporaryDirectory(prefix="rfid-voice-memo-") as tmp:
        raw_path = Path(tmp) / "capture.raw"
        if shutil.which("parecord"):
            command = [
                "parecord",
                "--format=s16le",
                f"--rate={sample_rate}",
                f"--channels={channels}",
                "--file-format=raw",
                "--latency-msec=50",
            ]
            if source:
                command.append(f"--device={source}")
            command.append(str(raw_path))
        elif shutil.which("arecord"):
            command = ["arecord", "-q", "-f", "S16_LE", "-r", str(sample_rate), "-c", str(channels), "-t", "raw"]
            if source:
                command.extend(["-D", source])
            command.append(str(raw_path))
        else:
            raise VoiceMemoError("Neither parecord nor arecord is available")

        def update_lcd(elapsed: float) -> None:
            lcd_event("VOICE MEMO", f"REC {progress_bar(elapsed, max_seconds)}", seconds=3)

        lcd_event("VOICE MEMO", f"CARD {card_id[-8:]}", seconds=3)
        run_recorder(command, raw_path, max_seconds=max_seconds, progress=update_lcd)
        pcm = raw_path.read_bytes()
    wav_bytes = wav_bytes_from_pcm(pcm, sample_rate=sample_rate, channels=channels)
    sample_count = len(pcm) / max(DEFAULT_SAMPLE_WIDTH * channels, 1)
    duration = min(max_seconds, sample_count / sample_rate)
    return CapturedAudio(wav_bytes, duration, sample_rate, channels, source)


def store_memo(card_id: str, audio: CapturedAudio) -> None:
    timestamp = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO voice_memos(
                card_id, audio_data, content_type, duration_seconds, sample_rate,
                channels, byte_size, capture_device, recorded_at, updated_at
            ) VALUES (?, ?, 'audio/wav', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(card_id) DO UPDATE SET
                audio_data=excluded.audio_data,
                content_type=excluded.content_type,
                duration_seconds=excluded.duration_seconds,
                sample_rate=excluded.sample_rate,
                channels=excluded.channels,
                byte_size=excluded.byte_size,
                capture_device=excluded.capture_device,
                recorded_at=excluded.recorded_at,
                updated_at=excluded.updated_at
            """,
            (
                card_id,
                sqlite3.Binary(audio.wav_bytes),
                audio.duration_seconds,
                audio.sample_rate,
                audio.channels,
                len(audio.wav_bytes),
                audio.capture_device,
                timestamp,
                timestamp,
            ),
        )


def load_memo(card_id: str) -> dict[str, Any] | None:
    with db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM voice_memos WHERE card_id = ?", (card_id,)).fetchone()
    return dict(row) if row is not None else None


def play_audio(wav_bytes: bytes, *, playback_device: str | None) -> str:
    sink = resolve_pulse_device("sinks", playback_device, "RFID_VOICE_MEMO_PLAYBACK_DEVICE")
    with tempfile.TemporaryDirectory(prefix="rfid-voice-memo-") as tmp:
        wav_path = Path(tmp) / "memo.wav"
        wav_path.write_bytes(wav_bytes)
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
            raise VoiceMemoError("Neither paplay nor aplay is available")
        process = subprocess.run(command, check=False)
    if process.returncode != 0:
        raise VoiceMemoError(f"Playback exited with status {process.returncode}")
    return sink


def command_record(args: argparse.Namespace) -> int:
    card_id = resolve_card_id(args)
    try:
        audio = capture_audio(seconds=args.seconds, capture_device=args.capture_device, card_id=card_id)
        store_memo(card_id, audio)
    except Exception as exc:
        lcd_event("VOICE MEMO", "RECORD FAIL", seconds=DEFAULT_STATUS_SECONDS)
        record_event_row(card_id, "record", False, str(exc))
        raise
    lcd_event("VOICE MEMO", "SAVED", seconds=DEFAULT_STATUS_SECONDS)
    record_event_row(card_id, "record", True, f"{audio.duration_seconds:.2f}s")
    print(json.dumps({"ok": True, "card_id": card_id, "duration_seconds": round(audio.duration_seconds, 3), "bytes": len(audio.wav_bytes), "capture_device": audio.capture_device}, sort_keys=True))
    return 0


def command_play(args: argparse.Namespace) -> int:
    card_id = resolve_card_id(args)
    memo = load_memo(card_id)
    if memo is None:
        lcd_event("VOICE MEMO", "NO MEMO", seconds=DEFAULT_STATUS_SECONDS)
        record_event_row(card_id, "play", False, "no memo")
        print(json.dumps({"ok": False, "card_id": card_id, "error": "no memo"}, sort_keys=True))
        return 2
    try:
        memo_duration = float(memo["duration_seconds"])
        lcd_event("VOICE MEMO", "PLAYING", seconds=max(DEFAULT_STATUS_SECONDS, int(round(memo_duration)) + 10))
        sink = play_audio(bytes(memo["audio_data"]), playback_device=args.playback_device)
        with db() as conn:
            conn.execute("UPDATE voice_memos SET playback_device = ?, updated_at = ? WHERE card_id = ?", (sink, now_iso(), card_id))
    except Exception as exc:
        lcd_event("VOICE MEMO", "PLAY FAIL", seconds=DEFAULT_STATUS_SECONDS)
        record_event_row(card_id, "play", False, str(exc))
        raise
    record_event_row(card_id, "play", True, sink)
    print(json.dumps({"ok": True, "card_id": card_id, "duration_seconds": round(float(memo["duration_seconds"]), 3), "playback_device": sink}, sort_keys=True))
    return 0


def sidecar_script(action: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "card_id=\"${RFID_MODE_CARD_ID:-}\"\n"
        "if [ -z \"$card_id\" ]; then\n"
        "  echo \"RFID_MODE_CARD_ID is required\" >&2\n"
        "  exit 2\n"
        "fi\n"
        f"exec /home/arthe/.local/bin/rfid-voice-memo {action} --card \"$card_id\"\n"
    )


def command_bind(args: argparse.Namespace) -> int:
    card_id = resolve_card_id(args)
    card_dir = MODE_ROOT / card_id
    quick_script = MODE_ROOT / f"{card_id}.sh"
    hold_script = card_dir / "hold.sh"
    existing = [path for path in (card_dir, quick_script, hold_script) if path.exists()]
    if existing and not args.force:
        raise VoiceMemoError(
            "Refusing to overwrite existing RFID mode files without --force: "
            + ", ".join(str(path) for path in existing)
        )
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "card_id": card_id, "card_dir": str(card_dir), "quick_script": str(quick_script), "hold_script": str(hold_script)}, sort_keys=True))
        return 0
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "label.txt").write_text("VOICE MEMO\nHOLD TO RECORD\n", encoding="utf-8")
    quick_script.write_text(sidecar_script("play"), encoding="utf-8")
    hold_script.write_text(sidecar_script("record"), encoding="utf-8")
    quick_script.chmod(0o755)
    hold_script.chmod(0o755)
    log_event("bind", card_id=card_id, quick_script=str(quick_script), hold_script=str(hold_script))
    print(json.dumps({"ok": True, "card_id": card_id, "card_dir": str(card_dir), "quick_script": str(quick_script), "hold_script": str(hold_script)}, sort_keys=True))
    return 0


def command_status(args: argparse.Namespace) -> int:
    card_id = resolve_card_id(args) if (args.card or args.latest) else ""
    payload: dict[str, Any] = {
        "database": str(DB_PATH),
        "mode_root": str(MODE_ROOT),
        "scan_file": str(scan_file_path()),
        "devices": list_devices(),
    }
    if card_id:
        memo = load_memo(card_id)
        payload["card_id"] = card_id
        payload["bound"] = (MODE_ROOT / card_id).is_dir()
        payload["memo"] = None if memo is None else {
            "duration_seconds": memo["duration_seconds"],
            "byte_size": memo["byte_size"],
            "capture_device": memo["capture_device"],
            "playback_device": memo["playback_device"],
            "recorded_at": memo["recorded_at"],
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_arm_next(args: argparse.Namespace) -> int:
    helper = HOME / ".local" / "bin" / "rfid-voice-memo-arm-next"
    helper_command = [str(helper)]
    if args.timeout is not None:
        helper_command.extend(["--timeout", str(args.timeout)])
    if args.force:
        helper_command.append("--force")
    if args.foreground:
        return subprocess.run(helper_command, check=False).returncode

    unit_name = "rfid-voice-memo-arm-next"
    subprocess.run(["systemctl", "--user", "stop", f"{unit_name}.service"], capture_output=True, text=True, check=False)
    command = ["systemd-run", "--user", f"--unit={unit_name}", "--collect", *helper_command]
    process = subprocess.run(command, capture_output=True, text=True, check=False)
    if process.returncode != 0:
        raise VoiceMemoError((process.stderr or process.stdout or "Unable to arm next-card service").strip())
    lcd_event("VOICE MEMO", "SCAN NEXT", seconds=60)
    print(json.dumps({"ok": True, "unit": f"{unit_name}.service", "waits_forever": args.timeout is None}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local RFID voice memo recorder/player for gway-001.")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="Record up to 30 seconds for a card.")
    record.add_argument("--card", help="RFID card id. Defaults to RFID_MODE_CARD_ID/latest scan.")
    record.add_argument("--seconds", type=float, default=DEFAULT_MAX_SECONDS)
    record.add_argument("--capture-device", help="Explicit PulseAudio/PipeWire source.")
    record.set_defaults(func=command_record)

    play = sub.add_parser("play", help="Play the memo for a card.")
    play.add_argument("--card", help="RFID card id. Defaults to RFID_MODE_CARD_ID/latest scan.")
    play.add_argument("--playback-device", help="Explicit PulseAudio/PipeWire sink.")
    play.set_defaults(func=command_play)

    bind = sub.add_parser("bind", help="Bind a card in rfid-mode-runner: quick scan plays, deep scan records.")
    target = bind.add_mutually_exclusive_group(required=True)
    target.add_argument("--card", help="RFID card id to bind.")
    target.add_argument("--latest", action="store_true", help="Bind the latest scanned card.")
    bind.add_argument("--force", action="store_true", help="Overwrite existing local RFID mode files for this card.")
    bind.add_argument("--dry-run", action="store_true")
    bind.set_defaults(func=command_bind)

    devices = sub.add_parser("devices", help="List visible PulseAudio/PipeWire devices.")
    devices.set_defaults(func=lambda args: print(json.dumps(list_devices(), indent=2, sort_keys=True)) or 0)

    status = sub.add_parser("status", help="Show local voice memo state.")
    status.add_argument("--card", help="RFID card id to inspect.")
    status.add_argument("--latest", action="store_true", help="Inspect latest scanned card.")
    status.set_defaults(func=command_status)

    arm = sub.add_parser("arm-next", help="Bind the next distinct scanned RFID card to voice memo mode.")
    arm.add_argument("--timeout", type=float, help="Seconds to wait. Omit to wait forever.")
    arm.add_argument("--force", action="store_true", help="Allow replacing existing voice memo mode files for the scanned card.")
    arm.add_argument("--foreground", action="store_true", help="Wait in this terminal instead of starting a user service.")
    arm.set_defaults(func=command_arm_next)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except VoiceMemoError as exc:
        print(f"rfid-voice-memo: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("rfid-voice-memo: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
