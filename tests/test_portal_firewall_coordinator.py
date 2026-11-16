"""Hermetic tests for the two-layer nftables coordinator."""
import importlib.util
import json
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / "scripts/portal_firewall_coordinator.py"
spec = importlib.util.spec_from_file_location("portal_firewall_coordinator", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
MAC = "aa:bb:cc:dd:ee:ff"
FOREIGN = "12:34:56:78:9a:bc"


class FakeNft:
    def __init__(self, missing=False, fail_batch=False):
        self.elements = {mod.SETS[0]: {FOREIGN}, mod.SETS[1]: set()}
        self.missing = missing
        self.fail_batch = fail_batch
        self.batches = []

    def __call__(self, argv, *, input=None):
        if argv[:3] == ["nft", "-j", "list"]:
            key = (argv[-2], argv[-1])
            if self.missing and key == mod.SETS[0]:
                raise mod.CoordinationError("missing table")
            return json.dumps({"nftables": [
                {"set": {"family": "inet", "table": key[0], "name": key[1],
                         "type": "ether_addr", "elem": sorted(self.elements[key])}}]})
        assert argv == ["nft", "-f", "-"]
        if self.fail_batch:
            raise mod.CoordinationError("transaction rejected")
        self.batches.append(input)
        new = {k: set(v) for k, v in self.elements.items()}
        for line in input.splitlines():
            verb, _, _, table, name, remainder = line.split(maxsplit=5)
            key = (table, name)
            values = {value.strip() for value in remainder.strip(" {}").split(",")}
            if verb == "add":
                new[key].update(values)
            elif verb == "delete":
                new[key].difference_update(values)
            else:
                raise AssertionError(verb)
        self.elements = new
        return ""


def test_atomic_add_and_idempotence_retains_independent_grant():
    fake = FakeNft()
    coordinator = mod.NftGrantCoordinator(run=fake)
    delta = coordinator.add({MAC})
    assert len(fake.batches) == 1
    assert all(MAC in fake.elements[key] for key in mod.SETS)
    assert FOREIGN in fake.elements[mod.SETS[0]]
    assert coordinator.add({MAC}) == {key: set() for key in mod.SETS}
    assert len(fake.batches) == 1
    with pytest.raises(mod.CoordinationError, match="exclusive"):
        coordinator.rollback(delta)
    coordinator.rollback(delta, exclusive_lock=True)
    assert FOREIGN in fake.elements[mod.SETS[0]]
    assert all(MAC not in fake.elements[key] for key in mod.SETS)


@pytest.mark.parametrize("failure", ["missing", "batch"])
def test_failed_precondition_or_transaction_never_partially_grants(failure):
    fake = FakeNft(missing=failure == "missing", fail_batch=failure == "batch")
    with pytest.raises(mod.CoordinationError):
        mod.NftGrantCoordinator(run=fake).add({MAC})
    assert MAC not in fake.elements[mod.SETS[0]]
    assert MAC not in fake.elements[mod.SETS[1]]


def test_reject_invalid_and_unparseable_or_wrong_type():
    fake = FakeNft()
    coordinator = mod.NftGrantCoordinator(run=fake)
    with pytest.raises(mod.CoordinationError):
        coordinator.add({"aa:bb:cc:dd:ee:ff; flush ruleset"})
    assert not fake.batches
    with pytest.raises(mod.CoordinationError):
        mod._elements({"nftables": [{"set": {"family": "inet",
            "table": "host_security", "name": "ap_allowed_macs",
            "type": "ipv4_addr", "elem": []}}]}, "host_security", "ap_allowed_macs")


def test_restart_reconciliation_only_adds_missing_grants():
    fake = FakeNft()
    fake.elements[mod.SETS[0]].add(MAC)
    mod.NftGrantCoordinator(run=fake).add({MAC})
    assert fake.elements[mod.SETS[0]] == {FOREIGN, MAC}
    assert fake.elements[mod.SETS[1]] == {MAC}
    assert "add element inet host_security" not in fake.batches[0]
