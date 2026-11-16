"""Hermetic proof of reconciler preserving unrelated nft grants."""
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "portal_authorization_reconcile", ROOT / "scripts/portal_authorization_reconcile.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
MAC = "aa:bb:cc:dd:ee:ff"


class Fake:
    def __init__(self, failed=False):
        self.calls = []
        self.failed = failed

    def add(self, macs):
        self.calls.append(("add", set(macs)))
        if self.failed:
            raise OSError("simulated failed nft batch")

    def snapshot(self):
        self.calls.append(("snapshot", None))


def test_reconcile_uses_additive_sets_and_restarts_idempotently(tmp_path):
    (tmp_path / "authorized_macs.txt").write_text(MAC + "\n")
    (tmp_path / "trusted_macs.txt").write_text("12:34:56:78:9a:bc device\n")
    fake = Fake()
    module.reconcile(tmp_path, coordinator=fake)
    module.reconcile(tmp_path, coordinator=fake)
    assert fake.calls == [
        ("add", {MAC, "12:34:56:78:9a:bc"}),
        ("add", {MAC, "12:34:56:78:9a:bc"}),
    ]
    assert (tmp_path / "authorized_macs.txt").read_text() == MAC + "\n"


def test_empty_state_validates_both_sets_without_writes(tmp_path):
    fake = Fake()
    module.reconcile(tmp_path, coordinator=fake)
    assert fake.calls == [("snapshot", None)]


def test_invalid_records_fail_before_firewall(tmp_path):
    (tmp_path / "authorized_macs.txt").write_text("invalid-grant\n")
    fake = Fake()
    with pytest.raises(Exception, match="invalid"):
        module.reconcile(tmp_path, coordinator=fake)
    assert fake.calls == []


def test_failure_does_not_mutate_authorization_file(tmp_path):
    (tmp_path / "authorized_macs.txt").write_text(MAC + "\n")
    with pytest.raises(OSError):
        module.reconcile(tmp_path, coordinator=Fake(failed=True))
    assert (tmp_path / "authorized_macs.txt").read_text() == MAC + "\n"


def test_legacy_wrapper_has_no_flush_and_requires_cutover():
    wrapper = (ROOT / "scripts/ap/gway-ap-authorized-firewall-sync").read_text()
    assert "flush set" not in "\n".join(
        x for x in wrapper.splitlines() if not x.lstrip().startswith("#"))
    assert 'GWAY_AP_CUTOVER_APPROVED:-' in wrapper
    assert "portal_authorization_reconcile.py" in wrapper
