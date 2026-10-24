"""Behavior tests for independent kiosk status (no external suite integrations)."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "kiosk/bin/gway-kiosk-status"
loader = importlib.machinery.SourceFileLoader("standalone_kiosk", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
kiosk = importlib.util.module_from_spec(spec)
loader.exec_module(kiosk)


def test_missing_ap_state_still_renders():
    assert kiosk.build_payload()["ap"]["available"] is False
    assert 'aria-label="System status"' in kiosk.render_html(kiosk.build_payload())

def test_ap_credentials_never_disclosed_in_json_or_html():
    response = json.dumps({"state": "selected", "ap": {
        "ssid": "TEST-AP", "interface": "wlan0", "password": "super-private-test-value",
    }})
    with patch.object(Path, "is_file", return_value=True), patch.object(kiosk.subprocess, "run") as run:
        run.return_value.stdout = response
        payload = kiosk.probe_ap()
    assert payload["ssid"] == "TEST-AP"
    assert "super-private-test-value" not in json.dumps(payload)
    assert "super-private-test-value" not in kiosk.render_html({
        "ap": payload, "rfid": {"installed": False, "card": "Not Installed", "seen_at": ""},
    })

def test_ap_observation_has_a_timestamp_after_successful_status_read():
    response = json.dumps({"state": "selected", "ap": {"ssid": "TEST-AP", "interface": "wlan0"}})
    with patch.object(Path, "is_file", return_value=True), patch.object(kiosk.subprocess, "run") as run:
        run.return_value.stdout = response
        payload = kiosk.probe_ap()
    assert payload["updated_at"]
    assert run.call_args.kwargs["timeout"] == kiosk.AP_PROBE_TIMEOUT

def test_dashboard_has_four_blocks_and_an_offline_signup_qr_without_credentials():
    page = kiosk.render_html({"ap": {
        "ssid": "TEST-AP", "interface": "wlan0", "updated_at": "2026-10-22T12:34:56Z",
    }, "rfid": {"installed": True, "card": "A1B2C3D4", "seen_at": "2026-10-22T12:00:00Z"},
    })
    assert 'class="panel ap-panel"' in page
    assert page.count('class="panel') == 4
    assert "GWAY Kiosk" not in page
    assert "AP signup" in page
    assert "<h2" not in page
    assert "<h3" not in page
    assert 'aria-label="System status"' in page
    assert kiosk.AP_SIGNUP_URL in page
    assert 'class="signup-qr"' in page
    assert "password" not in page.lower()


def test_system_status_reuses_published_lcd_frames(tmp_path, monkeypatch):
    source = tmp_path / "lcd-system-info.json"
    source.write_text(json.dumps({"frames": [
        {"key": "host", "line1": "HOST gway-001", "line2": "field up 1d2h"},
        {"key": "health", "line1": "HEALTH 48C", "line2": "L0.12 RAM20%"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(kiosk, "SYSTEM_STATUS_FILE", source)

    context = kiosk.system_status_context()

    assert context == {
        "available": True,
        "frames": [
            {"line1": "HOST gway-001", "line2": "field up 1d2h"},
            {"line1": "HEALTH 48C", "line2": "L0.12 RAM20%"},
        ],
        "message": "",
    }


def test_missing_system_status_is_safe_and_page_escapes_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(kiosk, "SYSTEM_STATUS_FILE", tmp_path / "missing")
    assert kiosk.system_status_context() == {
        "available": False, "frames": [], "message": "System status unavailable"
    }
    page = kiosk.render_html({
        "ap": {"ssid": "", "interface": "", "updated_at": ""},
        "rfid": {"installed": False, "card": "Not Installed", "seen_at": ""},
        "system": {"available": True, "message": "", "frames": [
            {"line1": "<unsafe>", "line2": "ok"},
        ]},
    })
    assert "&lt;unsafe&gt;" in page
    assert "\\u003cunsafe\\u003e" in page


def test_rfid_not_installed_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(kiosk, "RFID_PUBLISHER_COMMAND", tmp_path / "rfid-scan-publisher")
    assert kiosk.rfid_context() == {"installed": False, "card": "Not Installed", "seen_at": ""}


def test_rfid_card_comes_from_the_latest_published_snapshot(monkeypatch, tmp_path):
    publisher = tmp_path / "rfid-scan-publisher"
    publisher.touch()
    scan = tmp_path / "rfid-scan.json"
    scan.write_text(json.dumps({"uid": "A1B2C3D4", "seen_at": "2026-10-22T12:00:00Z"}))
    monkeypatch.setattr(kiosk, "RFID_PUBLISHER_COMMAND", publisher)
    monkeypatch.setattr(kiosk, "RFID_SCAN_FILE", scan)
    assert kiosk.rfid_context() == {"installed": True, "card": "A1B2C3D4", "seen_at": "2026-10-22T12:00:00Z"}


def test_ap_context_returns_cache_while_refresh_runs(monkeypatch):
    expected = {"available": True, "ssid": "TEST-AP", "interface": "wlan0", "updated_at": "2026-10-22T12:34:56Z", "state": "selected"}
    snapshot = kiosk.APStatusSnapshot(clock=lambda: 10.0)
    snapshot.update(expected)
    monkeypatch.setattr(kiosk, "AP_SNAPSHOT", snapshot)
    with patch.object(kiosk.subprocess, "run") as run:
        assert kiosk.ap_context() == expected
    run.assert_not_called()

def test_no_external_controls_in_page(monkeypatch):
    monkeypatch.delenv("GWAY_KIOSK_ALLOW_AP_ROTATION", raising=False)
    html = kiosk.render_html({"ap": {
        "ssid": "TEST-AP", "interface": "wlan0", "updated_at": "2026-10-22T12:34:56Z",
    }, "rfid": {"installed": False, "card": "Not Installed", "seen_at": ""},
    })
    for external in ("iframe", "ocpp", "arthexis", "lcd", "vnc", "/kiosk/console/", "restart"):
        assert external not in html.lower()


def test_unknown_post_does_not_invoke_rotation():
    handler = object.__new__(kiosk.KioskStatusHandler)
    handler.path = "/kiosk/status/services/example.service/restart"
    handler.client_address = ("127.0.0.1", 10000)
    sent = []
    handler._send = lambda status, kind, body: sent.append(status)
    with patch.object(kiosk, "reset_access_point") as rotate:
        handler.do_POST()
    assert sent == [404]
    rotate.assert_not_called()


def test_rotation_not_display_dependent():
    with patch.object(Path, "is_file", return_value=True), patch.object(kiosk.os, "access", return_value=True), patch.object(kiosk.subprocess, "run") as run:
        run.return_value.returncode = 0
        assert kiosk.reset_access_point()
    assert run.call_args.args[0] == ["sudo", "-n", str(kiosk.AP_ROTATE_COMMAND), "rotate"]
