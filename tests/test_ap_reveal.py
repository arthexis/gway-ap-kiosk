"""Regression checks for the read-only AP reveal command."""
import importlib.machinery
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ap/prodigy-ap-password"
loader = importlib.machinery.SourceFileLoader("ap_password", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
ap = importlib.util.module_from_spec(spec)
loader.exec_module(ap)


class RevealTests(unittest.TestCase):
    def test_reveal_reads_profile_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "ap.nmconnection"
            profile.write_text("[connection]\nid=PRODIGY\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=sample-secret\n")
            output = io.StringIO()
            with patch.object(ap, "find_profile_file", return_value=profile), \
                 patch.object(ap, "rotate_password", side_effect=AssertionError("rotated")), \
                 patch.object(ap, "apply_password", side_effect=AssertionError("applied")), \
                 patch.object(ap.os, "geteuid", return_value=0), \
                 patch("sys.argv", ["prodigy-ap-password", "reveal"]), \
                 redirect_stdout(output):
                self.assertEqual(ap.main(), 0)
            self.assertEqual(output.getvalue(), "sample-secret\n")
            self.assertIn("psk=sample-secret", profile.read_text())


if __name__ == "__main__":
    unittest.main()
