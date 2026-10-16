#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ansible-playbook -i localhost, -c local "$repo_dir/ansible/playbooks/install.yml" "$@"
