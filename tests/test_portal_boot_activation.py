"""Hermetic fail-closed AP boot/reload and protected activation contracts."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import portal_boot_rules as boot
import portal_host_grants as grants
import portal_nft_reload as reload
import portal_firewall_coordinator as coord

MAC = "aa:bb:cc:dd:ee:ff"
FOREIGN = "12:34:56:78:9a:bc"
BASE = ('#!/usr/sbin/nft -f\n'
        '# use existing distribution firewall\n'
        'flush ruleset\n\n'
        'table inet filter { chain input { type filter hook input priority 0; policy accept; } }\n'
        'include "/etc/ocpp-discover/nftables.conf"\n')


def host_source(*macs):
    return ("table inet host_security {\n"
            " set ap_allowed_macs {\n"
            "  type ether_addr\n"
            "  elements = { " + ", ".join(macs) + " }\n"
            " }\n"
            "}\n")


def host_live(*macs):
    return json.dumps({"nftables": [{"set": {
        "family": "inet", "table": "host_security", "name": "ap_allowed_macs",
        "type": "ether_addr", "elem": list(macs)}}]})


def test_boot_includes_are_after_flush_and_idempotent():
    result = boot.render_boot(BASE)
    assert result.startswith(BASE)
    assert result.index("flush ruleset") < result.index(boot.HOST_SOURCE)
    assert result.index(boot.HOST_SOURCE) < result.index(boot.PORTAL_SOURCE)
    assert result.count(boot.BEGIN) == 1
    assert boot.render_boot(result) == result
    assert boot.remove_managed_boot(result, BASE) == BASE
    assert "table inet gway_ap_portal" in boot.render_captive()
    assert 'iifname "wlan0" drop' in boot.render_captive()


@pytest.mark.parametrize("unsafe", [
    "", "table inet filter {}\n",
    "flush ruleset\nflush ruleset\n",
    "table inet filter {}\nflush ruleset\n",
    BASE + '\ninclude "/etc/nftables.d/*.nft"\n',
    BASE + '\ninclude "/etc/nftables.d/host-security.nft"\n',
    BASE + "\ntable inet gway_ap_portal {}\n",
    BASE + "\n" + boot.BEGIN + "\n",
])
def test_boot_rejects_unmanaged_policy_drift(unsafe):
    with pytest.raises(boot.BootRulesError):
        boot.render_boot(unsafe)


def test_boot_rollback_refuses_interleaved_updates():
    result = boot.render_boot(BASE)
    with pytest.raises(boot.BootRulesError, match="drift"):
        boot.remove_managed_boot(result + "# another writer\n", BASE)


def test_private_input_rejects_symlink(tmp_path):
    original = tmp_path / "nft.conf"
    original.write_text(BASE)
    link = tmp_path / "link"
    link.symlink_to(original)
    with pytest.raises(boot.BootRulesError, match="symlink"):
        boot._read_regular(link)


def test_static_host_grants_exact_parity_with_live():
    assert grants.compare_source_and_live(
        host_source(MAC, FOREIGN), host_live(FOREIGN, MAC)) == 2
    with pytest.raises(grants.HostGrantError, match="differ"):
        grants.compare_source_and_live(host_source(MAC), host_live(MAC, FOREIGN))
    with pytest.raises(grants.HostGrantError, match="differ"):
        grants.compare_source_and_live(host_source(MAC, FOREIGN), host_live(MAC))


@pytest.mark.parametrize("static", [
    "table inet host_security { set ap_allowed_macs { type ether_addr; } }",
    host_source("not-a-mac"),
    host_source(MAC, MAC),
    host_source(MAC).replace("ether_addr", "ipv4_addr"),
    host_source(MAC).replace("elements", "dynamic"),
    host_source(MAC) + host_source(FOREIGN),
])
def test_unproven_or_malformed_static_grants_block_reload(static):
    with pytest.raises(grants.HostGrantError):
        grants.static_grants(static)


class FakeReload:
    def __init__(self, *, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        success = tuple(argv) != self.fail_on
        stdout = host_live(MAC) if argv[:3] == ["nft", "-j", "list"] else ""
        return SimpleNamespace(returncode=0 if success else 1, stdout=stdout)


def test_reload_serializes_and_queues_reconciliation(tmp_path, monkeypatch):
    lock = tmp_path / "lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    monkeypatch.setattr(reload, "check_file", lambda _: host_source(MAC))
    monkeypatch.setattr(reload, "dynamic_eligible", lambda: set())
    fake = FakeReload()
    reload.reload_with_reconcile(lock=lock, root_uid=os.getuid(), runner=fake)
    assert fake.calls == [
        ["nft", "-j", "list", "set", "inet", "host_security", "ap_allowed_macs"],
        ["nft", "-c", "-f", "/etc/nftables.conf"],
        ["nft", "-f", "/etc/nftables.conf"],
        ["systemctl", "--no-block", "start", reload.RECONCILER],
    ]


@pytest.mark.parametrize("fail_on", [
    ("nft", "-j", "list", "set", "inet", "host_security", "ap_allowed_macs"),
    ("nft", "-c", "-f", "/etc/nftables.conf"),
    ("nft", "-f", "/etc/nftables.conf"),
    ("systemctl", "--no-block", "start", reload.RECONCILER),
])
def test_reload_propagates_failures(tmp_path, monkeypatch, fail_on):
    lock = tmp_path / "lock"
    lock.touch(mode=0o600)
    monkeypatch.setattr(reload, "check_file", lambda _: host_source(MAC))
    monkeypatch.setattr(reload, "dynamic_eligible", lambda: set())
    runner = FakeReload(fail_on=fail_on)
    with pytest.raises(reload.ReloadError):
        reload.reload_with_reconcile(lock=lock, root_uid=os.getuid(), runner=runner)
    if fail_on[0] == "nft" and fail_on[1] != "-f":
        assert ["nft", "-f", "/etc/nftables.conf"] not in runner.calls


def test_reload_refuses_independent_grant_drift_before_flush(tmp_path, monkeypatch):
    lock = tmp_path / "lock"
    lock.touch(mode=0o600)
    monkeypatch.setattr(reload, "check_file", lambda _: host_source(FOREIGN))
    monkeypatch.setattr(reload, "dynamic_eligible", lambda: set())
    runner = FakeReload()
    with pytest.raises(grants.HostGrantError):
        reload.reload_with_reconcile(lock=lock, root_uid=os.getuid(), runner=runner)
    assert ["nft", "-f", "/etc/nftables.conf"] not in runner.calls


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666])
def test_reload_refuses_public_coordination_lock(tmp_path, mode):
    lock = tmp_path / "lock"
    lock.touch()
    lock.chmod(mode)
    runner = FakeReload()
    with pytest.raises(reload.ReloadError, match="untrusted"):
        reload.reload_with_reconcile(lock=lock, root_uid=os.getuid(), runner=runner)
    assert runner.calls == []


def test_guarded_playbook_never_runs_via_default_deploy():
    activation = (ROOT / "ansible/playbooks/activate-portal.yml").read_text()
    stage = (ROOT / "ansible/playbooks/stage-portal.yml").read_text()
    deploy = (ROOT / "deploy.sh").read_text()
    assert "activate-portal.yml" not in deploy
    assert "activate-portal.yml" not in stage
    assert "portal_cutover_approved | default(false) | bool" in activation
    assert "portal_maintenance_window | default(false) | bool" in activation
    assert "portal_rollback_operator_ready | default(false) | bool" in activation
    assert "hosts: localhost" in activation
    assert "rescue:" in activation
    assert "original-nftables.conf" in activation
    assert "authorization.pending" not in activation or "ensure_clean" in activation
    assert "portal_host_grants.py" in activation
    assert "nft, -c, -f" in activation
    assert "nft, delete, table, inet, gway_ap_portal" in activation
    assert "nft, flush, ruleset" not in activation
    assert "portal_nft_reload.py" in stage
    assert "state: started" not in stage


def test_systemd_reload_and_boot_are_explicitly_reconciled():
    template = (ROOT / "ansible/templates/gway-ap-nftables-reconcile-dropin.conf.j2").read_text()
    assert "ExecReload=" in template
    assert "portal_nft_reload.py" in template
    assert "ExecStartPost=" in template
    for service in ("gway-ap-portal-candidate.service.j2",
                    "gway-ap-nft-broker-candidate.service.j2"):
        unit = (ROOT / "ansible/templates" / service).read_text()
        assert "host-security-firewall.service" in unit


def test_portal_consent_delta_survives_proven_reload():
    assert grants.compare_source_and_live(host_source(FOREIGN),
        host_live(FOREIGN, MAC), consented_dynamic={MAC}) == 1
    with pytest.raises(grants.HostGrantError):
        grants.compare_source_and_live(host_source(FOREIGN),
            host_live(FOREIGN, MAC), consented_dynamic=set())
    with pytest.raises(grants.HostGrantError):
        grants.compare_source_and_live(host_source(FOREIGN),
            host_live(MAC), consented_dynamic={MAC})


def test_reload_accepts_only_durable_portal_delta(tmp_path, monkeypatch):
    lock = tmp_path / "lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    monkeypatch.setattr(reload, "check_file", lambda _: host_source(FOREIGN))
    monkeypatch.setattr(reload, "dynamic_eligible", lambda: {MAC})

    class DynamicFake(FakeReload):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            if argv[:3] == ["nft", "-j", "list"]:
                result.stdout = host_live(FOREIGN, MAC)
            return result
    runner = DynamicFake()
    reload.reload_with_reconcile(lock=lock, root_uid=os.getuid(), runner=runner)
    assert runner.calls[-1] == ["systemctl", "--no-block", "start", reload.RECONCILER]
