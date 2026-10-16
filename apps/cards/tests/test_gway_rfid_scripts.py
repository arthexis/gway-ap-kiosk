from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from scripts.gway import rfid_etron_register_service as etron
from scripts.gway import rfid_mode_register as mode_register
from scripts.gway import rfid_mode_reset_root_if_current as reset_root
from scripts.gway import rfid_mode_runner as mode_runner
from scripts.gway import rfid_record_30s as record_30s
from scripts.gway import rfid_scan_publisher as publisher


def test_rfid_card_id_and_label_normalization():
    assert mode_register.normalize_card_id("ab:cd 12") == "ABCD12"
    assert publisher.normalize_card_id("ab:cd 12!!") == "ABCD12_"
    assert record_30s.normalize_card_id("fa:ce-feed:00") == "FACEFEED00"
    assert mode_runner.normalize_card_label("Line one\nLine two\nignored") == "Line one\nLine two"
    assert mode_runner.normalize_trait_env_name("door open?") == "RFID_DOOR_OPEN"


def test_mode_register_uses_sidecar_for_existing_directory(tmp_path):
    root = tmp_path / "cards"
    first = root / "FIRST_CARD"
    first.mkdir(parents=True)

    assert mode_register.validate_script_target(root, ["FIRST_CARD"], False, ".sh") == root / "FIRST_CARD.sh"
    with pytest.raises(SystemExit, match="top-level scripts are not allowed"):
        mode_register.validate_script_target(root, ["NEW_CARD"], False, ".sh")


def test_mode_register_writes_queue_policy_directives(tmp_path):
    target = tmp_path / "CARD.sh"

    mode_register.write_command_script(
        target,
        "echo ok",
        "QUEUE ACTION",
        directives=["RFID_MODE_QUEUE_INTERRUPT=1", "RFID_MODE_QUEUE_CONSUME_NEXT_SECONDS=45"],
        clear_queue=True,
    )

    text = target.read_text(encoding="utf-8")
    assert "RFID_MODE_QUEUE_INTERRUPT=1" in text
    assert "RFID_MODE_QUEUE_CONSUME_NEXT_SECONDS=45" in text
    assert "--request-queue-clear" in text


def test_scan_publisher_normalizes_payload_and_redacts_keys_from_log():
    payload = publisher.normalize_payload({"rfid": "ab:cd ef", "keys": ["secret"]})
    logged = publisher.log_payload(payload)

    assert payload["schema"] == publisher.SCHEMA
    assert payload["rfid"] == "ABCDEF"
    assert payload["name_key"] == "abcdef"
    assert payload["generated_label"] == "RFID ABCDEF"
    assert "keys" not in logged


def test_etron_registration_matches_reversed_and_prefix_codes(tmp_path):
    cards = tmp_path / "cards.txt"
    cards.write_text("01020304, Sample\nAABBCCDD99, Existing\n", encoding="utf-8")

    assert etron.reverse_uid("01020304") == "04030201"
    assert etron.load_card_codes(cards) == ["01020304", "AABBCCDD99"]
    assert etron.matches_existing_card("04030201", ["01020304"])
    assert etron.matches_existing_card("AABBCCDD00", ["AABBCCDD99"])
    assert not etron.matches_existing_card("11223344", ["AABBCCDD99"])


def test_reset_root_only_when_current_state_matches(tmp_path):
    config_dir = tmp_path / "rfid-modes"
    state_file = config_dir / "state" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "current_card": "TEST_CARD",
                "current_script": "/tmp/test.sh",
                "last_scan_card": "TEST_CARD",
                "state_path": ["TEST_CARD"],
            }
        ),
        encoding="utf-8",
    )

    assert reset_root.main(["--config-dir", str(config_dir), "--card", "TEST_CARD", "--script", "/tmp/test.sh"]) == 0

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["state_path"] == []
    assert state["current_card"] is None
    assert state["one_shot_reset_card"] == "TEST_CARD"


def test_bluetooth_keyboard_binding_dry_run_uses_placeholders(tmp_path):
    env = {
        **os.environ,
        "RFID_MODE_CONFIG_DIR": str(tmp_path / "rfid-modes"),
        "RFID_REGISTER_BIN": "/bin/true",
        "RFID_BT_KEYBOARD_PAIR_BIN": "/bin/true",
    }

    result = subprocess.run(
        ["scripts/gway/rfid-bind-bluetooth-keyboard-pair", "--dry-run", "--card", "test_card"],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "TEST_CARD" in result.stdout


def test_preempt_action_can_continue_or_handle_before_mode_switch(tmp_path):
    config_dir = tmp_path / "rfid-modes"
    runner = mode_runner.ModeRunner(
        scan_file=tmp_path / "scan.json",
        config_dir=config_dir,
        poll_seconds=0.1,
        stop_timeout=0.1,
    )
    runner.setup()
    script = config_dir / "cards" / "AUTO.sh"
    script.write_text(
        "#!/usr/bin/env bash\n# RFID_MODE_PREEMPT_ACTION=1\nexit 75\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    scan = mode_runner.Scan(card_id="AUTO", scanned_at="now", key="AUTO|now", payload={})

    assert runner.run_preempt_action(scan, script, config_dir / "cards", []) == "continue"

    script.write_text(
        "#!/usr/bin/env bash\n# RFID_MODE_PREEMPT_ACTION=1\nexit 0\n",
        encoding="utf-8",
    )
    assert runner.run_preempt_action(scan, script, config_dir / "cards", []) == "handled"
    assert runner.state["last_scan_card"] == "AUTO"


def test_top_level_sidecar_defaults_to_one_shot_action(tmp_path):
    config_dir = tmp_path / "rfid-modes"
    runner = mode_runner.ModeRunner(
        scan_file=tmp_path / "scan.json",
        config_dir=config_dir,
        poll_seconds=0.1,
        stop_timeout=0.1,
        dry_run=True,
    )
    runner.setup()
    card_dir = config_dir / "cards" / "MUTE"
    card_dir.mkdir()
    script = config_dir / "cards" / "MUTE.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    scan = mode_runner.Scan(card_id="MUTE", scanned_at="now", key="MUTE|now", payload={})

    assert runner.resolve_transition(config_dir / "cards", "MUTE").action == "execute_top_level_action"

    runner.handle_scan(scan)

    assert runner.state["current_card"] == "MUTE"
    assert runner.state["current_mode_kind"] == "action"
    assert runner.state["state_path"] == []


def test_top_level_sidecar_can_opt_into_stateful_mode(tmp_path):
    config_dir = tmp_path / "rfid-modes"
    runner = mode_runner.ModeRunner(
        scan_file=tmp_path / "scan.json",
        config_dir=config_dir,
        poll_seconds=0.1,
        stop_timeout=0.1,
    )
    runner.setup()
    card_dir = config_dir / "cards" / "MODE"
    card_dir.mkdir()
    script = config_dir / "cards" / "MODE.sh"
    script.write_text("#!/usr/bin/env bash\n# RFID_MODE_STATEFUL=1\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    resolution = runner.resolve_transition(config_dir / "cards", "MODE")

    assert resolution.action == "execute_and_navigate"
    assert resolution.next_path == card_dir


def test_stopping_current_mode_releases_running_lcd_event(monkeypatch, tmp_path):
    config_dir = tmp_path / "rfid-modes"
    lcd_dir = tmp_path / "lcd"
    lcd_dir.mkdir()
    monkeypatch.setattr(mode_runner, "DEFAULT_LCD_LOCK_DIR", lcd_dir)

    runner = mode_runner.ModeRunner(
        scan_file=tmp_path / "scan.json",
        config_dir=config_dir,
        poll_seconds=0.1,
        stop_timeout=0.5,
    )
    runner.setup()
    script = config_dir / "cards" / "EXAMINE.sh"
    script.write_text("#!/usr/bin/env bash\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    process = subprocess.Popen([str(script)], start_new_session=True)
    runner.current_proc = process
    runner.write_state(
        current_card="EXAMINE",
        current_script=str(script),
        current_mode_kind="mode",
        current_presence_id="EXAMINE|now",
        pid=process.pid,
        mode_started_at=mode_runner.now_iso(),
        mode_log=str(tmp_path / "mode.log"),
    )
    running_event = lcd_dir / "lcd-event-5.lck"
    running_event.write_text("EXAMINE\nEVIDENCE\n2099-01-01T00:00:00+00:00\n", encoding="utf-8")

    try:
        runner.stop_current("mode_switch", next_card="REVEAL")
    finally:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    assert not running_event.exists()
    assert runner.state["current_card"] is None


def test_queue_runs_one_card_action_at_a_time(tmp_path):
    config_dir = tmp_path / "rfid-modes"
    queue_dir = tmp_path / "queue"
    cards_dir = config_dir / "cards"
    for card in ("FIRST", "SECOND"):
        (cards_dir / card).mkdir(parents=True)
        script = cards_dir / f"{card}.sh"
        script.write_text("#!/usr/bin/env bash\nsleep 0.15\n", encoding="utf-8")
        script.chmod(0o755)
    pending = queue_dir / "pending"
    pending.mkdir(parents=True)
    for number, card in enumerate(("FIRST", "SECOND"), start=1):
        event_id = f"{number:020d}-{card}"
        (pending / f"{event_id}.json").write_text(
            json.dumps(
                {
                    "event_id": event_id,
                    "scan": {"rfid": card, "scanned_at": f"t{number}", "queue_event_id": event_id},
                }
            ),
            encoding="utf-8",
        )
    runner = mode_runner.ModeRunner(
        scan_file=tmp_path / "scan.json",
        queue_dir=queue_dir,
        config_dir=config_dir,
        poll_seconds=0.01,
        stop_timeout=0.1,
    )
    runner.setup()

    runner.process_queue()
    assert runner.queue_active is not None
    assert runner.queue_active.scan.card_id == "FIRST"
    assert len(list(pending.glob("*.json"))) == 1
    runner.process_queue()
    assert runner.queue_active is not None
    assert runner.queue_active.scan.card_id == "FIRST"

    time.sleep(0.2)
    runner.reap_current_if_exited()
    runner.process_queue()
    runner.process_queue()
    assert runner.queue_active is not None
    assert runner.queue_active.scan.card_id == "SECOND"
    time.sleep(0.2)
    runner.reap_current_if_exited()
    runner.process_queue()
    assert len(list((queue_dir / "done").glob("*.json"))) == 2


def test_queue_clear_leaves_no_control_or_pending_records(tmp_path):
    config_dir = tmp_path / "rfid-modes"
    queue_dir = tmp_path / "queue"
    control = queue_dir / "control"
    pending = queue_dir / "pending"
    control.mkdir(parents=True)
    pending.mkdir(parents=True)
    (control / "clear-test.json").write_text(json.dumps({"reason": "test"}), encoding="utf-8")
    (pending / "queued.json").write_text("{}", encoding="utf-8")
    runner = mode_runner.ModeRunner(
        scan_file=tmp_path / "scan.json",
        queue_dir=queue_dir,
        config_dir=config_dir,
        poll_seconds=0.01,
        stop_timeout=0.1,
    )
    runner.setup()

    runner.process_queue_controls()

    assert not list(pending.glob("*.json"))
    assert not list(control.glob("clear-*.json"))
