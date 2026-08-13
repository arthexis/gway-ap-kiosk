#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AP_CONNECTION = "arthexis-ap"
DEFAULT_AP_INTERFACE = "wlan0"
DEFAULT_UPLINK_INTERFACE = "wlan1"
DEFAULT_PREFERRED_CLIENT_CONNECTIONS: tuple[str, ...] = ()
DEFAULT_NMCLI_BIN = "nmcli"
DEFAULT_STATE_FILE = "/var/lib/arthexis-wifi-role/state.json"
DEFAULT_RECENT_SSID_TTL_SECONDS = 45
DEFAULT_FAILOVER_RETRY_SECONDS = 4
DEFAULT_FAILOVER_RETRY_INTERVAL_SECONDS = 2
DEFAULT_FALLBACK_FAILURE_COOLDOWN_SECONDS = 90
DEFAULT_CONNECTED_SCAN_INTERVAL_SECONDS = 20


@dataclass(frozen=True)
class DeviceStatus:
    device: str
    device_type: str
    state: str
    connection: str


@dataclass(frozen=True)
class PreferredConnection:
    name: str
    ssid: str


def _env_csv(name: str, default: tuple[str, ...]) -> list[str]:
    raw_value = os.environ.get(name, "")
    if not raw_value.strip():
        return list(default)
    return [token.strip() for token in raw_value.split(",") if token.strip()]


def _env_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _run_nmcli(*parts: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    nmcli_bin = os.environ.get("NMCLI_BIN", DEFAULT_NMCLI_BIN).strip()
    nmcli_bin = nmcli_bin or DEFAULT_NMCLI_BIN
    completed = subprocess.run(
        [nmcli_bin, *parts],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or "unknown nmcli error"
        raise RuntimeError(f"nmcli {' '.join(parts)} failed: {detail}")
    return completed


def _device_statuses() -> dict[str, DeviceStatus]:
    result = _run_nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
    statuses: dict[str, DeviceStatus] = {}
    for raw_line in result.stdout.splitlines():
        if not raw_line.strip():
            continue
        device, device_type, state, connection = raw_line.split(":", 3)
        statuses[device] = DeviceStatus(
            device=device,
            device_type=device_type,
            state=state,
            connection=connection,
        )
    return statuses


def _connection_exists(name: str) -> bool:
    result = _run_nmcli("-t", "-f", "NAME", "connection", "show", check=False)
    if result.returncode != 0:
        return False
    return name in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _connection_field(name: str, field: str) -> str:
    result = _run_nmcli("-s", "-g", field, "connection", "show", name, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _scan_visible_ssids(interface: str) -> set[str]:
    _run_nmcli("device", "wifi", "rescan", "ifname", interface, check=False)
    result = _run_nmcli(
        "-t",
        "-f",
        "SSID",
        "device",
        "wifi",
        "list",
        "ifname",
        interface,
        "--rescan",
        "no",
        check=False,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _load_state(state_file: Path) -> dict[str, object]:
    default_state: dict[str, object] = {
        "seen_ssids": {},
        "suppressed_until": {},
        "last_connected_scan_at": 0,
    }
    try:
        raw_state = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_state
    except (OSError, json.JSONDecodeError):
        return default_state
    if not isinstance(raw_state, dict):
        return default_state

    normalized_state: dict[str, object] = {}
    for key in ("seen_ssids", "suppressed_until"):
        raw_bucket = raw_state.get(key, {})
        if not isinstance(raw_bucket, dict):
            normalized_state[key] = {}
            continue
        normalized_bucket: dict[str, int] = {}
        for name, value in raw_bucket.items():
            if not isinstance(name, str):
                continue
            try:
                normalized_bucket[name] = int(value)
            except (TypeError, ValueError):
                continue
        normalized_state[key] = normalized_bucket
    try:
        normalized_state["last_connected_scan_at"] = int(
            raw_state.get("last_connected_scan_at", 0)
        )
    except (TypeError, ValueError):
        normalized_state["last_connected_scan_at"] = 0
    return normalized_state


def _save_state(state_file: Path, state: dict[str, object]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")


def _preferred_connection_profiles(
    interface: str,
    preferred_connections: list[str],
) -> list[PreferredConnection]:
    profiles: list[PreferredConnection] = []
    for connection_name in preferred_connections:
        if not _connection_exists(connection_name):
            continue
        bound_interface = _connection_field(connection_name, "connection.interface-name")
        if bound_interface and bound_interface != interface:
            continue
        mode = _connection_field(connection_name, "802-11-wireless.mode")
        if mode and mode != "infrastructure":
            continue
        ssid = _connection_field(connection_name, "802-11-wireless.ssid")
        if not ssid:
            continue
        profiles.append(PreferredConnection(name=connection_name, ssid=ssid))
    return profiles


def _update_seen_ssids(
    state: dict[str, object],
    profiles: list[PreferredConnection],
    visible_ssids: set[str],
    *,
    observed_at: int,
) -> None:
    if not visible_ssids:
        return
    seen_ssids = state.setdefault("seen_ssids", {})
    assert isinstance(seen_ssids, dict)
    for profile in profiles:
        if profile.ssid in visible_ssids:
            seen_ssids[profile.ssid] = observed_at


def _is_suppressed(
    state: dict[str, object],
    ssid: str,
    *,
    observed_at: int,
) -> bool:
    suppressed_until = state.get("suppressed_until", {})
    if not isinstance(suppressed_until, dict):
        return False
    return int(suppressed_until.get(ssid, 0)) > observed_at


def _suppress_ssid(
    state: dict[str, object],
    ssid: str,
    *,
    observed_at: int,
    cooldown_seconds: int,
) -> None:
    if cooldown_seconds <= 0:
        return
    suppressed_until = state.setdefault("suppressed_until", {})
    assert isinstance(suppressed_until, dict)
    suppressed_until[ssid] = observed_at + cooldown_seconds


def _clear_seen_ssid(state: dict[str, object], ssid: str) -> None:
    seen_ssids = state.setdefault("seen_ssids", {})
    assert isinstance(seen_ssids, dict)
    seen_ssids.pop(ssid, None)


def _visible_fallback_connection(
    profiles: list[PreferredConnection],
    visible_ssids: set[str],
    state: dict[str, object],
    *,
    observed_at: int,
) -> tuple[str, str]:
    for profile in profiles:
        if profile.ssid not in visible_ssids:
            continue
        if _is_suppressed(state, profile.ssid, observed_at=observed_at):
            continue
        return profile.name, profile.ssid
    return "", ""


def _recently_seen_fallback_connection(
    profiles: list[PreferredConnection],
    state: dict[str, object],
    *,
    observed_at: int,
    ttl_seconds: int,
) -> tuple[str, str, int]:
    if ttl_seconds <= 0:
        return "", "", -1
    seen_ssids = state.get("seen_ssids", {})
    if not isinstance(seen_ssids, dict):
        return "", "", -1
    for profile in profiles:
        if _is_suppressed(state, profile.ssid, observed_at=observed_at):
            continue
        last_seen_at = seen_ssids.get(profile.ssid)
        if last_seen_at is None:
            continue
        age_seconds = observed_at - int(last_seen_at)
        if 0 <= age_seconds <= ttl_seconds:
            return profile.name, profile.ssid, age_seconds
    return "", "", -1


def _scan_available_ssids(scan_interfaces: list[str]) -> tuple[set[str], list[str]]:
    visible_ssids: set[str] = set()
    used_scan_interfaces: list[str] = []
    for scan_interface in scan_interfaces:
        scanned_ssids = _scan_visible_ssids(scan_interface)
        if not scanned_ssids:
            continue
        visible_ssids.update(scanned_ssids)
        used_scan_interfaces.append(scan_interface)
    return visible_ssids, used_scan_interfaces


def _retry_visible_fallback_connection(
    profiles: list[PreferredConnection],
    scan_interfaces: list[str],
    state: dict[str, object],
    *,
    retry_seconds: int,
    retry_interval_seconds: int,
) -> tuple[str, str, set[str], list[str]]:
    if retry_seconds <= 0:
        return "", "", set(), []

    deadline = time.monotonic() + retry_seconds
    retry_interval_seconds = max(retry_interval_seconds, 1)
    combined_visible_ssids: set[str] = set()
    combined_scan_interfaces: list[str] = []

    while time.monotonic() < deadline:
        visible_ssids, used_scan_interfaces = _scan_available_ssids(scan_interfaces)
        observed_at = int(time.time())
        _update_seen_ssids(
            state,
            profiles,
            visible_ssids,
            observed_at=observed_at,
        )
        combined_visible_ssids.update(visible_ssids)
        for scan_interface in used_scan_interfaces:
            if scan_interface not in combined_scan_interfaces:
                combined_scan_interfaces.append(scan_interface)
        connection_name, ssid = _visible_fallback_connection(
            profiles,
            visible_ssids,
            state,
            observed_at=observed_at,
        )
        if connection_name:
            return connection_name, ssid, combined_visible_ssids, combined_scan_interfaces
        time.sleep(retry_interval_seconds)

    return "", "", combined_visible_ssids, combined_scan_interfaces


def _scan_candidates(
    device_statuses: dict[str, DeviceStatus],
    *,
    scan_interface: str,
    ap_interface: str,
) -> list[str]:
    candidates: list[str] = []
    for interface in (scan_interface, ap_interface):
        if not interface or interface in candidates:
            continue
        status = device_statuses.get(interface)
        if status is not None and status.device_type != "wifi":
            continue
        candidates.append(interface)
    return candidates


def _activate_connection(name: str, interface: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"action=activate connection={name} interface={interface}")
        return
    completed = _run_nmcli(
        "--wait",
        "20",
        "connection",
        "up",
        "id",
        name,
        "ifname",
        interface,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or "activation failed"
        raise RuntimeError(f"could not activate {name} on {interface}: {detail}")
    print(f"action=activated connection={name} interface={interface}")


def _modify_connection(name: str, updates: list[str], *, dry_run: bool) -> None:
    if not updates:
        return
    if dry_run:
        print(f"action=modify connection={name} updates={' '.join(updates)}")
        return
    _run_nmcli("connection", "modify", name, *updates)
    print(f"action=modified connection={name} updates={' '.join(updates)}")


def _current_connection(
    device_statuses: dict[str, DeviceStatus], interface: str
) -> str:
    status = device_statuses.get(interface)
    if status is None:
        return ""
    return status.connection.strip()


def _uplink_connected(
    device_statuses: dict[str, DeviceStatus], interface: str
) -> bool:
    status = device_statuses.get(interface)
    if status is None:
        return False
    return status.state == "connected" and bool(status.connection.strip())


def _normalize_autoconnect_policy(
    ap_connection: str,
    preferred_connections: list[str],
    *,
    dry_run: bool,
) -> None:
    if _connection_exists(ap_connection):
        ap_updates: list[str] = []
        if _connection_field(ap_connection, "connection.autoconnect") != "yes":
            ap_updates.extend(["connection.autoconnect", "yes"])
        if _connection_field(ap_connection, "connection.autoconnect-priority") != "100":
            ap_updates.extend(["connection.autoconnect-priority", "100"])
        _modify_connection(ap_connection, ap_updates, dry_run=dry_run)

    for connection_name in preferred_connections:
        if not _connection_exists(connection_name):
            continue
        fallback_updates: list[str] = []
        if _connection_field(connection_name, "connection.autoconnect") != "no":
            fallback_updates.extend(["connection.autoconnect", "no"])
        if _connection_field(connection_name, "connection.autoconnect-priority") != "0":
            fallback_updates.extend(["connection.autoconnect-priority", "0"])
        _modify_connection(connection_name, fallback_updates, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Keep the AP interface on the AP profile unless the uplink is down "
            "and a configured fallback client SSID is visible."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the chosen action without changing connections.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print additional decision detail.",
    )
    args = parser.parse_args()

    ap_connection = os.environ.get("AP_CONNECTION", DEFAULT_AP_CONNECTION).strip()
    ap_connection = ap_connection or DEFAULT_AP_CONNECTION
    ap_interface = os.environ.get("AP_INTERFACE", DEFAULT_AP_INTERFACE).strip()
    ap_interface = ap_interface or DEFAULT_AP_INTERFACE
    uplink_interface = os.environ.get(
        "UPLINK_INTERFACE", DEFAULT_UPLINK_INTERFACE
    ).strip()
    uplink_interface = uplink_interface or DEFAULT_UPLINK_INTERFACE
    scan_interface = os.environ.get("SCAN_INTERFACE", "").strip() or uplink_interface
    preferred_connections = _env_csv(
        "PREFERRED_CLIENT_CONNECTIONS",
        DEFAULT_PREFERRED_CLIENT_CONNECTIONS,
    )
    state_file = Path(
        os.environ.get("STATE_FILE", DEFAULT_STATE_FILE).strip()
        or DEFAULT_STATE_FILE
    )
    recent_ssid_ttl_seconds = _env_int(
        "RECENT_SSID_TTL_SECONDS",
        DEFAULT_RECENT_SSID_TTL_SECONDS,
    )
    failover_retry_seconds = _env_int(
        "FAILOVER_RETRY_SECONDS",
        DEFAULT_FAILOVER_RETRY_SECONDS,
    )
    failover_retry_interval_seconds = _env_int(
        "FAILOVER_RETRY_INTERVAL_SECONDS",
        DEFAULT_FAILOVER_RETRY_INTERVAL_SECONDS,
    )
    connected_scan_interval_seconds = _env_int(
        "CONNECTED_SCAN_INTERVAL_SECONDS",
        DEFAULT_CONNECTED_SCAN_INTERVAL_SECONDS,
    )
    fallback_failure_cooldown_seconds = _env_int(
        "FALLBACK_FAILURE_COOLDOWN_SECONDS",
        DEFAULT_FALLBACK_FAILURE_COOLDOWN_SECONDS,
    )

    try:
        _normalize_autoconnect_policy(
            ap_connection,
            preferred_connections,
            dry_run=args.dry_run,
        )
        device_statuses = _device_statuses()
    except RuntimeError as exc:
        print(f"error={exc}", file=sys.stderr)
        return 1

    current_ap_interface_connection = _current_connection(
        device_statuses, ap_interface
    )
    uplink_is_connected = _uplink_connected(device_statuses, uplink_interface)
    observed_at = int(time.time())
    state = _load_state(state_file)
    scan_interfaces = _scan_candidates(
        device_statuses,
        scan_interface=scan_interface,
        ap_interface=ap_interface,
    )
    profiles = _preferred_connection_profiles(ap_interface, preferred_connections)

    desired_connection = ap_connection
    visible_ssids: set[str] = set()
    decision_reason = f"{uplink_interface} connected"
    used_scan_interfaces: list[str] = []
    recent_candidate = ""
    recent_candidate_ssid = ""
    recent_candidate_age_seconds = -1

    last_connected_scan_at = int(state.get("last_connected_scan_at", 0))
    should_scan_now = (not uplink_is_connected) or (
        observed_at - last_connected_scan_at >= connected_scan_interval_seconds
    )
    if should_scan_now:
        visible_ssids, used_scan_interfaces = _scan_available_ssids(scan_interfaces)
        _update_seen_ssids(
            state,
            profiles,
            visible_ssids,
            observed_at=observed_at,
        )
        if uplink_is_connected:
            state["last_connected_scan_at"] = observed_at

    if not uplink_is_connected:
        state["last_connected_scan_at"] = 0
        fallback_connection, _fallback_ssid = _visible_fallback_connection(
            profiles,
            visible_ssids,
            state,
            observed_at=observed_at,
        )
        if fallback_connection:
            desired_connection = fallback_connection
            decision_reason = (
                f"{uplink_interface} disconnected and fallback client SSID visible"
            )
        else:
            (
                recent_candidate,
                recent_candidate_ssid,
                recent_candidate_age_seconds,
            ) = _recently_seen_fallback_connection(
                profiles,
                state,
                observed_at=observed_at,
                ttl_seconds=recent_ssid_ttl_seconds,
            )
            if recent_candidate:
                desired_connection = recent_candidate
                decision_reason = (
                    f"{uplink_interface} disconnected and fallback client SSID "
                    "recently seen"
                )
            else:
                (
                    retry_connection,
                    _retry_ssid,
                    retried_visible_ssids,
                    retried_scan_interfaces,
                ) = _retry_visible_fallback_connection(
                    profiles,
                    scan_interfaces,
                    state,
                    retry_seconds=failover_retry_seconds,
                    retry_interval_seconds=failover_retry_interval_seconds,
                )
                visible_ssids.update(retried_visible_ssids)
                for retried_interface in retried_scan_interfaces:
                    if retried_interface not in used_scan_interfaces:
                        used_scan_interfaces.append(retried_interface)
                if retry_connection:
                    desired_connection = retry_connection
                    decision_reason = (
                        f"{uplink_interface} disconnected and fallback client "
                        "SSID visible after retry"
                    )
                else:
                    decision_reason = (
                        f"{uplink_interface} disconnected and no fallback "
                        "client SSID visible"
                    )

    if not args.dry_run:
        _save_state(state_file, state)

    if args.verbose:
        seen_ssids = state.get("seen_ssids", {})
        if not isinstance(seen_ssids, dict):
            seen_ssids = {}
        recent_seen = ",".join(
            f"{profile.ssid}:{max(observed_at - int(seen_ssids.get(profile.ssid, observed_at)), 0)}s"
            for profile in profiles
            if profile.ssid in seen_ssids
        ) or "(none)"
        uplink_status = device_statuses.get(uplink_interface)
        print(
            "status="
            f"ap_interface={ap_interface}:"
            f"{current_ap_interface_connection or '(none)'} "
            f"uplink_interface={uplink_interface}:"
            f"{uplink_status.state if uplink_status else 'missing'} "
            f"scan_interface={scan_interface} "
            f"should_scan_now={'yes' if should_scan_now else 'no'} "
            f"used_scan_interfaces={','.join(used_scan_interfaces) or '(none)'} "
            f"preferred={','.join(preferred_connections) or '(none)'} "
            f"visible={','.join(sorted(visible_ssids)) or '(none)'} "
            f"recent={recent_seen}"
        )
        if recent_candidate:
            print(
                "recent_candidate="
                f"{recent_candidate} "
                f"ssid={recent_candidate_ssid} "
                f"age_seconds={recent_candidate_age_seconds}"
            )

    if not _connection_exists(ap_connection):
        print(f"error=missing_ap_connection connection={ap_connection}", file=sys.stderr)
        return 1

    if current_ap_interface_connection == desired_connection:
        print(
            "action=noop "
            f"decision={desired_connection} "
            f"reason={decision_reason.replace(' ', '_')}"
        )
        return 0

    print(
        "action=change "
        f"from_connection={current_ap_interface_connection or '(none)'} "
        f"to_connection={desired_connection} "
        f"reason={decision_reason.replace(' ', '_')}"
    )
    try:
        _activate_connection(desired_connection, ap_interface, dry_run=args.dry_run)
    except RuntimeError as exc:
        if desired_connection != ap_connection:
            failed_profile = next(
                (profile for profile in profiles if profile.name == desired_connection),
                None,
            )
            if failed_profile is not None:
                _clear_seen_ssid(state, failed_profile.ssid)
                _suppress_ssid(
                    state,
                    failed_profile.ssid,
                    observed_at=int(time.time()),
                    cooldown_seconds=fallback_failure_cooldown_seconds,
                )
            if not args.dry_run:
                _save_state(state_file, state)
            try:
                _activate_connection(ap_connection, ap_interface, dry_run=args.dry_run)
            except RuntimeError as recovery_exc:
                print(f"error={exc}; recovery={recovery_exc}", file=sys.stderr)
                return 1
            print(
                "action=reverted "
                f"connection={ap_connection} "
                "reason=fallback_activation_failed"
            )
            print(f"error={exc}", file=sys.stderr)
            return 1
        print(f"error={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
