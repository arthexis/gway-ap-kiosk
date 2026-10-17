#!/usr/bin/env bash
set -Eeuo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

python3 -m py_compile \
  bin/gway-kiosk-status \
  sbin/gway-hdmi-kiosk-layout

bash -n \
  bin/kiosk-mode \
  sbin/gway-hdmi-kiosk-session \
  libexec/gway-boot-stagger-until \
  tools/verify.sh

bin/gway-kiosk-status --self-test

if command -v man >/dev/null 2>&1; then
  MANPATH="${repo_dir}/man:${MANPATH:-}" man --warnings -P cat gway-kiosk-status >/dev/null
  MANPATH="${repo_dir}/man:${MANPATH:-}" man --warnings -P cat kiosk-mode >/dev/null
fi

if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "desktop/Kiosk Mode.desktop"
  desktop-file-validate applications/kiosk-mode.desktop
fi

python3 - <<'PY'
from pathlib import Path

for path in Path("systemd").glob("*.service.d/*.conf"):
    first_real_line = next(
        (line.strip() for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")),
        "",
    )
    if first_real_line not in {"[Unit]", "[Service]", "[Install]"}:
        raise SystemExit(f"{path}: first non-comment line is not a unit section")
PY

systemd-analyze verify systemd/*.service

printf 'verify ok\n'
