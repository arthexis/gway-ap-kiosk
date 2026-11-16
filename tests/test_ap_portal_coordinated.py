"""Offline integrated consent-first tests; no host nft or service calls."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("ap_portal_server_coordinated", ROOT / "scripts/ap_portal_server.py")
portal = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = portal
spec.loader.exec_module(portal)
MAC = "aa:bb:cc:dd:ee:ff"


def state_for(tmp_path, monkeypatch, *, failed=False):
    directory = tmp_path / "state"
    config = portal.PortalConfig(
        bind="127.0.0.1", port=0, assets_dir=tmp_path, state_dir=directory,
        authorized_macs_path=directory / "authorized_macs.txt",
        trusted_macs_path=directory / "trusted_macs.txt",
        consents_path=directory / "consents.jsonl",
        activity_path=directory / "activity.jsonl",
        source_url="", sync_firewall=False, coordinated_firewall=True)
    state = portal.PortalState(config)
    state.config = replace(config, sync_firewall=True)
    state.resolve_mac = lambda _: MAC
    events = []

    class Coordinator:
        def add(self, macs):
            events.append(("firewall", set(macs)))
            if failed:
                raise portal.FirewallSyncError("simulated atomic nft failure")

    monkeypatch.setattr(state, "_coordinator", lambda: Coordinator())
    orig_consent = state._append_consent
    def consent(record):
        events.append(("consent", None))
        orig_consent(record)
    monkeypatch.setattr(state, "_append_consent", consent)
    orig_authorized = state._write_authorized_macs
    def authorization(macs):
        events.append(("file", set(macs)))
        orig_authorized(macs)
    monkeypatch.setattr(state, "_write_authorized_macs", authorization)
    return state, events


def submit(state):
    return state.subscribe(email="unit@example.invalid", accept_terms=True,
                           ip_address="10.42.0.10", user_agent="pytest", host="ap.local")


def test_consent_then_file_then_atomic_firewall(tmp_path, monkeypatch):
    state, events = state_for(tmp_path, monkeypatch)
    assert submit(state)["authorized"] is True
    assert [e[0] for e in events] == ["consent", "file", "firewall"]
    assert MAC in state.config.authorized_macs_path.read_text()
    assert len(state.config.consents_path.read_text().splitlines()) == 1
    assert len(state.config.activity_path.read_text().splitlines()) == 1
    assert submit(state)["already_authorized"] is True
    assert events[-1][0] == "firewall"


def test_atomic_batch_failure_restores_logs_and_authorization(tmp_path, monkeypatch):
    state, events = state_for(tmp_path, monkeypatch, failed=True)
    with pytest.raises(portal.FirewallSyncError):
        submit(state)
    assert not state.config.authorized_macs_path.exists()
    assert not state.config.consents_path.exists()
    assert not state.config.activity_path.exists()
    assert state._authorized == set()


def test_preserves_existing_independent_file_and_consent_on_failure(tmp_path, monkeypatch):
    state, _ = state_for(tmp_path, monkeypatch, failed=True)
    other = "12:34:56:78:9a:bc"
    state.config.authorized_macs_path.write_text(other + "\n")
    state.config.consents_path.write_text("previous-record\n")
    state.config.activity_path.write_text("previous-activity\n")
    with pytest.raises(portal.FirewallSyncError):
        submit(state)
    assert state.config.authorized_macs_path.read_text() == other + "\n"
    assert state.config.consents_path.read_text() == "previous-record\n"
    assert state.config.activity_path.read_text() == "previous-activity\n"


def test_missing_consent_storage_never_grants(tmp_path, monkeypatch):
    state, events = state_for(tmp_path, monkeypatch)
    def fail(_):
        raise OSError("write failed")
    monkeypatch.setattr(state, "_append_consent", fail)
    with pytest.raises(OSError):
        submit(state)
    assert all(name != "firewall" for name, _ in events)


def test_startup_requires_coordinator_snapshot_even_without_grants(tmp_path, monkeypatch):
    monkeypatch.setattr(portal.PortalState, "_coordinator", lambda self: type(
        "Fail", (), {"snapshot": lambda _: (_ for _ in ()).throw(
            portal.FirewallSyncError("missing set"))})())
    directory = tmp_path / "state"
    config = portal.PortalConfig(
        bind="127.0.0.1", port=0, assets_dir=tmp_path, state_dir=directory,
        authorized_macs_path=directory / "authorized_macs.txt",
        trusted_macs_path=directory / "trusted_macs.txt",
        consents_path=directory / "consents.jsonl", activity_path=directory / "activity.jsonl",
        source_url="", sync_firewall=True, coordinated_firewall=True)
    with pytest.raises(portal.FirewallSyncError):
        portal.PortalState(config)


def test_ambiguous_broker_response_preserves_uncertainty(tmp_path, monkeypatch):
    state, events = state_for(tmp_path, monkeypatch, failed=True)
    with pytest.raises(portal.FirewallSyncError):
        submit(state)
    assert (state.config.state_dir / "authorization.pending").is_file()
    # Files are compensated, but a lost reply does not establish that nft
    # did not apply the grant. Restart must fail closed.
    assert not state.config.authorized_macs_path.exists()
    assert not state.config.consents_path.exists()
    with pytest.raises(Exception, match="uncertain"):
        submit(state)
    assert [item[0] for item in events].count("firewall") == 1


def test_pre_broker_write_failure_can_clear_intent(tmp_path, monkeypatch):
    state, _events = state_for(tmp_path, monkeypatch)
    def fail(_):
        raise OSError("synthetic consent I/O failure")
    monkeypatch.setattr(state, "_append_consent", fail)
    with pytest.raises(OSError):
        submit(state)
    assert not (state.config.state_dir / "authorization.pending").exists()
