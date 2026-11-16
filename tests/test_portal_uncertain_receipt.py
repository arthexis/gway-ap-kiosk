"""Read-only, redacted receipts do not reveal identity or clear uncertainty."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/portal_uncertain_receipt.py"
spec = importlib.util.spec_from_file_location("portal_uncertain_receipt", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_redacts_private_records_and_never_releases(tmp_path):
    secret = "aa:bb:cc:dd:ee:ff account@example.invalid"
    for name in mod.FILES:
        p = tmp_path / name
        p.write_text(secret)
        p.chmod(0o600)
    receipt = mod.collect(tmp_path)
    assert receipt["uncertain"]
    assert receipt["operator_release_allowed"] is False
    assert receipt["firewall_verified"] is False
    assert receipt["state_backup_verified"] is False
    assert secret not in json.dumps(receipt)
    assert (tmp_path / "authorization.pending").read_text() == secret


def test_suspicious_paths_fail_closed_without_following_links(tmp_path):
    target = tmp_path / "target"
    target.write_text("private")
    (tmp_path / "authorization.pending").symlink_to(target)
    report = mod.collect(tmp_path)
    assert report["file_status"]["authorization.pending"] == "unsafe_symlink"
    assert report["uncertain"]
    assert target.read_text() == "private"
    with pytest.raises(ValueError):
        mod.collect(tmp_path / "authorization.pending")


def test_missing_pending_marker_is_not_a_verified_recovery(tmp_path):
    report = mod.collect(tmp_path)
    assert report["uncertain"] is False
    assert report["operator_release_allowed"] is False
    assert all(value == "missing" for value in report["file_status"].values())


def test_verified_offline_backup_never_authorizes_runtime_recovery(tmp_path):
    import sys
    sys.path.insert(0, str(SCRIPT.parent))
    from portal_state_backup import create_snapshot
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    marker = state / "authorization.pending"
    marker.write_text("sensitive MAC aa:bb:cc:dd:ee:ff")
    marker.chmod(0o600)
    backup = tmp_path / "backup"
    create_snapshot(state, backup)
    report = mod.collect(state, snapshot=backup)
    assert report["offline_snapshot_integrity_verified"] is True
    assert report["offline_snapshot_pending_preserved"] is True
    assert report["state_backup_verified"] is False
    assert report["operator_release_allowed"] is False
    assert marker.exists()
    assert "aa:bb:cc:dd:ee:ff" not in json.dumps(report)
    (backup/"authorization.pending").write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        mod.collect(state, snapshot=backup)
    assert marker.exists()
