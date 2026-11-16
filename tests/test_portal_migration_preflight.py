"""Hermetic tests: never execute nft or systemctl."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts/portal_migration_preflight.py'
spec = importlib.util.spec_from_file_location('portal_migration_preflight', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PreflightTests(unittest.TestCase):
    def test_redacts_state_contents_and_never_authorizes_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            secret = 'aa:bb:cc:dd:ee:ff user@example.invalid'
            for name in module.STATE_FILES:
                (path / name).write_text(secret)
            calls = []
            def stub(*argv):
                calls.append(argv)
                return True
            result = module.inspect(path, command=stub)
            report = json.dumps(result)
            self.assertNotIn(secret, report)
            self.assertNotIn('user@example.invalid', report)
            self.assertTrue(all(result['state_file_presence'].values()))
            self.assertFalse(result['ready_for_automatic_migration'])
            self.assertTrue(result['read_only'])
            self.assertEqual(len(calls), 5)
            self.assertTrue(all(call[0] in {'nft', 'systemctl'} for call in calls))
            self.assertTrue(all('list' in call or 'cat' in call for call in calls))

    def test_missing_tables_and_missing_state_are_reported_not_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            result = module.inspect(Path(directory), command=lambda *args: False)
            self.assertFalse(any(result['state_file_presence'].values()))
            self.assertFalse(result['host_firewall_ap_set_present'])
            self.assertFalse(result['portal_firewall_table_present'])
            self.assertFalse(result['ready_for_automatic_migration'])


if __name__ == '__main__':
    unittest.main()
