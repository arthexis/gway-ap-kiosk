from __future__ import annotations

import json
import os
import subprocess
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
