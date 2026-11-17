"""Adversarial offline checks for pinned-state broker provenance."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("portal_nft_broker_provenance", ROOT / "scripts/portal_nft_broker.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

MAC = "aa:bb:cc:dd:ee:ff"
OTHER = "12:34:56:78:9a:bc"

def setup_state(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    (directory / "authorized_macs.txt").write_text(MAC + "\n")
    (directory / "authorized_macs.txt").chmod(0o600)
    (directory / "trusted_macs.txt").write_text(OTHER + "\n")
    (directory / "trusted_macs.txt").chmod(0o600)
    return directory

def test_valid_persisted_and_independent_grants_preserved(tmp_path):
    directory = setup_state(tmp_path)
    module.verify_persisted_grants(directory, {MAC, OTHER}, uid=os.getuid())

def test_same_uid_cannot_grant_mac_absent_from_durable_state(tmp_path):
    directory = setup_state(tmp_path)
    with pytest.raises(module.BrokerRejected, match="lacks persisted"):
        module.verify_persisted_grants(directory, {"02:00:00:00:00:03"}, uid=os.getuid())

def test_different_uid_is_denied_even_with_valid_mac(tmp_path):
    directory = setup_state(tmp_path)
    with pytest.raises(module.BrokerRejected, match="ownership"):
        module.verify_persisted_grants(directory, {MAC}, uid=os.getuid()+1)

def test_symlink_private_data_and_world_readability_fail_closed(tmp_path):
    directory = setup_state(tmp_path)
    target = directory / "external"
    target.write_text(MAC + "\n")
    (directory / "authorized_macs.txt").unlink()
    (directory / "authorized_macs.txt").symlink_to(target)
    with pytest.raises(module.BrokerRejected, match="unsafe grant"):
        module.verify_persisted_grants(directory, {MAC}, uid=os.getuid())
    (directory / "authorized_macs.txt").unlink()
    (directory / "authorized_macs.txt").write_text(MAC + "\n")
    (directory / "authorized_macs.txt").chmod(0o644)
    with pytest.raises(module.BrokerRejected, match="unsafe grant"):
        module.verify_persisted_grants(directory, {MAC}, uid=os.getuid())

def test_symlink_and_world_accessible_state_dir_fail_closed(tmp_path):
    directory = setup_state(tmp_path)
    (tmp_path / "link").symlink_to(directory, target_is_directory=True)
    with pytest.raises(module.BrokerRejected):
        module.verify_persisted_grants(tmp_path/"link", {MAC}, uid=os.getuid())
    directory.chmod(0o755)
    with pytest.raises(module.BrokerRejected, match="ownership"):
        module.verify_persisted_grants(directory, {MAC}, uid=os.getuid())

def test_pending_marker_must_be_private_and_owned(tmp_path):
    directory = setup_state(tmp_path)
    pending = directory / "authorization.pending"
    pending.write_text('{"schema":1,"status":"uncertain"}\n')
    pending.chmod(0o600)
    module.verify_persisted_grants(directory, {MAC}, uid=os.getuid())
    pending.chmod(0o644)
    with pytest.raises(module.BrokerRejected, match="pending"):
        module.verify_persisted_grants(directory, {MAC}, uid=os.getuid())


def test_state_file_payload_is_bounded_without_any_nft_write(tmp_path):
    directory = setup_state(tmp_path)
    (directory / "authorized_macs.txt").write_text("x" * (1024 * 1024 + 1))
    (directory / "authorized_macs.txt").chmod(0o600)
    with pytest.raises(module.BrokerRejected, match="oversize"):
        module.verify_persisted_grants(directory, {MAC}, uid=os.getuid())

def test_invalid_utf8_grants_fail_closed(tmp_path):
    directory = setup_state(tmp_path)
    (directory / "authorized_macs.txt").write_bytes(b"\xff\xfe")
    with pytest.raises(module.BrokerRejected, match="encoding"):
        module.verify_persisted_grants(directory, {MAC}, uid=os.getuid())


def test_racing_allowlist_replacement_is_rejected(tmp_path, monkeypatch):
    directory = setup_state(tmp_path)
    real_stat = module.os.stat
    triggered = False
    def racing_stat(path, *args, **kwargs):
        nonlocal triggered
        if path == "authorized_macs.txt" and "dir_fd" in kwargs and not triggered:
            triggered = True
            original = directory / "authorized_macs.txt"
            replacement = directory / "replacement"
            replacement.write_text(MAC + "\n")
            replacement.chmod(0o600)
            original.replace(directory / "old-allowlist")
            replacement.replace(original)
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(module.os, "stat", racing_stat)
    with pytest.raises(module.BrokerRejected, match="changed"):
        module.verify_persisted_grants(directory, {MAC}, uid=os.getuid())
    assert triggered


def test_broker_socket_protection_needs_no_chown_capability(tmp_path, monkeypatch):
    import socket
    import stat
    from types import SimpleNamespace
    sock_path = tmp_path / "broker.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(sock_path))
        monkeypatch.setattr(module.pwd, "getpwuid",
                            lambda uid: SimpleNamespace(pw_gid=os.getgid()))
        monkeypatch.setattr(module.os, "chown",
                            lambda *args: pytest.fail("CAP_CHOWN must not be needed"))
        module.protect_bound_socket(
            sock_path, peer_uid=os.getuid() + 1, root_uid=os.getuid())
        meta = sock_path.lstat()
        assert stat.S_ISSOCK(meta.st_mode)
        assert stat.S_IMODE(meta.st_mode) == 0o660


def test_broker_socket_rejects_wrong_group_or_same_uid(tmp_path, monkeypatch):
    import socket
    from types import SimpleNamespace
    sock_path = tmp_path / "broker.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(sock_path))
        monkeypatch.setattr(module.pwd, "getpwuid",
                            lambda uid: SimpleNamespace(pw_gid=os.getgid() + 1))
        with pytest.raises(module.BrokerRejected, match="ownership"):
            module.protect_bound_socket(sock_path, peer_uid=os.getuid() + 1,
                                        root_uid=os.getuid())
        monkeypatch.setattr(module.pwd, "getpwuid",
                            lambda uid: SimpleNamespace(pw_gid=os.getgid()))
        with pytest.raises(module.BrokerRejected, match="ownership"):
            module.protect_bound_socket(sock_path, peer_uid=os.getuid(),
                                        root_uid=os.getuid())
