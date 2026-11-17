"""Offline attribution and integration tests. No network, nft or devices."""
import importlib.util
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    obj = importlib.util.module_from_spec(spec)
    sys.modules[name] = obj
    spec.loader.exec_module(obj)
    return obj


ident = load("portal_attribution")
portal = load("ap_portal_server")
A = "aa:bb:cc:dd:ee:ff"
B = "12:34:56:78:9a:bc"


def make_state(tmp_path):
    state_dir = tmp_path / "private"
    config = portal.PortalConfig(
        bind="127.0.0.1", port=0, assets_dir=tmp_path,
        state_dir=state_dir,
        authorized_macs_path=state_dir / "authorized_macs.txt",
        trusted_macs_path=state_dir / "trusted_macs.txt",
        consents_path=state_dir / "consents.jsonl",
        activity_path=state_dir / "activity.jsonl", source_url="",
        sync_firewall=False)
    return portal.PortalState(config)


def subscribe(state, email, mac=A, ip="10.42.0.25"):
    state.resolve_mac = lambda _: mac
    return state.subscribe(email=email, accept_terms=True, ip_address=ip,
                           user_agent="test", host="portal.test")


def consent_rows(state):
    return [json.loads(l) for l in state.config.consents_path.read_text().splitlines()]


def activity_rows(state):
    return [json.loads(l) for l in state.config.activity_path.read_text().splitlines()]


def test_persistent_user_device_and_fresh_session_across_restart(tmp_path):
    state = make_state(tmp_path)
    subscribe(state, "User@Example.com")
    first = consent_rows(state)[-1]
    assert all(str(uuid.UUID(first[k])) == first[k] for k in
               ("user_id", "device_id", "session_id"))
    assert "user_id" in activity_rows(state)[-1]
    state = make_state(tmp_path)
    subscribe(state, "user@example.com", ip="10.42.0.99")
    second = consent_rows(state)[-1]
    assert first["user_id"] == second["user_id"]
    assert first["device_id"] == second["device_id"]
    assert first["session_id"] != second["session_id"]
    state.status_for_request(ip_address="10.42.0.99", user_agent="test",
                             path="/api/status", host="portal.test")
    event = activity_rows(state)[-1]
    assert event["user_id"] == first["user_id"]
    assert event["session_id"] == second["session_id"]
    assert event["attribution_basis"] == "last_device_consent"
    assert state.activity.client_summary()[0]["user_id"] == first["user_id"]


def test_shared_device_changes_last_consent_but_not_device_id(tmp_path):
    state = make_state(tmp_path)
    subscribe(state, "first@example.com")
    first = consent_rows(state)[-1]
    subscribe(state, "second@example.com")
    second = consent_rows(state)[-1]
    assert first["user_id"] != second["user_id"]
    assert first["device_id"] == second["device_id"]
    state.record_request(ip_address="10.42.0.25", user_agent="", method="GET",
                         path="/", host="", referer="")
    assert activity_rows(state)[-1]["user_id"] == second["user_id"]


def test_same_email_different_mac_is_distinct_device(tmp_path):
    state = make_state(tmp_path)
    subscribe(state, "same@example.com", mac=A)
    first = consent_rows(state)[-1]
    subscribe(state, "same@example.com", mac=B)
    second = consent_rows(state)[-1]
    assert first["user_id"] == second["user_id"]
    assert first["device_id"] != second["device_id"]


def test_legacy_untagged_consent_remains_unattributed(tmp_path):
    state = make_state(tmp_path)
    state.config.consents_path.write_text(
        json.dumps({"email": "old@example.com", "mac_address": A,
                    "accept_terms": True}) + "\n")
    assert ident.latest_device_attribution(state.activity.read_consents(), A) == {}
    state.resolve_mac = lambda _: A
    state.record_request(ip_address="10.42.0.25", user_agent="", method="GET",
                         path="/", host="", referer="")
    assert "user_id" not in activity_rows(state)[-1]


def test_no_attribution_from_ip_without_mac(tmp_path):
    state = make_state(tmp_path)
    subscribe(state, "one@example.com")
    state.resolve_mac = lambda _: None
    state.record_request(ip_address="10.42.0.25", user_agent="", method="GET",
                         path="/", host="", referer="")
    assert "user_id" not in activity_rows(state)[-1]


def test_no_consent_does_not_create_identity_records(tmp_path):
    state = make_state(tmp_path)
    state.resolve_mac = lambda _: A
    import pytest
    with pytest.raises(ValueError):
        state.subscribe(email="a@example.com", accept_terms=False,
                        ip_address="10.42.0.25", user_agent="", host="")
    assert not state.config.consents_path.exists()
    assert not state.config.authorized_macs_path.exists()


def test_forged_or_invalid_uuid_never_attributed():
    old = {"accept_terms": True, "email": "x@example.com",
           "mac_address": A, "user_id": "../../path",
           "device_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4())}
    assert ident.latest_device_attribution([old], A) == {}
    assert ident.latest_device_attribution([old], None) == {}
    new = ident.registration_ids([old], email="x@example.com", mac_address=A)
    assert new["user_id"] != old["user_id"]
