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


def test_hdmi_unit_dropins_are_installed_and_reference_existing_units():
    playbook = (ROOT / "ansible/playbooks/install.yml").read_text()
    systemd = ROOT / "kiosk/systemd"
    for name in (
        "gway-hdmi-kiosk.service",
        "gway-hdmi-kiosk-layout.service",
        "gway-kiosk-status.service",
    ):
        assert (systemd / name).is_file()
        assert name in playbook

    for name in (
        "gway-hdmi-kiosk.service.d/98-boot-stagger-30s.conf",
        "gway-hdmi-kiosk-layout.service.d/20-bastion-refresh-order.conf",
        "gway-hdmi-kiosk-layout.service.d/98-boot-stagger-30s.conf",
    ):
        assert (systemd / name).is_file()
        assert name in playbook

    bastion_hook = systemd / "bastion-usb-refresh.service.d/60-kiosk-layout.conf"
    assert bastion_hook.is_file()
    assert "gway-hdmi-kiosk-layout.service" in bastion_hook.read_text()
    assert "when: bastion_refresh_units.results" in playbook


def test_old_hdmi_names_are_not_installed():
    playbook = (ROOT / "ansible/playbooks/install.yml").read_text()
    assert "arthexis-hdmi-kiosk" not in playbook
    assert "kiosk/tools/install.sh" not in playbook
    for name in ("arthexis-hdmi-kiosk.service", "arthexis-hdmi-kiosk-layout.service"):
        assert not (ROOT / "kiosk/systemd" / name).exists()
