"""Generic AP installation and invocation contract tests."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installation_uses_generic_ap_controller():
    playbook = (ROOT / "ansible/playbooks/install.yml").read_text()
    assert "scripts/ap/kiosk-ap" in playbook
    assert "/usr/local/sbin/kiosk-ap" in playbook
    assert "NOPASSWD: /usr/local/sbin/kiosk-ap rotate" in playbook
    assert "validate: /usr/sbin/visudo -cf %s" in playbook
    assert "prodigy-ap-rotate" not in playbook


def test_kiosk_never_uses_prodigy_rotation():
    source = (ROOT / "kiosk/bin/gway-kiosk-status").read_text()
    assert "prodigy-ap-rotate" not in source
    assert "ap-password.txt" not in source
