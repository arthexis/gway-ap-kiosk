#!/usr/bin/env python3
"""Run suite RFID service in etron card-registration mode.

This wrapper is intended for GWAY nodes that still use the suite to read the
local RFID hardware but should register scans into the plain etron-server
cards file instead of running suite command-card actions.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

MATCH_PREFIX_LENGTH = 8
DEFAULT_CARDS_FILE = Path.home() / "cards.txt"


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


def load_card_codes(path: Path) -> list[str]:
    if not path.exists():
        return []
    codes: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            raw_code, _label = line.split(",", 1)
        else:
            parts = line.split(maxsplit=1)
            raw_code = parts[0]
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
    prefixes = {
        check[: min(len(check), MATCH_PREFIX_LENGTH)] for check in checks if check
    }
    return any(
        any(code.startswith(prefix) for prefix in prefixes if prefix)
        for code in existing_codes
    )


def ensure_cards_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    path.write_text(
        "# One RFID idTag per line. Add an optional label after a comma.\n"
        "# Lines beginning with # are ignored.\n"
        "#\n"
        "# CARD_ID_HEX, Example label\n",
        encoding="utf-8",
    )


def append_card_if_new(path: Path, payload: dict[str, Any]) -> bool:
    rfid = normalize_code(payload.get("rfid"))
    if not rfid:
        return False
    ensure_cards_file(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            import fcntl
        except ModuleNotFoundError:  # pragma: no cover - Windows inspection only.
            fcntl = None
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        existing_codes = load_card_codes(path)
        if matches_existing_card(rfid, existing_codes):
            return False
        with path.open("a", encoding="utf-8") as cards_file:
            cards_file.write(f"{rfid}\n")
        return True


def patch_rfid_service(cards_file: Path, *, disable_command_cards: bool) -> None:
    from apps.cards import rfid_service

    original_emit = rfid_service.RFIDServiceState._emit_scan_artifacts

    def emit_and_register(self, result: dict[str, Any]) -> dict[str, Any] | None:
        payload = original_emit(self, result)
        if isinstance(payload, dict):
            try:
                append_card_if_new(cards_file, payload)
            except OSError:
                rfid_service.logger.warning(
                    "Unable to append scanned RFID to %s",
                    cards_file,
                    exc_info=True,
                )
        return payload

    rfid_service.RFIDServiceState._emit_scan_artifacts = emit_and_register

    if disable_command_cards:

        def skip_command_card(self, payload: dict[str, Any]) -> bool:
            return False

        rfid_service.RFIDServiceState._maybe_execute_command_card = skip_command_card


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the suite RFID scanner service while registering new scans "
            "into an etron-server cards.txt file."
        )
    )
    parser.add_argument(
        "--cards-file",
        default=os.environ.get("ETRON_RFID_CARDS_FILE", str(DEFAULT_CARDS_FILE)),
        help="Plain etron-server cards file to append new RFID UIDs to.",
    )
    parser.add_argument("--host", default=None, help="RFID UDP host override.")
    parser.add_argument("--port", type=int, default=None, help="RFID UDP port override.")
    parser.add_argument(
        "--allow-command-cards",
        action="store_true",
        help="Keep suite command-card execution enabled.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)
    cards_file = Path(options.cards_file).expanduser()

    from config.loadenv import loadenv
    from config.sqlite_driver import bootstrap_sqlite_driver

    loadenv()
    bootstrap_sqlite_driver()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    import django

    django.setup()

    patch_rfid_service(
        cards_file,
        disable_command_cards=not options.allow_command_cards,
    )

    from apps.cards import rfid_service

    ensure_cards_file(cards_file)
    print(
        "Starting RFID service in etron registration mode; "
        f"cards_file={cards_file}",
        flush=True,
    )
    rfid_service.run_service(host=options.host, port=options.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
