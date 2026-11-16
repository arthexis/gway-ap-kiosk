from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_candidate_playbook_is_nonactivating_and_stages_all_assets():
    playbook = (ROOT / "ansible/playbooks/stage-portal.yml").read_text()
    assert "hosts: localhost" in playbook
    assert "portal_candidate_dir" in playbook
    assert all(name in playbook for name in ("index.html", "app.css", "app.js", "ap_portal_server.py"))
    assert "systemd_service:" not in playbook
    assert "state: started" not in playbook
    assert "enabled: true" not in playbook
    assert "nft " not in playbook
    assert "/etc/nginx/sites-enabled" not in playbook


def test_candidate_unit_uses_supported_arguments_and_no_developer_venv():
    unit = (ROOT / "ansible/templates/gway-ap-portal-candidate.service.j2").read_text()
    assert "ExecStart=/usr/bin/python3" in unit
    for arg in ("--bind", "--port", "--assets-dir", "--state-dir"):
        assert arg in unit
    assert "--suite-login-host" not in unit
    assert ".venv" not in unit
    assert "ConditionPathExists=/etc/gway-ap-kiosk/portal-activation-approved" in unit
    assert "User=arthe" in unit


def test_nginx_candidate_restricts_upstream_to_loopback():
    snippet = (ROOT / "config/nginx/gway-ap-portal.location").read_text()
    assert "127.0.0.1:9080" in snippet
    assert "X-Real-IP" in snippet
    assert "X-Forwarded-For $remote_addr" in snippet
