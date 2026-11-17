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
            self.reads = 0
        def getsockopt(self, *args):
            return struct.pack("3i", 42, self.uid, 42)
        def recv(self, n):
            self.reads += 1
            return (json.dumps({"op": "add", "macs": [MAC]}).encode() + b"\n") if self.reads == 1 else b""
        def sendall(self, data):
            self.out.append(data)
    class Coordinator:
        def __init__(self):
            self.calls = []
        def add(self, macs):
            self.calls.append(set(macs))
    c = Coordinator()
    broker.serve_request(Conn(12345), uid=12345, coordinator=c,
                         lock_path=tmp_path / "lock", lock_owner_uid=os.getuid())
    assert c.calls == [{MAC}]
    with pytest.raises(broker.BrokerRejected):
        broker.serve_request(Conn(12346), uid=12345, coordinator=c,
                             lock_path=tmp_path / "lock", lock_owner_uid=os.getuid())
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


def test_portal_refuses_startup_after_interrupted_grant(tmp_path):
    portal = load("ap_portal_server")
    directory = tmp_path / "state"
    directory.mkdir()
    journal.GrantIntent(directory).begin()
    config = portal.PortalConfig(
        bind="127.0.0.1", port=0, assets_dir=tmp_path,
        state_dir=directory, authorized_macs_path=directory / "authorized_macs.txt",
        trusted_macs_path=directory / "trusted_macs.txt",
        consents_path=directory / "consents.jsonl",
        activity_path=directory / "activity.jsonl",
        source_url="", sync_firewall=True, coordinated_firewall=True)
    with pytest.raises(journal.RecoveryRequired):
        portal.PortalState(config)


def test_broker_client_failure_is_closed(tmp_path):
    client_mod = load("portal_nft_client")
    client = client_mod.BrokerClient(tmp_path / "missing.sock")
    with pytest.raises(client_mod.CoordinationError):
        client.add({MAC})
    with pytest.raises(client_mod.CoordinationError):
        client.snapshot()
    with pytest.raises(client_mod.CoordinationError):
        client.add({"bad; flush ruleset"})


def test_broker_rejects_second_frame_and_untrusted_uid(tmp_path):
    payload = json.dumps({"op": "add", "macs": [MAC]}).encode() + b"\n"
    with pytest.raises(broker.BrokerRejected):
        broker.decode_request(payload + payload)
    assert broker.decode_request(json.dumps({"op": "check", "macs": []}).encode()+b"\n") == set()


def test_broker_supports_over_64_grants_atomically():
    values = [f"02:00:00:{i//65536:02x}:{(i//256)%256:02x}:{i%256:02x}" for i in range(130)]
    msg = json.dumps({"op":"add","macs":values}).encode() + b"\n"
    assert len(broker.decode_request(msg)) == 130
    assert len(msg) <= broker.MAX_REQUEST
    too_many = [f"02:00:{i//(256*256*256):02x}:{(i//65536)%256:02x}:{(i//256)%256:02x}:{i%256:02x}" for i in range(broker.MAX_MACS + 1)]
    with pytest.raises(broker.BrokerRejected):
        broker.decode_request(json.dumps({"op":"add","macs":too_many}).encode()+b"\n")


def test_malformed_peer_credentials_rejected():
    class Fake:
        def getsockopt(self, *_):
            return b"bad"
    with pytest.raises(broker.BrokerRejected):
        broker.authorize_peer(Fake(), allowed_uid=123)


def test_busy_broker_lock_refuses_firewall_write(tmp_path, monkeypatch):
    class Conn:
        def __init__(self):
            self.reads = 0
        def getsockopt(self, *_):
            return struct.pack("3i", 42, 12345, 42)
        def recv(self, size):
            self.reads += 1
            return (json.dumps({"op": "add", "macs": [MAC]}).encode() + b"\n") if self.reads == 1 else b""
        def sendall(self, data):
            raise AssertionError("busy lock must not acknowledge success")
    class Coordinator:
        def add(self, macs):
            raise AssertionError("busy lock must not call nft")
    def busy_lock(*args):
        raise BlockingIOError("locked")
    monkeypatch.setattr(broker.fcntl, "flock", busy_lock)
    with pytest.raises(broker.BrokerRejected, match="writer is active"):
        broker.serve_request(Conn(), uid=12345, coordinator=Coordinator(), lock_path=tmp_path/"broker.lock", lock_owner_uid=os.getuid())


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666])
def test_broker_rejects_exposed_lock_without_nft_access(tmp_path, mode):
    lock = tmp_path / "lock"
    lock.write_text("")
    lock.chmod(mode)
    class Conn:
        def __init__(self): self.reads = 0
        def getsockopt(self, *_): return struct.pack("3i", 42, 12345, 42)
        def recv(self, size):
            self.reads += 1
            return (json.dumps({"op":"add","macs":[MAC]}).encode()+b"\n") if self.reads == 1 else b""
        def sendall(self, _): raise AssertionError("untrusted lock must not succeed")
    class NoNft:
        def add(self, _): raise AssertionError("untrusted lock must not reach nft")
    with pytest.raises(broker.BrokerRejected, match="untrusted broker lock"):
        broker.serve_request(Conn(), uid=12345, coordinator=NoNft(),
                             lock_path=lock, lock_owner_uid=os.getuid())


def test_broker_rejects_hardlinked_lock_without_nft_access(tmp_path):
    lock = tmp_path / "lock"
    lock.write_text("")
    lock.chmod(0o600)
    os.link(lock, tmp_path / "second-link")
    class Conn:
        def __init__(self): self.reads = 0
        def getsockopt(self, *_): return struct.pack("3i", 42, 12345, 42)
        def recv(self, size):
            self.reads += 1
            return (json.dumps({"op":"check","macs":[]}).encode()+b"\n") if self.reads == 1 else b""
        def sendall(self, _): raise AssertionError("untrusted lock must not succeed")
    class NoNft:
        def snapshot(self): raise AssertionError("untrusted lock must not reach nft")
    with pytest.raises(broker.BrokerRejected, match="untrusted broker lock"):
        broker.serve_request(Conn(), uid=12345, coordinator=NoNft(),
                             lock_path=lock, lock_owner_uid=os.getuid())
