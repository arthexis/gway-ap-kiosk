"""Behavioral safety checks for AP credential handling without real hardware."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

script = Path(__file__).resolve().parents[1] / "scripts/ap/kiosk-ap"
loader = importlib.machinery.SourceFileLoader("kiosk_ap_security", str(script))
spec = importlib.util.spec_from_loader(loader.name, loader)
ap_module = importlib.util.module_from_spec(spec)
loader.exec_module(ap_module)


def test_unprivileged_credential_operations_never_query_nm(monkeypatch, capsys):
    monkeypatch.setattr(ap_module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(ap_module.sys, "argv", ["kiosk-ap", "reveal"])
    monkeypatch.setattr(ap_module, "profiles", lambda: pytest.fail("unexpected NM access"))
    assert ap_module.main() == 1
    assert "Root privileges required" in capsys.readouterr().err


def test_nm_modify_failure_does_not_reapply_or_leak_password(monkeypatch):
    commands = []
    class Failure:
        returncode = 1
        stderr = "sensitive backend details"
    def fake_run(command, **kwargs):
        commands.append(command)
        return Failure()
    monkeypatch.setattr(ap_module.subprocess, "run", fake_run)
    with pytest.raises(ap_module.DiscoveryError, match="Unable to update selected AP credentials") as error:
        ap_module.rotate_password({"uuid": "chosen", "active": True, "interface": "wifi7"})
    assert len(commands) == 1
    assert "sensitive backend details" not in str(error.value)
    assert "reapply" not in commands[0]
