"""Static safety checks for AP wrappers and kiosk installation."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AP = ROOT / "scripts/ap"


class ApWrapperTests(unittest.TestCase):
    def run_wrapper(self, name, args=(), *, fail_on=None):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "sudo"
            log = Path(directory) / "calls"
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$AP_CALLS"\ncase "$*" in *"$AP_FAIL_ON"*) [ -z "$AP_FAIL_ON" ] || exit 1 ;; esac\n')
            fake.chmod(0o755)
            lcd = Path(directory) / "lcd"
            lcd.write_text('#!/bin/sh\nprintf "LCD\\n" >> "$AP_CALLS"\n')
            lcd.chmod(0o755)
            import os
            env = dict(os.environ, PATH=directory + ":" + os.environ["PATH"], AP_CALLS=str(log),
                       PRODIGY_AP_LCD_HELPER=str(lcd), AP_FAIL_ON=fail_on or "")
            result = subprocess.run(["bash", str(AP / name), *args], capture_output=True,
                                    text=True, env=env, check=False)
            return result, log.read_text() if log.exists() else ""

    def test_reveal_only_reads(self):
        result, calls = self.run_wrapper("prodigy-ap-reveal")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls.splitlines(), ["-n /usr/local/sbin/prodigy-ap-password reveal"])

    def test_reveal_rejects_extra_arguments(self):
        result, calls = self.run_wrapper("prodigy-ap-reveal", ["--display"])
        self.assertEqual(result.returncode, 2)
        self.assertFalse(calls)

    def test_rotate_without_lcd(self):
        result, calls = self.run_wrapper("prodigy-ap-rotate")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls.splitlines(), [
            "-n /usr/local/sbin/prodigy-ap-password rotate",
            "-n /usr/local/bin/prodigy-nmcli connection up PRODIGY",
        ])

    def test_failed_rotation_skips_network_reapply(self):
        result, calls = self.run_wrapper("prodigy-ap-rotate", fail_on="password rotate")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("connection up", calls)

    def test_failed_network_reapply_is_reported(self):
        result, calls = self.run_wrapper("prodigy-ap-rotate", fail_on="connection up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("password rotate", calls)

    def test_invalid_rotate_option_has_no_side_effects(self):
        result, calls = self.run_wrapper("prodigy-ap-rotate", ["--invalid"])
        self.assertEqual(result.returncode, 2)
        self.assertFalse(calls)

    def test_kiosk_installer_has_rotate_dependency(self):
        script = (ROOT / "kiosk/tools/install.sh").read_text()
        self.assertIn("scripts/ap/prodigy-ap-rotate", script)
        self.assertIn("/home/arthe/.local/bin/prodigy-ap-rotate", script)
        self.assertLess(script.index("scripts/ap/prodigy-ap-rotate"),
                        script.index("bin/gway-kiosk-status /home/arthe/.local/bin/gway-kiosk-status"))


if __name__ == "__main__":
    unittest.main()
