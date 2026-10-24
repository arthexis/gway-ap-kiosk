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


def test_hdmi_kiosk_unit_provisions_private_state_for_its_browser_profile():
    unit = (ROOT / "kiosk/systemd/gway-hdmi-kiosk.service").read_text()
    assert "User=arthe" in unit
    assert "Group=arthe" in unit
    assert "StateDirectory=gway-hdmi-kiosk" in unit
    assert "StateDirectoryMode=0700" in unit


def test_old_hdmi_names_are_not_installed():
    playbook = (ROOT / "ansible/playbooks/install.yml").read_text()
    assert "arthexis-hdmi-kiosk" not in playbook
    assert "kiosk/tools/install.sh" not in playbook
    for name in ("arthexis-hdmi-kiosk.service", "arthexis-hdmi-kiosk-layout.service"):
        assert not (ROOT / "kiosk/systemd" / name).exists()


def test_kiosk_preserves_operator_bin_ownership_and_system_paths():
    playbook = (ROOT / "ansible/playbooks/install.yml").read_text()
    assert 'path: "{{ kiosk_home }}/.local/bin"' in playbook
    assert 'owner: "{{ kiosk_user }}"' in playbook
    assert 'dest: /usr/local/bin/gway-kiosk-status' in playbook
    assert 'dest: /usr/local/bin/kiosk-mode' in playbook
    assert 'dest: /home/arthe/.local/bin/' not in playbook
    for path in (
        ROOT / "kiosk/systemd/gway-kiosk-status.service",
        ROOT / "kiosk/applications/kiosk-mode.desktop",
        ROOT / "kiosk/desktop/Kiosk Mode.desktop",
    ):
        content = path.read_text()
        assert "/home/arthe/.local/bin/" not in content
        assert "/usr/local/bin/" in content


def test_ansible_installs_the_kiosk_mode_on_desktop_launcher():
    playbook = (ROOT / "ansible/playbooks/install.yml").read_text()
    desktop = (ROOT / "kiosk/desktop/Kiosk Mode.desktop").read_text()
    application = (ROOT / "kiosk/applications/kiosk-mode.desktop").read_text()
    assert "Desktop/Kiosk Mode On.desktop" in playbook
    assert "Name=Kiosk Mode On" in desktop
    assert "Name=Kiosk Mode On" in application
    assert "Exec=/usr/local/bin/kiosk-mode on" in desktop


def test_ansible_installs_narrow_kiosk_layout_polkit_policy():
    playbook = (ROOT / "ansible/playbooks/install.yml").read_text()
    rule = ROOT / "config/polkit-1/rules.d/49-gway-kiosk-layout.rules"
    assert rule.is_file()
    assert "49-gway-kiosk-layout.rules" in playbook
    content = rule.read_text()
    assert 'action.lookup("unit") === "gway-hdmi-kiosk-layout.service"' in content
    assert 'action.lookup("verb") === "start"' in content
