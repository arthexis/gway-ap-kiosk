from __future__ import annotations

import json

from scripts.gway import arthexis_wifi_role as wifi_role


def test_env_csv_uses_default_when_empty(monkeypatch):
    monkeypatch.delenv("PREFERRED_CLIENT_CONNECTIONS", raising=False)

    assert wifi_role._env_csv("PREFERRED_CLIENT_CONNECTIONS", ("a", "b")) == [
        "a",
        "b",
    ]


def test_env_csv_strips_blank_tokens(monkeypatch):
    monkeypatch.setenv("PREFERRED_CLIENT_CONNECTIONS", " first, ,second ,, ")

    assert wifi_role._env_csv("PREFERRED_CLIENT_CONNECTIONS", ()) == [
        "first",
        "second",
    ]


def test_load_state_normalizes_integer_buckets(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "seen_ssids": {"field-uplink": "123", "bad": "not-int"},
                "suppressed_until": {"field-uplink": 456},
                "last_connected_scan_at": "789",
            }
        ),
        encoding="utf-8",
    )

    assert wifi_role._load_state(state_path) == {
        "seen_ssids": {"field-uplink": 123},
        "suppressed_until": {"field-uplink": 456},
        "last_connected_scan_at": 789,
    }


def test_visible_fallback_respects_suppression():
    profiles = [
        wifi_role.PreferredConnection(name="primary", ssid="primary-ssid"),
        wifi_role.PreferredConnection(name="backup", ssid="backup-ssid"),
    ]
    state = {"suppressed_until": {"primary-ssid": 200}}

    assert wifi_role._visible_fallback_connection(
        profiles,
        {"primary-ssid", "backup-ssid"},
        state,
        observed_at=100,
    ) == ("backup", "backup-ssid")


def test_recent_fallback_respects_ttl():
    profiles = [
        wifi_role.PreferredConnection(name="old", ssid="old-ssid"),
        wifi_role.PreferredConnection(name="fresh", ssid="fresh-ssid"),
    ]
    state = {"seen_ssids": {"old-ssid": 10, "fresh-ssid": 90}}

    assert wifi_role._recently_seen_fallback_connection(
        profiles,
        state,
        observed_at=100,
        ttl_seconds=30,
    ) == ("fresh", "fresh-ssid", 10)


def test_scan_candidates_include_wifi_interfaces_only():
    statuses = {
        "wlan0": wifi_role.DeviceStatus("wlan0", "wifi", "connected", "ap"),
        "eth0": wifi_role.DeviceStatus("eth0", "ethernet", "connected", "wired"),
    }

    assert wifi_role._scan_candidates(
        statuses,
        scan_interface="eth0",
        ap_interface="wlan0",
    ) == ["wlan0"]


def test_public_defaults_do_not_name_private_fallback_profiles():
    assert wifi_role.DEFAULT_PREFERRED_CLIENT_CONNECTIONS == ()
