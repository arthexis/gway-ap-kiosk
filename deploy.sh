#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Increment 1: stage a verified release, without changing live paths or services.
if [[ "${1:-}" == "--stage-release" ]]; then
  shift
  [[ $# -eq 0 ]] || { echo "Usage: ./deploy.sh --stage-release" >&2; exit 2; }
  [[ $(id -u) -ne 0 ]] || { echo "Run as the non-root operator" >&2; exit 1; }
  state_dir="$HOME/.local/state/gway-ap-kiosk"
  [[ ! -L "$HOME/.local/state" && ! -L "$state_dir" ]] || {
    echo "Unsafe stage lock directory" >&2; exit 2;
  }
  (umask 077; mkdir -p "$state_dir")
  [[ ! -L "$state_dir/deploy.lock" ]] || { echo "Unsafe stage lock file" >&2; exit 2; }
  (umask 077; : >> "$state_dir/deploy.lock")
  exec 9<>"$state_dir/deploy.lock"
  flock -n 9 || { echo "Another AP Kiosk operation holds the lock" >&2; exit 3; }
  exec ansible-playbook -i localhost, -c local "$repo_dir/ansible/playbooks/stage-release.yml"
fi
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
