from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ap_reveal_helpers_are_packaged():
    assert (ROOT / "scripts/ap/prodigy-password-lcd-60s").exists()
    assert (ROOT / "scripts/ap/prodigy-ap-reveal").exists()
    assert (ROOT / "docs/man/man1/prodigy-password-lcd-60s.1").exists()
    assert (ROOT / "docs/man/man1/prodigy-ap-reveal.1").exists()


def test_ap_reveal_helpers_parse():
    for script in (ROOT / "scripts/ap/prodigy-password-lcd-60s", ROOT / "scripts/ap/prodigy-ap-reveal"):
        result = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_vnc_kiosk_does_not_track_realvnc_child_pid():
    unit = ROOT / "kiosk/systemd/arthexis-vnc-kiosk.service"
    text = unit.read_text(encoding="utf-8")

    assert "Type=forking" in text
    assert "PIDFile=" not in text


def test_rfid_queue_status_is_packaged():
    assert (ROOT / "scripts/gway/rfid_queue_status.py").exists()
    assert (ROOT / "docs/man/man1/rfid-queue-status.1").exists()
