"""Hermetic broker parser, peer-gate and durable interrupted-intent tests."""
import importlib.util
import json
import os
import socket
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

broker = load("portal_nft_broker")
journal = load("portal_grant_intent")
MAC = "aa:bb:cc:dd:ee:ff"


@pytest.mark.parametrize("payload", [
    b"", b'{"op":"delete","macs":["aa:bb:cc:dd:ee:ff"]}\n',
    b'{"op":"add","macs":["aa:bb:cc:dd:ee:ff"],"table":"host_security"}\n',
    b'{"op":"add","macs":["aa:bb:cc:dd:ee:ff; flush ruleset"]}\n',
    b'{"op":"add","macs":[]}\n',
    b'{"op":"add","macs":["aa:bb:cc:dd:ee:ff"]}',
    b'{"op":"add","macs":["aa:bb:cc:dd:ee:ff"]}\nX',
    b'{"op":"add","macs":["aa:bb:cc:dd:ee:ff","AA:BB:CC:DD:EE:FF"]}\n',
    b"x" * (broker.MAX_REQUEST + 1),
])
def test_broker_rejects_untrusted_command_and_framing(payload):
    with pytest.raises(broker.BrokerRejected):
        broker.decode_request(payload)


def test_broker_allows_only_fixed_add_grants():
    assert broker.decode_request(json.dumps({"op": "add", "macs": [MAC]}).encode() + b"\n") == {MAC}


def test_broker_validates_peer_uid_not_header(monkeypatch, tmp_path):
    class Conn:
        def __init__(self, uid):
            self.uid = uid
            self.out = []
        def getsockopt(self, *args):
            return struct.pack("3i", 42, self.uid, 42)
        def recv(self, n):
            return json.dumps({"op": "add", "macs": [MAC]}).encode() + b"\n"
        def sendall(self, data):
            self.out.append(data)
    class Coordinator:
        def __init__(self):
            self.calls = []
        def add(self, macs):
            self.calls.append(set(macs))
    c = Coordinator()
    broker.serve_request(Conn(12345), uid=12345, coordinator=c,
                         lock_path=tmp_path / "lock")
    assert c.calls == [{MAC}]
    with pytest.raises(broker.BrokerRejected):
        broker.serve_request(Conn(12346), uid=12345, coordinator=c,
                             lock_path=tmp_path / "lock")
    assert len(c.calls) == 1


def test_durable_intent_blocks_restart_until_explicit_recovery(tmp_path):
    intent = journal.GrantIntent(tmp_path)
    intent.begin()
    assert (tmp_path / "authorization.pending").stat().st_mode & 0o777 == 0o600
    with pytest.raises(journal.RecoveryRequired, match="uncertain"):
        intent.ensure_clean()
    with pytest.raises(journal.RecoveryRequired):
        intent.begin()
    intent.finish()
    intent.ensure_clean()


def test_suspicious_symlink_marker_fails_closed(tmp_path):
    target = tmp_path / "other"
    target.write_text("do-not-edit")
    (tmp_path / "authorization.pending").symlink_to(target)
    intent = journal.GrantIntent(tmp_path)
    with pytest.raises(journal.RecoveryRequired):
        intent.ensure_clean()
    with pytest.raises(journal.RecoveryRequired):
        intent.finish()
    assert target.read_text() == "do-not-edit"
