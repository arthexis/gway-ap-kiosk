from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gway" / "rfid_scan_publisher.py"


def load_module():
    spec = importlib.util.spec_from_file_location("rfid_scan_publisher", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def args(tmp_path: Path, cards_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        scan_file=tmp_path / "rfid-scan.json",
        queue_dir=tmp_path / "queue",
        log_file=tmp_path / "rfid-scans.ndjson",
        event_log=tmp_path / "events.ndjson",
        status_file=tmp_path / "status.json",
        lcd_lock_dir=tmp_path / "lcd",
        lcd_event_id=4,
        lcd_event_seconds=10,
        mode_cards_dir=cards_dir,
        sound_hook=tmp_path / "missing-sound-hook",
        normal_sound="notice",
        deep_sound="ok",
        queued_sound="ok",
        deep_progress_sound="attention",
        cards_file=None,
        spi_bus=0,
        spi_device=0,
        gpio_pin_mode=11,
        rst_pin=25,
        poll_seconds=0.1,
        refresh_seconds=1.0,
        presence_gap_seconds=8.0,
        release_gap_seconds=1.2,
        deep_hold_seconds=0.0,
        deep_blocks=64,
        error_sleep_seconds=5.0,
        once=False,
        once_timeout=5.0,
    )


class FakeReader:
    def __init__(self, deep_payload: dict[str, object]) -> None:
        self.deep_payload = deep_payload

    def read_deep(self, uid: list[int], *, max_blocks: int, progress=None) -> dict[str, object]:
        if progress is not None:
            progress()
        return self.deep_payload

    def close_crypto(self) -> None:
        return None


def event_lines(tmp_path: Path) -> list[str]:
    return (tmp_path / "lcd" / "lcd-event-4.lck").read_text(encoding="utf-8").splitlines()


def test_registered_mode_label_prefers_card_label(tmp_path):
    module = load_module()
    cards_dir = tmp_path / "cards"
    card_dir = cards_dir / "ABCDEF1234"
    card_dir.mkdir(parents=True)
    (card_dir / "label.txt").write_text("AUTO EXAM\nETH0\n", encoding="utf-8")
    (cards_dir / "ABCDEF1234.sh").write_text("# RFID mode: fallback script\n", encoding="utf-8")

    assert module.registered_mode_label("ABCDEF1234", cards_dir) == "AUTO EXAM"


def test_registered_mode_label_uses_script_comment(tmp_path):
    module = load_module()
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    (cards_dir / "ABCDEF1234.sh").write_text("# RFID mode: WIFI + BLUETOOTH SNIFF\n", encoding="utf-8")

    assert module.registered_mode_label("ABCDEF1234", cards_dir) == "WIFI + BLUETOOTH SNIFF"


def test_registered_mode_label_reports_unlinked(tmp_path):
    module = load_module()

    assert module.registered_mode_label("ABCDEF1234", tmp_path / "missing") == "UNLINKED"


def test_start_presence_lcd_shows_rfid_and_registered_label(tmp_path):
    module = load_module()
    cards_dir = tmp_path / "cards"
    card_dir = cards_dir / "ABCDEF1234"
    card_dir.mkdir(parents=True)
    (card_dir / "label.txt").write_text("AUTO EXAM\n", encoding="utf-8")
    (card_dir / "new.sh").write_text("# RFID_MODE_QUEUE_DEEP_DATA=1\n", encoding="utf-8")
    publisher = module.Publisher(args(tmp_path, cards_dir))
    observation = module.ScanObservation(rfid="ABCDEF1234", uid=[0xAB, 0xCD, 0xEF, 0x12, 0x34])

    publisher.start_presence(observation, module.now_iso(), time.monotonic())

    lines = event_lines(tmp_path)
    assert lines[0] == "RFID ABCDEF1234"
    assert lines[1] == "AUTO EXAM"


def test_start_presence_lcd_shows_unlinked_for_unknown_card(tmp_path):
    module = load_module()
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    publisher = module.Publisher(args(tmp_path, cards_dir))
    observation = module.ScanObservation(rfid="ABCDEF1234", uid=[0xAB, 0xCD, 0xEF, 0x12, 0x34])

    publisher.start_presence(observation, module.now_iso(), time.monotonic())

    lines = event_lines(tmp_path)
    assert lines[0] == "RFID ABCDEF1234"
    assert lines[1] == "UNLINKED"


def test_deep_read_keeps_initial_lcd_and_queues_when_done(tmp_path):
    module = load_module()
    cards_dir = tmp_path / "cards"
    card_dir = cards_dir / "ABCDEF1234"
    card_dir.mkdir(parents=True)
    (card_dir / "label.txt").write_text("AUTO EXAM\n", encoding="utf-8")
    (card_dir / "new.sh").write_text("# RFID_MODE_QUEUE_DEEP_DATA=1\n", encoding="utf-8")
    publisher = module.Publisher(args(tmp_path, cards_dir))
    publisher.reader = FakeReader(
        {
            "deep_read": True,
            "deep_scan": {"status": "ok"},
            "dump": [{"block": 1, "data": [], "key": "A"}],
            "keys": {},
        }
    )
    observation = module.ScanObservation(rfid="ABCDEF1234", uid=[0xAB, 0xCD, 0xEF, 0x12, 0x34])
    observed_at = module.now_iso()
    observed_monotonic = time.monotonic()
    publisher.start_presence(observation, observed_at, observed_monotonic)

    publisher.maybe_deep_read(observation, module.now_iso(), observed_monotonic + 1)

    assert not (tmp_path / "lcd" / "lcd-event-4.lck").exists()
    queued = sorted((tmp_path / "queue" / "pending").glob("*.json"))
    assert len(queued) == 1
    record = json.loads(queued[0].read_text(encoding="utf-8"))
    assert record["scan"]["queue_trigger"] == "deep_complete"


def test_release_clears_scan_lcd_event(tmp_path):
    module = load_module()
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    publisher = module.Publisher(args(tmp_path, cards_dir))
    observation = module.ScanObservation(rfid="ABCDEF1234", uid=[0xAB, 0xCD, 0xEF, 0x12, 0x34])
    observed_at = module.now_iso()
    observed_monotonic = time.monotonic()
    publisher.start_presence(observation, observed_at, observed_monotonic)

    publisher.maybe_release(module.now_iso(), observed_monotonic + 2)

    assert not (tmp_path / "lcd" / "lcd-event-4.lck").exists()
    queued = sorted((tmp_path / "queue" / "pending").glob("*.json"))
    assert len(queued) == 1
    assert json.loads(queued[0].read_text(encoding="utf-8"))["scan"]["queue_trigger"] == "released"


def test_card_queue_policy_recognizes_interrupt_sidecar(tmp_path):
    module = load_module()
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "MUTE.sh").write_text(
        "#!/usr/bin/env bash\n# RFID_MODE_QUEUE_INTERRUPT=1\nexit 0\n",
        encoding="utf-8",
    )

    assert module.card_queue_policy("MUTE", cards) == (False, True)
