"""Exercise route ownership and failover in an isolated network namespace."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ap/gway-uplink-recovery'


def test_real_kernel_route_failover_and_recovery():
    if os.geteuid() != 0 or not shutil.which('unshare') or not shutil.which('ip'):
        pytest.skip('requires root, iproute2 and an isolated network namespace')
    check = subprocess.run(['unshare', '--net', 'true'], capture_output=True, text=True)
    if check.returncode:
        pytest.skip('network namespaces unavailable')
    program = r'''
import importlib.machinery, importlib.util, pathlib, subprocess, sys, tempfile

def run(*args):
    subprocess.run(args, check=True, capture_output=True)
run('ip', 'link', 'set', 'lo', 'up')
for name, net, metric in [('up_a', '201', '50'), ('up_b', '202', '100'), ('up_c', '203', '150')]:
    run('ip', 'link', 'add', name, 'type', 'dummy')
    run('ip', 'link', 'set', name, 'up')
    run('ip', 'address', 'add', f'10.{net}.0.2/24', 'dev', name)
    run('ip', 'route', 'add', 'default', 'via', f'10.{net}.0.1', 'dev', name, 'metric', metric)
loader = importlib.machinery.SourceFileLoader('uplink', sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
m.STATE = pathlib.Path(tempfile.mkdtemp()) / 'state.json'
m.candidates = lambda rows: sorted((r for r in rows if not r.get('_owned')), key=lambda r:r['metric'])
health = {'up_a': False, 'up_b': True, 'up_c': True}
m.healthy = lambda dev: health[dev]
for _ in range(3): m.cycle()
assert m.override(m.routes())['dev'] == 'up_b'
health['up_b'] = False
for _ in range(3): m.cycle()
assert m.override(m.routes())['dev'] == 'up_c'
health['up_a'] = True
for _ in range(4): m.cycle()
rows = m.routes()
assert m.override(rows) is None and len(rows) == 3
'''
    result = subprocess.run(['unshare', '--net', sys.executable, '-c', program, str(SCRIPT)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
