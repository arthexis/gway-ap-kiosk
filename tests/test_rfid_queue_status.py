from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/gway/rfid_queue_status.py"


def load_module():
    spec = importlib.util.spec_from_file_location("rfid_queue_status", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_status_reports_counts_and_label_without_card_id(tmp_path):
    module = load_module()
    queue = tmp_path / "queue"
    processing = queue / "processing"
    processing.mkdir(parents=True)
    (processing / "event.json").write_text(json.dumps({"scan": {"rfid": "PRIVATECARD"}}), encoding="utf-8")
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "PRIVATECARD.sh").write_text("# RFID mode: SAFE ACTION\n", encoding="utf-8")
    args = type("Args", (), {"queue_dir": queue, "runner_state": tmp_path / "runner.json", "cards_dir": cards, "gadget_state": tmp_path / "gadget.json"})()

    payload = module.status(args)

    assert payload["queue"]["counts"]["processing"] == 1
    assert payload["queue"]["active_label"] == "SAFE ACTION"
    assert "PRIVATECARD" not in json.dumps(payload)
