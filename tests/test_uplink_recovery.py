import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ap/gway-uplink-recovery'
loader = importlib.machinery.SourceFileLoader('uplink_recovery', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)


def route(dev, metric, gateway='192.168.1.1', protocol='static'):
    return {'dev': dev, 'metric': metric, 'gateway': gateway, 'protocol': protocol, '_owned': protocol == m.PROTOCOL}


def setup_cycle(monkeypatch, tmp_path, rows, online):
    monkeypatch.setattr(m, 'STATE', tmp_path / 'state.json')
    monkeypatch.setattr(m, 'routes', lambda: rows)
    monkeypatch.setattr(m, 'candidates', lambda r: sorted([x for x in r if not x['_owned']], key=lambda x: x['metric']))
    monkeypatch.setattr(m, 'healthy', lambda dev: online[dev])
    changes = []
    monkeypatch.setattr(m, 'alter', lambda verb, r: changes.append((verb, r['dev'])))
    return changes


def test_switch_requires_repeated_failure_and_healthy_alternative(monkeypatch, tmp_path):
    rows = [route('wifi', 50), route('eth0', 100)]
    changes = setup_cycle(monkeypatch, tmp_path, rows, {'wifi': False, 'eth0': True})
    m.cycle(); m.cycle()
    assert changes == []
    m.cycle()
    assert changes == [('add', 'eth0')]


def test_no_switch_when_both_paths_fail(monkeypatch, tmp_path):
    rows = [route('wifi', 50), route('eth0', 100)]
    changes = setup_cycle(monkeypatch, tmp_path, rows, {'wifi': False, 'eth0': False})
    for _ in range(5): m.cycle()
    assert changes == []


def test_restores_only_after_sustained_preferred_health(monkeypatch, tmp_path):
    rows = [route('wifi', 50), route('eth0', 100), route('eth0', 5, protocol=m.PROTOCOL)]
    online = {'wifi': True, 'eth0': True}
    changes = setup_cycle(monkeypatch, tmp_path, rows, online)
    for _ in range(3): m.cycle()
    assert changes == []
    online['wifi'] = False
    m.cycle()
    online['wifi'] = True
    for _ in range(4): m.cycle()
    assert changes == [('del', 'eth0')]


def test_foreign_high_priority_route_is_not_overridden(monkeypatch, tmp_path):
    rows = [route('vpn', 1), route('wifi', 50), route('eth0', 100)]
    changes = setup_cycle(monkeypatch, tmp_path, rows, {'vpn': False, 'wifi': False, 'eth0': True})
    for _ in range(5): m.cycle()
    assert changes == []


def test_candidates_exclude_ap_and_local_gateway(monkeypatch):
    def cmd(args, timeout=12):
        if args[0] == 'nmcli' and args[1] == '-g' and args[2].startswith('GENERAL'):
            output = 'ethernet\n100 (connected)\nwired\n'
        elif args[:4] == ['ip', '-j', '-4', 'address']:
            output = json.dumps([{'addr_info': [{'family': 'inet', 'local': '192.168.1.1'}]}])
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, output, '')
    monkeypatch.setattr(m, 'command', cmd)
    assert m.candidates([route('wlan0', 50), route('eth0', 100)]) == []


def test_ap_mode_is_never_a_candidate_even_if_default_route_exists(monkeypatch):
    def cmd(args, timeout=12):
        if args[0] == 'nmcli' and args[2].startswith('GENERAL'):
            out = 'wifi\n100 (connected)\nportal\n'
        elif args[0] == 'nmcli':
            out = 'ap\n'
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, out, '')
    monkeypatch.setattr(m, 'command', cmd)
    assert m.candidates([route('wlan8', 50)]) == []


def test_failed_override_uses_another_proven_path(monkeypatch, tmp_path):
    rows = [route('preferred', 50), route('backup', 100), route('third', 200), route('backup', 5, protocol=m.PROTOCOL)]
    changes = setup_cycle(monkeypatch, tmp_path, rows, {'preferred': False, 'backup': False, 'third': True})
    for _ in range(3): m.cycle()
    assert changes == [('replace', 'third')]


def test_owned_routes_use_kernel_protocol_filter_not_displayed_name(monkeypatch):
    base = {'dev': 'eth0', 'metric': 5, 'gateway': '192.168.1.1', 'protocol': 'locally-named-protocol'}
    def cmd(args, timeout=12):
        return subprocess.CompletedProcess(args, 0, json.dumps([base]), '')
    monkeypatch.setattr(m, 'command', cmd)
    rows = m.routes()
    assert m.override(rows)['dev'] == 'eth0'
