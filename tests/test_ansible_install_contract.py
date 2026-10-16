"""Functional deployment entrypoint and Ansible contract checks."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_entrypoint_parses_and_invokes_ansible():
    script = ROOT / "deploy.sh"
    result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ansible-playbook" in script.read_text()


def test_ansible_installs_kiosk_not_rfid_or_vnc():
    playbook = (ROOT / "ansible/playbooks/install.yml").read_text()
    assert "gway-kiosk-status.service" in playbook
    assert "ansible.builtin.uri" in playbook
    assert "rfid-scan-publisher" not in playbook
    assert "arthexis-vnc-kiosk" not in playbook
