#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
started=$SECONDS
# A verified same-revision installation needs no Ansible convergence.
# Explicit Ansible options always force the full playbook.
if [[ $# -eq 0 ]]; then
  revision=$(git -C "$repo_dir" rev-parse HEAD)
  installed=$(cat /var/lib/gway-ap-kiosk/installed-revision 2>/dev/null || true)
  if [[ "$revision" == "$installed" ]] &&
     [[ -x /usr/local/bin/gway-kiosk-status ]] &&
     [[ -x /usr/local/sbin/kiosk-ap ]] &&
     [[ -f /etc/systemd/system/gway-kiosk-status.service ]] &&
     systemctl is-active --quiet gway-kiosk-status.service &&
     curl --fail --silent --show-error --max-time 3 --output /dev/null http://127.0.0.1:8899/kiosk/status/data/; then
    printf 'Kiosk revision %s already installed and healthy; skipped Ansible (%ds).\n' "$revision" "$((SECONDS-started))"
    exit 0
  fi
fi
ansible-playbook -i localhost, -c local "$repo_dir/ansible/playbooks/install.yml" "$@"
printf 'Kiosk deployment finished in %ds\n' "$((SECONDS-started))"
