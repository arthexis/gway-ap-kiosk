"""Hermetic root-owned cutover gates. No systemctl, nft or nodes."""
import importlib.util
import json
import os
from pathlib import Path
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/portal_cutover_contract.py"
spec = importlib.util.spec_from_file_location("portal_cutover_contract", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def make(path, assertions=None):
    payload = {"schema": 1, "assertions": assertions if assertions is not None else
               {k: True for k in mod.REQUIRED}}
    path.write_text(json.dumps(payload))
    path.chmod(0o600)

def test_complete_root_owned_contract_is_accepted(tmp_path):
    path = tmp_path / "contract"
    make(path)
    mod.check_contract(path, root_uid=os.getuid())

@pytest.mark.parametrize("missing", mod.REQUIRED)
def test_all_required_checks_fail_closed_individually(tmp_path, missing):
    path = tmp_path / "contract"
    checks = {x: True for x in mod.REQUIRED}
    checks[missing] = False
    make(path, checks)
    with pytest.raises(mod.CutoverDenied):
        mod.check_contract(path, root_uid=os.getuid())

def test_unsafe_ownership_modes_symlinks_and_unknown_assertions(tmp_path):
    path = tmp_path / "contract"
    make(path)
    with pytest.raises(mod.CutoverDenied):
        mod.check_contract(path, root_uid=os.getuid() + 1)
    path.chmod(0o644)
    with pytest.raises(mod.CutoverDenied):
        mod.check_contract(path, root_uid=os.getuid())
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(mod.CutoverDenied):
        mod.check_contract(link, root_uid=os.getuid())
    checks = {x: True for x in mod.REQUIRED}
    checks["extra"] = True
    make(path, checks)
    with pytest.raises(mod.CutoverDenied):
        mod.check_contract(path, root_uid=os.getuid())

def test_missing_contract_and_unproven_fields_are_denied(tmp_path):
    with pytest.raises(mod.CutoverDenied):
        mod.check_contract(tmp_path / "missing", root_uid=os.getuid())
    path = tmp_path / "contract"
    make(path, {"legacy_service_disabled": True})
    with pytest.raises(mod.CutoverDenied):
        mod.check_contract(path, root_uid=os.getuid())

def test_broker_unit_pins_private_root_contract_and_dedicated_uid():
    root = SOURCE.parents[1]
    unit = (root / "ansible/templates/gway-ap-nft-broker-candidate.service.j2").read_text()
    stage = (root / "ansible/playbooks/stage-portal.yml").read_text()
    assert "--cutover-contract /etc/gway-ap-kiosk/portal-writer-cutover.json" in unit
    assert "portal_cutover_contract.py" in stage
    assert "id -u gwayap" in stage


def test_activated_playbook_converts_cli_argument_into_path():
    """The gated contract must be reachable before any live nft mutation."""
    import yaml
    root = SOURCE.parents[1]
    tasks = yaml.safe_load(
        (root / "ansible/playbooks/activate-portal.yml").read_text())[0]["tasks"]
    task = next(t for t in tasks if t["name"] ==
                "Require privately owned root cutover assertions")
    program = task["ansible.builtin.command"]["argv"][2]
    assert "from pathlib import Path" in program
    assert "check_contract(Path(sys.argv[2]))" in program
    assert "check_contract(sys.argv[2])" not in program
