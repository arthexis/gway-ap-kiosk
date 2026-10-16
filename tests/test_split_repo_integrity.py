"""Functional checks for the retained standalone AP and kiosk helpers."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ap_rotation_and_reveal_helpers_parse():
    for path in ("scripts/ap/prodigy-ap-rotate", "scripts/ap/prodigy-ap-reveal"):
        result = subprocess.run(["bash", "-n", str(ROOT / path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_kiosk_installer_parses():
    result = subprocess.run(["bash", "-n", str(ROOT / "kiosk/tools/install.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
