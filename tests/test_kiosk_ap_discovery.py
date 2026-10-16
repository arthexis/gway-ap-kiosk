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
