#!/usr/bin/env python3
"""Show UID-safe local RFID queue and queue-aware gadget state."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


HOME = Path(os.environ.get("HOME", "/home/arthe"))
DEFAULT_QUEUE_DIR = HOME / ".local/state/rfid-mode/queue"
DEFAULT_RUNNER_STATE = HOME / ".config/rfid-modes/state/state.json"
DEFAULT_CARDS_DIR = HOME / ".config/rfid-modes/cards"
DEFAULT_GADGET_STATE = HOME / ".local/state/gway-gadget/queue-arm.json"
MODE_LABEL_RE = re.compile(r"^\s*#\s*RFID mode:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def count_records(queue_dir: Path, state: str) -> int:
    return sum(1 for _ in (queue_dir / state).glob("*.json"))


def label_for_record(path: Path, cards_dir: Path) -> str:
    """Resolve a human mapping label without returning the card identifier."""
    record = read_json(path)
    scan = record.get("scan") if isinstance(record.get("scan"), dict) else {}
    card_id = str(scan.get("rfid") or "")
    if not card_id:
        return "UNLINKED"
    for candidate in (cards_dir / card_id / "label.txt", cards_dir / f"{card_id}.sh", cards_dir / f"{card_id}.py"):
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if candidate.name == "label.txt":
            return next((line.strip() for line in text.splitlines() if line.strip()), "MAPPED")[:64]
        match = MODE_LABEL_RE.search(text)
        if match:
            return match.group(1).strip()[:64]
    return "MAPPED"


def status(args: argparse.Namespace) -> dict[str, Any]:
    states = ("pending", "processing", "interrupt", "control", "done", "failed", "cleared", "interrupted")
    counts = {state: count_records(args.queue_dir, state) for state in states}
    processing = sorted((args.queue_dir / "processing").glob("*.json"))
    interrupts = sorted((args.queue_dir / "interrupt").glob("*.json"))
    runner = read_json(args.runner_state)
    gadget = read_json(args.gadget_state)
    return {
        "queue": {"counts": counts, "active": bool(processing), "active_label": label_for_record(processing[0], args.cards_dir) if processing else ""},
        "interrupt": {"waiting": bool(interrupts), "next_label": label_for_record(interrupts[0], args.cards_dir) if interrupts else ""},
        "runner": {"active": bool(runner.get("queue_active_file")), "updated_at": str(runner.get("updated_at") or "")},
        "gadget_arm": {"active": bool(gadget), "mode": str(gadget.get("mode") or ""), "state": str(gadget.get("state") or ""), "expires_at": str(gadget.get("expires_at") or "")},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR)
    parser.add_argument("--runner-state", type=Path, default=DEFAULT_RUNNER_STATE)
    parser.add_argument("--cards-dir", type=Path, default=DEFAULT_CARDS_DIR)
    parser.add_argument("--gadget-state", type=Path, default=DEFAULT_GADGET_STATE)
    parser.add_argument("--json", action="store_true", help="print structured status")
    args = parser.parse_args()
    payload = status(args)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        counts = payload["queue"]["counts"]
        print("RFID queue: " + " ".join(f"{key}={value}" for key, value in counts.items()))
        if payload["queue"]["active"]:
            print(f"Active: {payload['queue']['active_label']}")
        if payload["interrupt"]["waiting"]:
            print(f"Interrupt waiting: {payload['interrupt']['next_label']}")
        gadget = payload["gadget_arm"]
        print(f"Gadget arm: {'active' if gadget['active'] else 'inactive'}" + (f" ({gadget['mode']})" if gadget["mode"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
