"""Hermetic captive policy, consent and simulated host firewall restart."""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import portal_captive_policy as policy
import portal_firewall_coordinator as nft
import portal_authorization_reconcile as reconcile
import portal_nft_broker as broker
import ap_portal_server as portal

MAC = "aa:bb:cc:dd:ee:ff"
FOREIGN = "12:34:56:78:9a:bc"


def record(mac=MAC):
    return dict(mac_address=mac, accept_terms=True, terms_version="v1",
                accepted_at="2026-11-16T00:00:00Z",
                user_id=str(uuid.uuid4()), device_id=str(uuid.uuid4()),
                session_id=str(uuid.uuid4()))


def write_records(path, records):
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    path.chmod(0o600)


def rule(index):
    interface = {"match": {"op": "==", "left": {"meta": {"key": "iifname"}},
                           "right": "wlan0"}}
    if index == 3:
        expr = [interface, {"drop": None}]
    else:
        name = ("authorized_macs", "preauth_ipv4", "preauth_ipv6")[index]
        expr = [interface, {"match": {"op": "==",
                            "left": {"payload": {"protocol": "ether" if index == 0 else ("ip" if index == 1 else "ip6"),
                                                  "field": "saddr" if index == 0 else "daddr"}},
                            "right": "@" + name}}]
        if index in (1, 2):
            expr.append({"match": {"op": "==",
                        "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                        "right": {"set": [80, 443]}}})
        expr.append({"accept": None})
    return {"rule": {"family": "inet", "table": "gway_ap_portal",
                     "chain": "forward", "expr": expr}}


def captive_chain(*, remove_drop=False, priority=-5, widen=False):
    entries = [{"chain": {"family": "inet", "table": "gway_ap_portal",
                          "name": "forward", "type": "filter",
                          "hook": "forward", "prio": priority, "policy": "accept"}}]
    entries.extend(rule(i) for i in range(4))
    if remove_drop:
        entries.pop()
    if widen:
        entries[1]["rule"]["expr"][1] = {"counter": None}
    return json.dumps({"nftables": entries})


class FakeNft:
    def __init__(self, **kwargs):
        self.host = {FOREIGN}
        self.portal = set()
        self.chain = captive_chain(**kwargs)
        self.batches = []
        self.fail = False

    def __call__(self, argv, *, input=None):
        if argv[:4] == ["nft", "-j", "list", "chain"]:
            return self.chain
        if argv[:4] == ["nft", "-j", "list", "set"]:
            key = (argv[-2], argv[-1])
            if key == nft.SETS[0]:
                values = self.host
            elif key == nft.SETS[1]:
                values = self.portal
            else:
                raise AssertionError(key)
            return json.dumps({"nftables": [{"set": {
                "family": "inet", "table": key[0], "name": key[1],
                "type": "ether_addr", "elem": sorted(values)}}]})
        assert argv == ["nft", "-f", "-"]
        if self.fail:
            raise nft.CoordinationError("simulated atomic rejection")
        self.batches.append(input)
        for line in input.splitlines():
            parts = line.split()
            target = self.host if parts[3] == "host_security" else self.portal
            target.update(mac for mac in (x.strip(" {},") for x in parts[6:]) if mac)
        return ""


def test_registered_only_and_independent_grant_preserved(tmp_path):
    journal = tmp_path / "consents.jsonl"
    write_records(journal, [record()])
    grants = policy.eligible_grants({MAC, FOREIGN}, {FOREIGN},
                                    policy.read_consented_macs(journal))
    assert grants == {MAC}
    assert FOREIGN not in grants
    f = FakeNft()
    c = nft.NftGrantCoordinator(run=f, require_captive=True)
    c.add(grants)
    assert f.host == {FOREIGN, MAC}
    assert f.portal == {MAC}
    assert c.add(grants) == {key: set() for key in nft.SETS}
    assert len(f.batches) == 1
    assert all("flush" not in batch and "delete" not in batch for batch in f.batches)


def test_host_security_recreated_then_reconciled(tmp_path):
    write_records(tmp_path / "consents.jsonl", [record()])
    (tmp_path / "authorized_macs.txt").write_text(MAC + "\n")
    (tmp_path / "trusted_macs.txt").write_text(FOREIGN + "\n")
    f = FakeNft()
    coordinator = nft.NftGrantCoordinator(run=f, require_captive=True)
    reconcile.reconcile(tmp_path, coordinator=coordinator, require_consent=True)
    assert MAC in f.host and MAC in f.portal
    # A host-security restart deletes and recreates ONLY its host set.
    f.host = {FOREIGN}
    reconcile.reconcile(tmp_path, coordinator=coordinator, require_consent=True)
    assert f.host == {FOREIGN, MAC}
    assert f.portal == {MAC}
    assert len(f.batches) == 2


@pytest.mark.parametrize("kwargs", [{"remove_drop": True}, {"priority": 0},
                                     {"widen": True}])
def test_reject_unsafe_or_missing_forward_hook(kwargs):
    f = FakeNft(**kwargs)
    with pytest.raises(nft.CoordinationError, match="captive"):
        nft.NftGrantCoordinator(run=f, require_captive=True).add({MAC})
    assert f.host == {FOREIGN}
    assert f.portal == set()
    assert f.batches == []


def test_failed_batch_cannot_partially_grant(tmp_path):
    f = FakeNft()
    f.fail = True
    with pytest.raises(nft.CoordinationError):
        nft.NftGrantCoordinator(run=f, require_captive=True).add({MAC})
    assert f.host == {FOREIGN} and f.portal == set()


def test_uncertain_blocks_reconciliation(tmp_path):
    write_records(tmp_path / "consents.jsonl", [record()])
    (tmp_path / "authorized_macs.txt").write_text(MAC + "\n")
    (tmp_path / "authorization.pending").write_text('{"status":"uncertain"}\n')
    f = FakeNft()
    with pytest.raises(Exception, match="uncertain"):
        reconcile.reconcile(tmp_path, coordinator=nft.NftGrantCoordinator(
            run=f, require_captive=True), require_consent=True)
    assert f.batches == []


def test_legacy_and_incomplete_consent_fail_closed(tmp_path):
    path = tmp_path / "consents.jsonl"
    write_records(path, [{"mac_address": MAC, "accept_terms": True}])
    assert policy.read_consented_macs(path) == set()
    # A forged partial attribution record must not pass as legacy.
    write_records(path, [{"mac_address": MAC, "accept_terms": True,
                          "user_id": "not-a-uuid"}])
    with pytest.raises(nft.CoordinationError):
        policy.read_consented_macs(path)
    path.write_text(json.dumps(record()))  # incomplete trailing line
    with pytest.raises(nft.CoordinationError):
        policy.read_consented_macs(path)


def test_broker_strict_provenance_rejects_unregistered(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    (directory / "authorized_macs.txt").write_text(MAC + "\n")
    (directory / "authorized_macs.txt").chmod(0o600)
    with pytest.raises(broker.BrokerRejected, match="consent"):
        broker.verify_persisted_grants(directory, {MAC}, uid=os.getuid(),
                                       require_consent=True)
    write_records(directory / "consents.jsonl", [record()])
    broker.verify_persisted_grants(directory, {MAC}, uid=os.getuid(),
                                   require_consent=True)


def test_candidate_requires_broker_and_stages_restart_hook():
    unit = (ROOT / "ansible/templates/gway-ap-portal-candidate.service.j2").read_text()
    one_shot = (ROOT / "ansible/templates/gway-ap-host-security-reconcile-candidate.service.j2").read_text()
    hook = (ROOT / "ansible/templates/gway-ap-host-security-reconcile-dropin.conf").read_text()
    stage = (ROOT / "ansible/playbooks/stage-portal.yml").read_text()
    assert "--mandatory-registration" in unit
    assert "--coordinated-firewall" in unit and "--nft-broker-socket" in unit
    assert "--require-consent" in one_shot
    assert "After=host-security-firewall.service" in one_shot
    assert "ExecStartPost=/usr/bin/systemctl --no-block start" in hook
    assert "90-gway-ap-reconcile.conf" in stage
    assert "gway-ap-host-security-reconcile.service" in stage
    assert "state: started" not in stage
    assert "systemd_service:" not in stage


def test_cli_implements_staged_args(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ap_portal_server.py", "--coordinated-firewall",
                        "--mandatory-registration", "--nft-broker-socket",
                        "/run/gway-ap-nft/broker.sock"])
    args = portal.parse_args()
    assert args.coordinated_firewall and args.mandatory_registration
    assert args.nft_broker_socket.endswith("broker.sock")


def mandatory_state(tmp_path, monkeypatch, *, trusted=False, fail_grant=False):
    from dataclasses import replace
    state_dir = tmp_path / "private"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    if trusted:
        (state_dir / "trusted_macs.txt").write_text(MAC + "\n")
    config = portal.PortalConfig(
        bind="127.0.0.1", port=0, assets_dir=tmp_path, state_dir=state_dir,
        authorized_macs_path=state_dir / "authorized_macs.txt",
        trusted_macs_path=state_dir / "trusted_macs.txt",
        consents_path=state_dir / "consents.jsonl",
        activity_path=state_dir / "activity.jsonl", source_url="",
        sync_firewall=True, coordinated_firewall=True,
        mandatory_registration=True, nft_broker_socket="/tmp/fake-broker.sock")
    actions = []

    class Broker:
        def snapshot(self):
            actions.append("check")
            return {}
        def add(self, macs):
            actions.append(("add", set(macs)))
            if fail_grant:
                raise portal.FirewallSyncError("simulated broker rejection")

    b = Broker()
    monkeypatch.setattr(portal.PortalState, "_coordinator", lambda _self: b)
    state = portal.PortalState(config)
    state.resolve_mac = lambda _: MAC
    return state, actions


def test_mandatory_mode_denies_trusted_without_registration_then_grants(tmp_path, monkeypatch):
    state, actions = mandatory_state(tmp_path, monkeypatch, trusted=True)
    status = state.status_for_request(
        ip_address="10.42.0.25", user_agent="", path="/api/status", host="")
    assert status["authorized"] is False
    assert actions == ["check"]
    assert all(row["authorized"] is False for row in state.activity.client_summary())
    result = state.subscribe(email="registered@example.com", accept_terms=True,
                             ip_address="10.42.0.25", user_agent="", host="")
    assert result["authorized"] is True
    assert actions[-1] == ("add", {MAC})
    after = state.status_for_request(
        ip_address="10.42.0.25", user_agent="", path="/api/status", host="")
    assert after["authorized"] is True
    assert not (state.config.state_dir / "authorization.pending").exists()
    assert state.activity.client_summary()[0]["authorized"] is True


def test_mandatory_mode_rejected_broker_never_grants_and_retains_uncertain(tmp_path, monkeypatch):
    state, actions = mandatory_state(tmp_path, monkeypatch, fail_grant=True)
    with pytest.raises(portal.FirewallSyncError):
        state.subscribe(email="registered@example.com", accept_terms=True,
                        ip_address="10.42.0.25", user_agent="", host="")
    assert actions[-1] == ("add", {MAC})
    assert not state.config.consents_path.exists()
    assert not state.config.authorized_macs_path.exists()
    assert (state.config.state_dir / "authorization.pending").exists()
    with pytest.raises(Exception, match="uncertain"):
        state.subscribe(email="again@example.com", accept_terms=True,
                        ip_address="10.42.0.25", user_agent="", host="")


def test_mandatory_mode_rejects_absent_broker(tmp_path):
    from dataclasses import replace
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = portal.PortalConfig(
        bind="127.0.0.1", port=0, assets_dir=tmp_path, state_dir=state_dir,
        authorized_macs_path=state_dir / "authorized_macs.txt",
        trusted_macs_path=state_dir / "trusted_macs.txt",
        consents_path=state_dir / "consents.jsonl",
        activity_path=state_dir / "activity.jsonl", source_url="",
        sync_firewall=True, coordinated_firewall=True,
        mandatory_registration=True)
    with pytest.raises(portal.FirewallSyncError, match="requires coordinated broker"):
        portal.PortalState(config)


def test_kernel_nft_1_0_6_emits_literal_set_reference():
    # Observed from nft -j list chain in a real isolated netns on gway-001.
    parsed = json.loads(captive_chain())
    assert parsed["nftables"][1]["rule"]["expr"][1]["match"]["right"] == "@authorized_macs"
    assert parsed["nftables"][2]["rule"]["expr"][1]["match"]["right"] == "@preauth_ipv4"
    assert parsed["nftables"][3]["rule"]["expr"][1]["match"]["right"] == "@preauth_ipv6"
    f = FakeNft()
    nft.NftGrantCoordinator(run=f, require_captive=True).verify_captive()


def test_reject_nonliteral_or_different_nft_set_reference():
    for forged in ({"set": "@authorized_macs"}, "@wrong_set", "", None):
        f = FakeNft()
        raw = json.loads(f.chain)
        raw["nftables"][1]["rule"]["expr"][1]["match"]["right"] = forged
        f.chain = json.dumps(raw)
        with pytest.raises(nft.CoordinationError, match="captive"):
            nft.NftGrantCoordinator(run=f, require_captive=True).add({MAC})
        assert f.batches == []
