#!/usr/bin/env python3
"""Register RFID transition directories and mode scripts."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "rfid-modes"
LOCAL_SCAN_FILE = Path.home() / ".local" / "state" / "rfid-mode" / "rfid-scan.json"
LEGACY_ARTHEXIS_SCAN_FILE = Path("/home/arthe/arthexis/.locks/rfid-scan.json")
SCAN_FILE_CONFIG_NAME = "scan-file"
CARD_ID_RE = re.compile(r"[^A-Z0-9_.-]+")
SCRIPT_SUFFIXES = ("", ".sh", ".py")
HOLD_SCRIPT_PREFIX = "hold"
NEW_SCRIPT_PREFIX = "new"


def expand_path(raw: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw))))


def configured_scan_file(config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    env_value = os.environ.get("RFID_MODE_SCAN_FILE")
    if env_value:
        return expand_path(env_value)

    config_path = expand_path(
        os.environ.get("RFID_MODE_SCAN_FILE_CONFIG", str(config_dir / SCAN_FILE_CONFIG_NAME))
    )
    try:
        configured = config_path.read_text(encoding="utf-8").strip()
    except OSError:
        configured = ""
    if configured:
        return expand_path(configured)

    for candidate in (LOCAL_SCAN_FILE, LEGACY_ARTHEXIS_SCAN_FILE):
        if candidate.exists():
            return candidate
    return LOCAL_SCAN_FILE


DEFAULT_SCAN_FILE = configured_scan_file()


def normalize_card_id(raw: Any) -> str:
    value = str(raw or "").strip().upper()
    value = value.replace(":", "").replace(" ", "")
    return CARD_ID_RE.sub("_", value)


def normalize_path(raw: str) -> list[str]:
    parts = [normalize_card_id(part) for part in raw.split("/") if normalize_card_id(part)]
    if not parts:
        raise SystemExit("path must include at least one RFID")
    return parts


def read_scan(path: Path) -> tuple[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    card_id = normalize_card_id(
        payload.get("rfid")
        or payload.get("card_id")
        or payload.get("uid")
        or payload.get("id")
    )
    if not card_id:
        raise SystemExit(f"no card ID found in {path}")
    scanned_at = str(payload.get("scanned_at") or payload.get("timestamp") or "")
    return card_id, scanned_at


def scan_key(path: Path) -> str:
    card_id, scanned_at = read_scan(path)
    return f"{card_id}|{scanned_at or path.stat().st_mtime_ns}"


def wait_for_card(path: Path, timeout: float | None) -> str:
    print("Scan the RFID card to register now...", file=sys.stderr)
    start_key = None
    try:
        start_key = scan_key(path)
    except Exception:
        pass
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise SystemExit("timed out waiting for a new RFID scan")
        try:
            current_key = scan_key(path)
            card_id, _ = read_scan(path)
        except Exception:
            time.sleep(0.25)
            continue
        if current_key != start_key:
            return card_id
        time.sleep(0.25)


def current_card(path: Path) -> str:
    card_id, scanned_at = read_scan(path)
    print(f"Using latest scan: {card_id} ({scanned_at})", file=sys.stderr)
    return card_id


def write_command_script(
    path: Path,
    command: str,
    label: str,
    *,
    directives: list[str] | None = None,
    clear_queue: bool = False,
) -> None:
    quoted = shlex.quote(command)
    policy_lines = "".join(f"# {directive}\n" for directive in directives or [])
    clear_line = (
        "/home/arthe/.local/bin/rfid-mode-runner --request-queue-clear --reason card_clear\n"
        if clear_queue
        else ""
    )
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"# RFID mode: {label}\n"
        + policy_lines
        + clear_line
        + f"exec bash -lc {quoted}\n",
        encoding="utf-8",
    )


def write_template_script(path: Path, label: str) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"# RFID mode: {label}\n"
        "# Replace this loop with the program for this mode.\n"
        "while true; do\n"
        "  sleep 3600\n"
        "done\n",
        encoding="utf-8",
    )


def cards_root(config_dir: Path) -> Path:
    root = config_dir / "cards"
    root.mkdir(parents=True, exist_ok=True)
    return root


def target_path(root: Path, parts: list[str]) -> Path:
    path = root
    for part in parts:
        path = path / part
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        raise SystemExit("target escaped cards directory") from None
    return path


def candidate_script_paths(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in SCRIPT_SUFFIXES]


def existing_scripts(path: Path) -> list[Path]:
    return [candidate for candidate in candidate_script_paths(path) if candidate.is_file()]


def existing_script(path: Path) -> Path | None:
    scripts = existing_scripts(path)
    return scripts[0] if scripts else None


def existing_hold_scripts(path: Path) -> list[Path]:
    return [path / f"{HOLD_SCRIPT_PREFIX}.sh", path / f"{HOLD_SCRIPT_PREFIX}.py"]


def existing_new_scripts(path: Path) -> list[Path]:
    return [path / f"{NEW_SCRIPT_PREFIX}.sh", path / f"{NEW_SCRIPT_PREFIX}.py"]


def script_target_suffix(source: Path | None) -> str:
    if source is None:
        return ""
    suffix = source.suffix.lower()
    return suffix if suffix in {".sh", ".py"} else ""


def validate_script_target(root: Path, parts: list[str], force: bool, suffix: str = "") -> Path:
    parent = target_path(root, parts[:-1])
    target = parent / parts[-1]
    target_is_dir = target.is_dir()
    if len(parts) < 2 and not target_is_dir:
        raise SystemExit("top-level scripts are not allowed; register a top-level directory first")
    if len(parts) > 1 and parts[-1] == parts[-2]:
        raise SystemExit("a directory cannot contain a script with the same RFID as the directory name")

    if not parent.is_dir():
        raise SystemExit(f"parent directory does not exist: {parent}")
    if target_is_dir and not suffix:
        raise SystemExit(
            f"target is already a directory; use a .sh or .py script sidecar: {target}"
        )
    existing = existing_scripts(target)
    if existing and not force:
        raise SystemExit(f"{existing[0]} already exists; pass --force to overwrite")
    if existing and force:
        requested_target = target.with_name(target.name + suffix)
        for script in existing:
            if script != requested_target:
                script.unlink()
        target = requested_target
    elif suffix:
        target = target.with_name(target.name + suffix)
    return target


def validate_dir_target(root: Path, parts: list[str], force: bool) -> Path:
    target = target_path(root, parts)
    if target.exists() and not target.is_dir():
        raise SystemExit(f"target exists and is not a directory: {target}")
    if target.exists() and not force:
        raise SystemExit(f"{target} already exists; pass --force to accept existing directory")
    return target


def validate_hold_target(root: Path, parts: list[str], force: bool, suffix: str = "") -> Path:
    target_dir = target_path(root, parts)
    if not target_dir.is_dir():
        raise SystemExit(f"hold scripts require an existing directory: {target_dir}")
    suffix = suffix if suffix in {".sh", ".py"} else ".sh"
    target = target_dir / f"{HOLD_SCRIPT_PREFIX}{suffix}"
    existing = [script for script in existing_hold_scripts(target_dir) if script.is_file()]
    if existing and not force:
        raise SystemExit(f"{existing[0]} already exists; pass --force to overwrite")
    for script in existing:
        if script != target:
            script.unlink()
    return target


def validate_new_target(root: Path, parts: list[str], force: bool, suffix: str = "") -> Path:
    target_dir = target_path(root, parts)
    if not target_dir.is_dir():
        raise SystemExit(f"new-card scripts require an existing directory: {target_dir}")
    suffix = suffix if suffix in {".sh", ".py"} else ".sh"
    target = target_dir / f"{NEW_SCRIPT_PREFIX}{suffix}"
    existing = [script for script in existing_new_scripts(target_dir) if script.is_file()]
    if existing and not force:
        raise SystemExit(f"{existing[0]} already exists; pass --force to overwrite")
    for script in existing:
        if script != target:
            script.unlink()
    return target


def list_cards(config_dir: Path) -> None:
    root = cards_root(config_dir)
    rows: list[tuple[str, str, Path]] = []
    for path in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root)
        label = "/".join(rel.parts)
        if path.is_dir():
            rows.append(("d", label + "/", path))
        elif path.is_file():
            marker = "x" if os.access(path, os.X_OK) else "-"
            rows.append((marker, label, path))
    if not rows:
        print("No RFID transition paths registered yet.")
        return
    for marker, label, path in rows:
        policy: list[str] = []
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "RFID_MODE_QUEUE_INTERRUPT=1" in text:
                policy.append("interrupt")
            if "RFID_MODE_QUEUE_DEEP_DATA=1" in text:
                policy.append("deep-data")
            if "RFID_MODE_QUEUE_CONSUME_NEXT" in text:
                policy.append("consume-next")
            if "--request-queue-clear" in text:
                policy.append("clear-queue")
        suffix = f" [{', '.join(policy)}]" if policy else ""
        print(f"{marker} {label}{suffix}\t{path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register RFID transition directories and mode scripts.")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--scan-file", type=Path, default=DEFAULT_SCAN_FILE)
    parser.add_argument("--path", help="slash-separated parent path or full path, e.g. CARD1/CARD2")
    parser.add_argument("--card", help="RFID card ID to append to --path or register at the root")
    parser.add_argument("--latest", action="store_true", help="use the latest scanned card")
    parser.add_argument("--wait", action="store_true", help="wait for the next scanned card")
    parser.add_argument("--wait-timeout", type=float, default=60.0)
    parser.add_argument("--script", type=Path, help="copy an existing shell or Python script into the transition path")
    parser.add_argument("--command", help="create a mode script that execs this shell command")
    parser.add_argument("--label", default="local mode", help="human label written as a script comment")
    parser.add_argument("--dir", action="store_true", help="create a directory transition")
    parser.add_argument("--hold", action="store_true", help="register hold.sh/hold.py in the target directory")
    parser.add_argument("--new", action="store_true", help="register new.sh/new.py in the target directory")
    parser.add_argument("--interrupt", action="store_true", help="queue this command as an interrupt that stops active work and runs next")
    parser.add_argument("--deep-data", action="store_true", help="request deep-read context before this command is queued")
    parser.add_argument("--consume-next-seconds", type=int, metavar="SECONDS", help="allow this command to consume the next queued card within SECONDS")
    parser.add_argument("--clear-queue", action="store_true", help="make this an interrupt that clears queued work when it runs")
    parser.add_argument("--force", action="store_true", help="overwrite an existing script or accept existing directory")
    parser.add_argument("--list", action="store_true", help="list registered transition paths")
    return parser.parse_args()


def selected_card(args: argparse.Namespace) -> str | None:
    selected = [bool(args.card), args.latest, args.wait]
    if sum(selected) > 1:
        raise SystemExit("choose only one of --card, --latest, or --wait")
    if args.card:
        return normalize_card_id(args.card)
    if args.latest:
        return current_card(args.scan_file)
    if args.wait:
        return wait_for_card(args.scan_file, args.wait_timeout)
    return None


def main() -> int:
    args = parse_args()
    root = cards_root(args.config_dir)

    if args.list:
        list_cards(args.config_dir)
        return 0

    if args.script and args.command:
        raise SystemExit("choose either --script or --command, not both")

    path_parts = normalize_path(args.path) if args.path else []
    card_id = selected_card(args)
    if card_id:
        path_parts.append(card_id)
    if not path_parts:
        raise SystemExit("provide --path or one of --card, --latest, --wait")

    script_mode = bool(args.script or args.command)
    if args.dir and script_mode:
        raise SystemExit("choose either --dir or a script/command action")
    if args.hold and args.dir:
        raise SystemExit("choose either --hold or --dir")
    if args.new and args.dir:
        raise SystemExit("choose either --new or --dir")
    if args.hold and args.new:
        raise SystemExit("choose only one of --hold or --new")
    if (args.hold or args.new) and not script_mode:
        raise SystemExit("--hold/--new requires --script or --command")
    policy_requested = args.interrupt or args.deep_data or args.consume_next_seconds is not None or args.clear_queue
    if policy_requested and args.script:
        raise SystemExit("queue policy options currently require --command so directives can be written safely")
    if policy_requested and not args.command:
        raise SystemExit("queue policy options require --command")
    if policy_requested and (args.hold or args.new):
        raise SystemExit("queue policy options cannot be combined with --hold or --new")
    if args.consume_next_seconds is not None and args.consume_next_seconds <= 0:
        raise SystemExit("--consume-next-seconds must be greater than zero")

    if script_mode:
        if args.hold:
            suffix = script_target_suffix(args.script) or ".sh"
            target = validate_hold_target(root, path_parts, args.force, suffix)
        elif args.new:
            suffix = script_target_suffix(args.script) or ".sh"
            target = validate_new_target(root, path_parts, args.force, suffix)
        else:
            suffix = script_target_suffix(args.script)
            if args.command and target_path(root, path_parts).is_dir():
                suffix = ".sh"
            target = validate_script_target(root, path_parts, args.force, suffix)
        if args.script:
            if not args.script.is_file():
                raise SystemExit(f"script not found: {args.script}")
            shutil.copyfile(args.script, target)
        else:
            directives: list[str] = []
            if args.interrupt or args.clear_queue:
                directives.append("RFID_MODE_QUEUE_INTERRUPT=1")
            if args.deep_data:
                directives.append("RFID_MODE_QUEUE_DEEP_DATA=1")
            if args.consume_next_seconds is not None:
                directives.append(f"RFID_MODE_QUEUE_CONSUME_NEXT_SECONDS={args.consume_next_seconds}")
            write_command_script(
                target,
                args.command or "",
                args.label,
                directives=directives,
                clear_queue=args.clear_queue,
            )
        target.chmod(target.stat().st_mode | 0o700)
        print(f"Registered RFID script {'/'.join(path_parts)}: {target}")
    else:
        target = validate_dir_target(root, path_parts, args.force)
        target.mkdir(parents=True, exist_ok=True)
        print(f"Registered RFID directory {'/'.join(path_parts)}: {target}")

    print(f"Updated at {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
