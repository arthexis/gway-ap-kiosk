"""Hermetic private backup / sandbox restoration, never live firewall."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("portal_state_backup",
                                              ROOT / "scripts/portal_state_backup.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def state(tmp_path):
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    for name in mod.FILES:
        file = source / name
        file.write_text("private state: aa:bb:cc:dd:ee:ff\n")
        file.chmod(0o600)
    return source

def test_backup_and_sandbox_restore_preserve_every_byte_and_pending(tmp_path):
    original = state(tmp_path)
    backup = tmp_path / "backup"
    manifest = mod.create_snapshot(original, backup)
    assert not manifest["restore_approved"]
    receipt = mod.verify_snapshot(backup)
    assert receipt["integrity_verified"] is True
    assert receipt["uncertain_marker_preserved"] is True
    result = mod.restore_to_empty_sandbox(backup, tmp_path / "restore")
    assert result["sandbox_restore_verified"]
    for name in mod.FILES:
        assert (original/name).read_bytes() == (tmp_path/"restore"/name).read_bytes()
    assert (original/"authorization.pending").exists()

def test_tampered_backup_fails_without_restoring(tmp_path):
    original = state(tmp_path)
    backup = tmp_path / "backup"
    mod.create_snapshot(original, backup)
    (backup/"authorized_macs.txt").write_text("tampered")
    with pytest.raises(mod.BackupError, match="mismatch"):
        mod.restore_to_empty_sandbox(backup, tmp_path / "restore")
    assert not (tmp_path/"restore").exists()

def test_symlinks_and_insecure_files_fail_closed(tmp_path):
    original = state(tmp_path)
    (original/"trusted_macs.txt").unlink()
    (original/"trusted_macs.txt").symlink_to(original/"authorized_macs.txt")
    with pytest.raises(mod.BackupError):
        mod.create_snapshot(original, tmp_path/"backup")
    (original/"trusted_macs.txt").unlink()
    (original/"trusted_macs.txt").write_text("trusted")
    (original/"trusted_macs.txt").chmod(0o644)
    with pytest.raises(mod.BackupError):
        mod.create_snapshot(original, tmp_path/"backup-2")

def test_no_overwrite_no_live_restore_and_no_release(tmp_path):
    original = state(tmp_path)
    backup = tmp_path/"backup"
    mod.create_snapshot(original, backup)
    with pytest.raises(FileExistsError):
        mod.create_snapshot(original, backup)
    with pytest.raises(mod.BackupError, match="live"):
        mod.restore_to_empty_sandbox(backup, Path("/var/lib/gway-ap-kiosk/portal"))
    assert (original/"authorization.pending").exists()

def test_missing_and_replaced_entries_detected(tmp_path):
    original = state(tmp_path)
    backup = tmp_path/"backup"
    mod.create_snapshot(original, backup)
    (backup/"activity.jsonl").unlink()
    with pytest.raises(mod.BackupError):
        mod.verify_snapshot(backup)
