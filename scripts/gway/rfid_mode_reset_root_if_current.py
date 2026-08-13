#!/usr/bin/env python3
"""Return RFID mode navigation to root after a one-shot card action."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def expand_path(raw: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default=os.environ.get("RFID_MODE_CONFIG_DIR", "~/.config/rfid-modes"))
    parser.add_argument("--card", default=os.environ.get("RFID_MODE_CARD_ID", ""))
    parser.add_argument("--script", default=os.environ.get("RFID_MODE_SCRIPT", ""))
    args = parser.parse_args(argv)

    config_dir = expand_path(args.config_dir)
    cards_dir = config_dir / "cards"
    state_file = config_dir / "state" / "state.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    expected_card = str(args.card or "").strip().upper()
    expected_script = str(args.script or "").strip()
    current_card = str(state.get("current_card") or "").strip().upper()
    current_script = str(state.get("current_script") or "").strip()
    last_scan_card = str(state.get("last_scan_card") or "").strip().upper()
    state_path = [str(part).strip().upper() for part in (state.get("state_path") or [])]

    if expected_card and current_card and current_card != expected_card:
        return 0
    if expected_script and current_script and current_script != expected_script:
        return 0
    if expected_card and last_scan_card and last_scan_card != expected_card:
        return 0
    if expected_card and state_path and state_path[-1] != expected_card:
        return 0

    state.update(
        {
            "state_path": [],
            "state_dir": str(cards_dir),
            "current_card": None,
            "current_script": None,
            "current_mode_kind": None,
            "pid": None,
            "mode_started_at": None,
            "mode_log": None,
            "hold_card": None,
            "hold_script": None,
            "hold_presence_id": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "one_shot_reset_at": datetime.now(timezone.utc).isoformat(),
            "one_shot_reset_card": expected_card,
            "one_shot_reset_script": expected_script,
        }
    )
    tmp = state_file.with_name(f".{state_file.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(state_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
