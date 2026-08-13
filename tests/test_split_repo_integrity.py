from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ap_reveal_helpers_are_packaged():
    assert (ROOT / "scripts/ap/prodigy-password-lcd-60s").exists()
    assert (ROOT / "scripts/ap/prodigy-reveal-ap").exists()
    assert (ROOT / "docs/man/man1/prodigy-password-lcd-60s.1").exists()
    assert (ROOT / "docs/man/man1/prodigy-reveal-ap.1").exists()


def test_ap_reveal_helpers_parse():
    for script in (ROOT / "scripts/ap/prodigy-password-lcd-60s", ROOT / "scripts/ap/prodigy-reveal-ap"):
        result = subprocess.run(["bash", "-n", str(script)], check=False, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
