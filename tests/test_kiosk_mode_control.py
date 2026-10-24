"""Regression tests for the Bastion-gated kiosk-mode page control."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "kiosk/bin/gway-kiosk-status"
loader = importlib.machinery.SourceFileLoader("kiosk_mode_control_test", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
kiosk = importlib.util.module_from_spec(spec)
loader.exec_module(kiosk)


def payload(key_present):
    return {
        "ap": {"ssid": "", "interface": "", "updated_at": ""},
        "rfid": {"installed": False, "card": "Not Installed", "seen_at": ""},
        "kiosk": {"key_present": key_present},
    }


def test_kiosk_mode_off_control_only_renders_with_verified_key():
    assert kiosk.KIOSK_MODE_OFF_PATH not in kiosk.render_html(payload(False))
    page = kiosk.render_html(payload(True))
    assert kiosk.KIOSK_MODE_OFF_PATH in page
    assert "Kiosk Mode Off" in page
    assert 'id="kiosk-mode-off-form"' in page
    assert "event.preventDefault()" in page


def test_kiosk_mode_off_uses_only_the_existing_command(monkeypatch, tmp_path):
    command_path = tmp_path / "kiosk-mode"
    command_path.touch(mode=0o755)
    monkeypatch.setattr(kiosk, "KIOSK_MODE_COMMAND", command_path)
    monkeypatch.setattr(kiosk, "bastion_key_present", lambda: True)
    with patch.object(kiosk.subprocess, "run") as run:
        run.return_value.returncode = 0
        assert kiosk.disable_kiosk_mode()
    assert run.call_args.args[0] == [str(kiosk.KIOSK_MODE_COMMAND), "off"]
    assert run.call_args.kwargs["capture_output"]
    assert run.call_args.kwargs["env"]["KIOSK_MODE_SYSTEMCTL_DIRECT"] == "1"


def test_kiosk_mode_direct_systemctl_path_is_opt_in_only():
    source = (Path(__file__).resolve().parents[1] / "kiosk/bin/kiosk-mode").read_text()
    assert 'KIOSK_MODE_SYSTEMCTL_DIRECT:-0' in source
    assert 'elif [[ "${KIOSK_MODE_SYSTEMCTL_DIRECT:-0}" == "1" ]]' in source


def test_kiosk_mode_off_post_requires_same_origin_and_key():
    handler = object.__new__(kiosk.KioskStatusHandler)
    handler.path = kiosk.KIOSK_MODE_OFF_PATH
    handler.client_address = ("127.0.0.1", 10000)
    handler.headers = {"Origin": "http://127.0.0.1:8899", "Host": "127.0.0.1:8899"}
    sent = []
    handler._send = lambda status, content_type, body: sent.append((status, json.loads(body)))
    with patch.object(kiosk, "disable_kiosk_mode", return_value=True) as command:
        handler.do_POST()
    assert sent == [(200, {"ok": True})]
    command.assert_called_once_with()

    handler.headers = {"Origin": "https://example.org", "Host": "127.0.0.1:8899"}
    sent.clear()
    with patch.object(kiosk, "disable_kiosk_mode") as command:
        handler.do_POST()
    assert sent == [(403, {"ok": False})]
    command.assert_not_called()
