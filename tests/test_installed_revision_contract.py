"""Only a successful runtime-enabled deployment may publish kiosk revision evidence."""
import json
from pathlib import Path
import unittest


class InstalledRevisionContract(unittest.TestCase):
    def test_marker_template_renders_exact_sha_bytes(self):
        playbook = (Path(__file__).resolve().parents[1] / "ansible/playbooks/install.yml").read_text()
        marker_section = playbook.split("dest: /var/lib/gway-ap-kiosk/installed-revision", 1)[1]
        encoded = next(line.strip().removeprefix("content: ") for line in marker_section.splitlines()
                       if line.strip().startswith("content: "))
        template = json.loads(encoded)
        expected = "{{ lookup('pipe', 'git -C ' ~ project_root ~ ' rev-parse HEAD') | trim }}"
        self.assertEqual(template, expected)
        sha = "a" * 40
        produced = template.replace(expected, sha).encode("ascii")
        self.assertEqual(produced, sha.encode("ascii"))
        self.assertEqual(len(produced), 40)

    def test_revision_marker_committed_after_health_check(self):
        playbook = (Path(__file__).resolve().parents[1] / "ansible/playbooks/install.yml").read_text()
        self.assertIn("Record verified installed kiosk revision", playbook)
        self.assertIn("/var/lib/gway-ap-kiosk/installed-revision", playbook)
        self.assertIn("git -C ", playbook)
        self.assertLess(playbook.index("Check standalone kiosk HTTP response"),
                        playbook.index("Record verified installed kiosk revision"))
        self.assertIn("when: not kiosk_ci_mode | bool\n      become: true\n      ansible.builtin.copy:\n        dest: /var/lib/gway-ap-kiosk/installed-revision", playbook)
