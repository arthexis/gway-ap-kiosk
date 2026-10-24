"""Regression checks for nonblocking kiosk AP discovery and revision markers."""
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / "kiosk/bin/gway-kiosk-status"
loader = importlib.machinery.SourceFileLoader("gway_kiosk_status_test", str(source))
spec = importlib.util.spec_from_loader(loader.name, loader)
status = importlib.util.module_from_spec(spec)
loader.exec_module(status)


class SnapshotTests(unittest.TestCase):
    def test_probe_longer_than_http_deadline_is_nonblocking(self):
        sample = {"available": True, "ssid": "Example", "interface": "wlan0",
                  "updated_at": "2026-10-22T00:00:00+00:00", "state": "selected"}
        clock = [0.]
        snapshot = status.APStatusSnapshot(clock=lambda: clock[0])
        snapshot.update(sample)
        # The cached HTTP response does not invoke a slow external process.
        with mock.patch.object(status.subprocess, "run", side_effect=AssertionError("synchronous probe")):
            self.assertEqual(snapshot.read()["ssid"], "Example")
        with mock.patch.object(status.AP_STATUS_COMMAND, "is_file", return_value=True), mock.patch.object(
            status.subprocess, "run", side_effect=subprocess.TimeoutExpired("kiosk-ap", 15)
        ):
            self.assertIsNone(status.probe_ap())
        self.assertEqual(snapshot.read()["state"], "selected")

    def test_expired_snapshot_is_explicitly_stale(self):
        clock = [0.]
        snapshot = status.APStatusSnapshot(clock=lambda: clock[0])
        snapshot.update({"available": True, "ssid": "AP", "interface": "wlan0",
                         "updated_at": "2026-10-22T00:00:00+00:00", "state": "selected"})
        clock[0] = status.AP_STALE_SECONDS + 1
        self.assertEqual(snapshot.read()["state"], "stale")
        self.assertFalse(snapshot.read()["available"])
        self.assertEqual(snapshot.read()["ssid"], "AP")

    def test_genuinely_unavailable_probe_replaces_selected_sample(self):
        clock = [0.]
        snapshot = status.APStatusSnapshot(clock=lambda: clock[0])
        snapshot.update({"available": True, "ssid": "AP", "interface": "wlan0",
                         "updated_at": "previous", "state": "selected"})
        with mock.patch.object(status.AP_STATUS_COMMAND, "is_file", return_value=True), mock.patch.object(
            status.subprocess, "run", return_value=mock.Mock(stdout='{"state":"unavailable","ap":null}', returncode=1)
        ):
            snapshot.update(status.probe_ap())
        self.assertEqual(snapshot.read()["state"], "unavailable")
        self.assertEqual(snapshot.read()["ssid"], "")

    def test_other_discovery_states_and_html(self):
        for state in ("not_configured", "ambiguous", "invalid_selection"):
            with self.subTest(state=state), mock.patch.object(status.AP_STATUS_COMMAND, "is_file", return_value=True), mock.patch.object(
                status.subprocess, "run", return_value=mock.Mock(stdout='{"state":"' + state + '","ap":null}', returncode=1)
            ):
                data = status.probe_ap()
                self.assertEqual(data["state"], state)
                self.assertTrue(data["updated_at"])
                self.assertIn(state, status.render_html({"ap": data}))

    def test_revision_marker_writes_sha_without_literal_backslash_n(self):
        # Producer contract: valid 40-hex SHA, no text escape suffix.
        playbook = (ROOT / "ansible/playbooks/install.yml").read_text()
        self.assertIn("rev-parse HEAD') | trim", playbook)
        self.assertNotIn("rev-parse HEAD') | trim }}\\\\n", playbook)


if __name__ == "__main__":
    unittest.main()
