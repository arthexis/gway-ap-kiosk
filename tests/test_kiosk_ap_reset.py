"""Kiosk AP rotation endpoint regression tests (no live AP commands)."""
import importlib.machinery
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "kiosk/bin/gway-kiosk-status"
loader = importlib.machinery.SourceFileLoader("kiosk_status_ap_test", str(PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
kiosk = importlib.util.module_from_spec(spec)
loader.exec_module(kiosk)


class KioskApResetTests(unittest.TestCase):
    def request(self, client, success=True):
        handler = object.__new__(kiosk.KioskStatusHandler)
        handler.path = kiosk.AP_RESET_PATH
        handler.client_address = (client, 1234)
        sent = []
        handler._send = lambda status, content_type, body: sent.append((status, json.loads(body)))
        handler.log_message = lambda *args: None
        with patch.object(kiosk, "reset_access_point", return_value=success) as command:
            handler.do_POST()
        return sent[0], command

    def test_non_loopback_rejected_without_invocation(self):
        (status, body), command = self.request("192.0.2.1")
        self.assertEqual((status, body), (403, {"ok": False}))
        command.assert_not_called()

    def test_loopback_success(self):
        (status, body), command = self.request("127.0.0.1")
        self.assertEqual((status, body), (200, {"ok": True}))
        command.assert_called_once_with()

    def test_loopback_failure_has_no_command_output(self):
        (status, body), command = self.request("127.0.0.1", False)
        self.assertEqual((status, body), (502, {"ok": False}))
        command.assert_called_once_with()

    def test_rotate_target_and_display_are_explicit(self):
        with patch.object(kiosk.AP_ROTATE_COMMAND, "is_file", return_value=True), \
             patch.object(kiosk.os, "access", return_value=True), \
             patch.object(kiosk.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(kiosk.reset_access_point())
        self.assertEqual(run.call_args.args[0], [str(kiosk.AP_ROTATE_COMMAND), "--display"])
        self.assertTrue(run.call_args.kwargs["capture_output"])


if __name__ == "__main__":
    unittest.main()
