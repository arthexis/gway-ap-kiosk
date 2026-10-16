"""Behavioral tests for generic AP selection, without NetworkManager hardware."""
import importlib.machinery
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "scripts/ap/kiosk-ap"
loader = importlib.machinery.SourceFileLoader("kiosk_ap", str(path))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)


def ap(uuid, active=False):
    return {"uuid": uuid, "active": active, "name": uuid, "ssid": uuid, "interface": None}


def test_empty_configuration_is_not_an_error():
    assert module.discover([])["state"] == "not_configured"


def test_unique_inactive_profile_is_selected_without_activation():
    assert module.discover([ap("a")])["ap"]["uuid"] == "a"


def test_unique_active_profile_wins():
    assert module.discover([ap("a"), ap("b", True)])["ap"]["uuid"] == "b"


def test_multiple_active_profiles_are_ambiguous():
    assert module.discover([ap("a", True), ap("b", True)])["state"] == "ambiguous"


def test_multiple_inactive_profiles_are_ambiguous():
    assert module.discover([ap("a"), ap("b")])["state"] == "ambiguous"


def test_explicit_uuid_overrides_active_profile():
    assert module.discover([ap("a", True), ap("b")], "b")["ap"]["uuid"] == "b"


def test_invalid_explicit_uuid_does_not_fall_back():
    assert module.discover([ap("a", True)], "missing")["state"] == "invalid_selection"


def test_nmcli_is_read_only(monkeypatch):
    calls = []
    class Result:
        returncode = 0
        stderr = ""
        stdout = ""
    def run(command, **kwargs):
        calls.append(command)
        if command[-3:] == ["connection", "show", "--active"]:
            return Result()
        if command[-2:] == ["connection", "show"]:
            return Result()
        return Result()
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.profiles() == []
    assert all("modify" not in c and "up" not in c and "down" not in c for c in calls)


def test_profiles_from_mock_networkmanager(monkeypatch):
    responses = {
        ("-g", "UUID", "connection", "show"): "wifi-client\nap-one\nap-two",
        ("-g", "connection.type", "connection", "show", "uuid", "wifi-client"): "802-11-wireless",
        ("-g", "802-11-wireless.mode", "connection", "show", "uuid", "wifi-client"): "infrastructure",
        ("-g", "connection.type", "connection", "show", "uuid", "ap-one"): "802-11-wireless",
        ("-g", "802-11-wireless.mode", "connection", "show", "uuid", "ap-one"): "ap",
        ("-g", "connection.id", "connection", "show", "uuid", "ap-one"): "Office:AP",
        ("-g", "802-11-wireless.ssid", "connection", "show", "uuid", "ap-one"): "Office WiFi",
        ("-g", "connection.type", "connection", "show", "uuid", "ap-two"): "802-11-wireless",
        ("-g", "802-11-wireless.mode", "connection", "show", "uuid", "ap-two"): "ap",
        ("-g", "connection.id", "connection", "show", "uuid", "ap-two"): "Backup",
        ("-g", "802-11-wireless.ssid", "connection", "show", "uuid", "ap-two"): "Backup SSID",
        ("-g", "UUID,DEVICE", "connection", "show", "--active"): "ap-one:wlan0",
    }
    monkeypatch.setattr(module, "nmcli", lambda *args: responses[args])
    items = module.profiles()
    assert len(items) == 2
    assert module.discover(items)["ap"] == {
        "uuid": "ap-one", "name": "Office:AP", "ssid": "Office WiFi",
        "active": True, "interface": "wlan0",
    }


def test_networkmanager_failure_is_not_hidden(monkeypatch):
    class Result:
        returncode = 10
        stderr = "NetworkManager is not running"
        stdout = ""
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())
    import pytest
    with pytest.raises(module.DiscoveryError, match="not running"):
        module.profiles()


def test_rotate_changes_only_selected_profile_password(monkeypatch):
    calls = []
    class Result:
        returncode = 0
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs: (calls.append(command), Result())[1])
    chosen = ap("chosen", True)\n    chosen["interface"] = "wlan7"\n    password = module.rotate_password(chosen)
    assert len(password) == 16
    assert calls == [\n        ["nmcli", "connection", "modify", "uuid", "chosen", "802-11-wireless-security.psk", password],\n        ["nmcli", "device", "reapply", "wlan7"],\n    ]


def test_rotate_refuses_inactive_profile(monkeypatch):
    import pytest
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not modify NetworkManager"))
    with pytest.raises(module.DiscoveryError, match="active"):
        module.rotate_password(ap("inactive"))


def test_reveal_uses_selected_uuid(monkeypatch):
    calls = []
    def fake_nmcli(*args):
        calls.append(args)
        return "existing-secret"
    monkeypatch.setattr(module, "nmcli", fake_nmcli)
    assert module.reveal_password(ap("chosen")) == "existing-secret"
    assert calls == [("-s", "-g", "802-11-wireless-security.psk", "connection", "show", "uuid", "chosen")]


def test_failed_reapply_does_not_restart_ap_or_disclose_password(monkeypatch):
    import pytest
    calls = []
    class Result:
        def __init__(self, returncode):
            self.returncode = returncode
    def run(command, **kwargs):
        calls.append(command)
        return Result(1 if command[1:3] == ["device", "reapply"] else 0)
    monkeypatch.setattr(module.subprocess, "run", run)
    chosen = ap("chosen", True)
    chosen["interface"] = "wlan7"
    with pytest.raises(module.DiscoveryError, match="activation is pending"):
        module.rotate_password(chosen)
    assert [command[1:3] for command in calls] == [
        ["connection", "modify"], ["device", "reapply"],
    ]


def test_active_ap_without_interface_is_not_modified(monkeypatch):
    import pytest
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not modify NetworkManager"))
    with pytest.raises(module.DiscoveryError, match="known interface"):
        module.rotate_password(ap("chosen", True))
