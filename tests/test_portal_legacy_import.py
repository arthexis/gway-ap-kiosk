"""Hermetic legacy import characterization; never touches a real AP."""
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "portal_legacy_import", ROOT / "scripts/portal_legacy_import.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def legacy(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir(mode=0o755)
    (source / "authorized_macs.txt").write_text("aa:bb:cc:dd:ee:ff\n")
    (source / "authorized_macs.txt").chmod(0o644)
    (source / "consents.jsonl").write_text('{"consent":true}\n')
    (source / "consents.jsonl").chmod(0o600)
    return source


def do_import(source, dest):
    return mod.import_quiescent_legacy(
        source, dest, source_uid=os.getuid(),
        target_uid=os.getuid() + 1, target_gid=os.getgid() + 1,
        change_owner=False)


def test_private_copy_preserves_payload_without_logging_private_data(tmp_path):
    source = legacy(tmp_path)
    dest = tmp_path / "private"
    before = (source / "authorized_macs.txt").read_bytes()
    result = do_import(source, dest)
    assert result["imported"] is True
    assert result["ready_for_activation"] is False
    assert result["firewall_backup_verified"] is False
    assert result["writer_quiescence_verified"] is False
    assert "aa:bb" not in str(result) and '"consent":true' not in str(result)
    assert (dest / "authorized_macs.txt").read_bytes() == before
    assert (source / "authorized_macs.txt").read_bytes() == before
    assert dest.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in dest.iterdir())
    assert not (dest / "trusted_macs.txt").exists()


def test_existing_destination_cannot_be_overwritten(tmp_path):
    source = legacy(tmp_path)
    dest = tmp_path / "private"
    dest.mkdir()
    with pytest.raises(mod.ImportDenied, match="destination"):
        do_import(source, dest)


def test_symlink_source_payload_rejected(tmp_path):
    source = legacy(tmp_path)
    target = source / "consents.jsonl"
    target.unlink()
    target.symlink_to(source / "authorized_macs.txt")
    with pytest.raises(mod.ImportDenied, match="unsafe legacy"):
        do_import(source, tmp_path / "private")


def test_group_writable_source_rejected(tmp_path):
    source = legacy(tmp_path)
    source.chmod(0o775)
    with pytest.raises(mod.ImportDenied, match="directory permissions"):
        do_import(source, tmp_path / "private")


def test_hardlink_source_payload_rejected(tmp_path):
    source = legacy(tmp_path)
    os.link(source / "authorized_macs.txt", source / "other")
    with pytest.raises(mod.ImportDenied, match="untrusted legacy"):
        do_import(source, tmp_path / "private")


def test_changed_source_detected_without_success_receipt(tmp_path, monkeypatch):
    source = legacy(tmp_path)
    original = mod._read_legacy
    calls = 0
    def racing(fd, name, *, source_uid):
        nonlocal calls
        data = original(fd, name, source_uid=source_uid)
        calls += 1
        if calls == len(mod.FILES) and data is None:
            (source / "trusted_macs.txt").write_text("aa:bb:cc:dd:ee:ff\n")
        return data
    monkeypatch.setattr(mod, "_read_legacy", racing)
    with pytest.raises(mod.ImportDenied, match="changed"):
        do_import(source, tmp_path / "private")
