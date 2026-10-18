"""Only a successful runtime-enabled deployment may publish kiosk revision evidence."""
from pathlib import Path
import unittest


class InstalledRevisionContract(unittest.TestCase):
    def test_revision_marker_committed_after_health_check(self):
        playbook = (Path(__file__).resolve().parents[1] / "ansible/playbooks/install.yml").read_text()
        self.assertIn("Record verified installed kiosk revision", playbook)
        self.assertIn("/var/lib/gway-ap-kiosk/installed-revision", playbook)
        self.assertIn("git -C ", playbook)
        self.assertLess(playbook.index("Check standalone kiosk HTTP response"),
                        playbook.index("Record verified installed kiosk revision"))
        self.assertIn("when: not kiosk_ci_mode | bool\n      become: true\n      ansible.builtin.copy:\n        dest: /var/lib/gway-ap-kiosk/installed-revision", playbook)
