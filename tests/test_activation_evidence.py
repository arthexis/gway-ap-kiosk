"""Activation evidence must fail closed for missing and contradictory bytes."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "verify_activation.py"
spec = importlib.util.spec_from_file_location("verify_activation", MODULE)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
SHA = "a" * 40
COMMAND = b"python3\0/usr/local/bin/gway-kiosk-status\0--port\08899\0"


class ActivationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.marker = root / "installed-revision"
        self.marker.write_text(SHA)
        self.releases = root / "releases"
        release = self.releases / SHA
        release.mkdir(parents=True)
        (release / "REVISION").write_text(SHA + "\n")
        self.installed = root / "installed"
        for rel, absolute in verifier.FILES.items():
            staged = release / rel
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(rel.encode())
            installed = self.installed / absolute.lstrip("/")
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(rel.encode())

    def check(self, **kwargs):
        params = dict(pid=42, cmdline=COMMAND, started_at=9999999999.0)
        params.update(kwargs)
        # Synthetic fixture files are owned by the CI user, not root.
        with mock.patch.object(verifier, 'trusted', side_effect=lambda p, directory=False: p.is_dir() if directory else p.is_file()):
            return verifier.verify(self.marker, self.releases, self.installed, **params)

    def test_untrusted_evidence_fails_closed(self):
        self.assertFalse(verifier.verify(self.marker, self.releases, self.installed,
                                         pid=42, cmdline=COMMAND, started_at=9999999999.0))

    def test_valid_matching_staged_and_running(self):
        self.assertTrue(self.check())

    def test_missing_marker_or_stage_is_unknown(self):
        self.marker.unlink()
        self.assertFalse(self.check())
        self.marker.write_text(SHA)
        (self.releases / SHA / "REVISION").unlink()
        self.assertFalse(self.check())

    def test_contradictory_installed_payload_is_rejected(self):
        target = self.installed / "usr/local/bin/gway-kiosk-status"
        target.write_bytes(b"modified")
        self.assertFalse(self.check())

    def test_wrong_process_or_replaced_after_start_is_rejected(self):
        self.assertFalse(self.check(cmdline=b"python3\0/other/app\0"))
        self.assertFalse(self.check(started_at=0))


if __name__ == "__main__":
    unittest.main()
