"""Increment 1 contract: immutable staging, no live AP/kiosk activation."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "ansible/playbooks/stage-release.yml"


def test_stage_playbook_syntax():
    result = subprocess.run(
        ["ansible-playbook", "--syntax-check", "-i", "localhost,", str(STAGE)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stage_only_changes_opt_release_root_and_never_activates():
    source = STAGE.read_text()
    assert "/opt/gway-ap-kiosk" in source
    assert "releases/{{ kiosk_stage_revision.stdout }}" in source
    assert "git" in source and "diff" in source and "--quiet" in source
    assert "ansible.builtin.tempfile" in source
    assert "ansible.builtin.copy" in source
    assert "sha256" in source
    assert "REVISION" in source
    assert "mv, -T, --" in source
    for forbidden in (
        "systemd_service", "daemon_reload", "state: restarted",
        "/usr/local/bin/", "/usr/local/sbin/",
        "/etc/systemd/", "/var/lib/gway-ap-kiosk/",
        "kiosk_stage_root }}/current", "kiosk_stage_root }}/previous",
    ):
        assert forbidden not in source


def test_stage_includes_every_live_executable():
    source = STAGE.read_text()
    for executable in (
        "kiosk/bin/gway-kiosk-status",
        "kiosk/bin/kiosk-mode",
        "kiosk/sbin/gway-hdmi-kiosk-session",
        "kiosk/sbin/gway-hdmi-kiosk-layout",
        "kiosk/libexec/gway-boot-stagger-until",
        "scripts/ap/kiosk-ap",
    ):
        assert executable in source
        assert (ROOT / executable).is_file()


def test_default_deployment_retained_and_stage_explicit():
    script = (ROOT / "deploy.sh").read_text()
    assert 'if [[ "${1:-}" == "--stage-release" ]]' in script
    assert "exec ansible-playbook" in script
    assert "ansible/playbooks/stage-release.yml" in script
    assert 'ansible/playbooks/install.yml' in script
    assert "flock -n 9" in script
    parsed = subprocess.run(["bash", "-n", "deploy.sh"], cwd=ROOT,
                            text=True, capture_output=True)
    assert parsed.returncode == 0, parsed.stderr
