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


def test_missing_ap_state_still_renders(monkeypatch, tmp_path):
    monkeypatch.setenv("GWAY_KIOSK_AP_PASSWORD_FILE", str(tmp_path / "absent"))
    assert kiosk.build_payload()["ap"]["available"] is False
    assert "GWAY Kiosk" in kiosk.render_html(kiosk.build_payload())


def test_ap_credentials_never_disclosed_in_json_or_html(monkeypatch, tmp_path):
    credentials = tmp_path / "ap-state"
    credentials.write_text("SSID=TEST-AP\nPASSWORD=super-private-test-value\nINTERFACE=wlan0\n")
    monkeypatch.setenv("GWAY_KIOSK_AP_PASSWORD_FILE", str(credentials))
    payload = kiosk.build_payload()
    assert payload["ap"]["ssid"] == "TEST-AP"
    assert "super-private-test-value" not in json.dumps(payload)
    assert "super-private-test-value" not in kiosk.render_html(payload)


def test_no_external_controls_in_page(monkeypatch):
    monkeypatch.delenv("GWAY_KIOSK_ALLOW_AP_ROTATION", raising=False)
    html = kiosk.render_html(kiosk.build_payload())
    for external in ("iframe", "ocpp", "arthexis", "rfid", "lcd", "vnc", "/kiosk/console/", "restart"):
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
    assert run.call_args.args[0] == [str(kiosk.AP_ROTATE_COMMAND)]
