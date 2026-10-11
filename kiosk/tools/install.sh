#!/usr/bin/env bash
set -Eeuo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install_config=0
overwrite_config=0

usage() {
  cat <<'EOF'
Usage: tools/install.sh [--install-config] [--overwrite-config]

Install gway-kiosk scripts, man pages, systemd units, and desktop launcher.
Config examples are installed only with --install-config. Existing config files
are preserved unless --overwrite-config is also passed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-config)
      install_config=1
      ;;
    --overwrite-config)
      overwrite_config=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done

install_user_file() {
  local mode="$1"
  local src="$2"
  local dest="$3"
  install -D -m "${mode}" "${repo_dir}/${src}" "${dest}"
}

install_root_file() {
  local mode="$1"
  local src="$2"
  local dest="$3"
  sudo install -D -m "${mode}" "${repo_dir}/${src}" "${dest}"
}

install_config_file() {
  local mode="$1"
  local src="$2"
  local dest="$3"
  if [[ -e "${dest}" && "${overwrite_config}" -ne 1 ]]; then
    printf 'preserve existing config: %s\n' "${dest}"
    return 0
  fi
  install_root_file "${mode}" "${src}" "${dest}"
}

# Install the new explicit rotation helper before deploying a kiosk that invokes it.
# The legacy reveal helper and root controller are intentionally NOT replaced here.
install -D -m 0755 "${repo_dir}/../scripts/ap/prodigy-ap-rotate" /home/arthe/.local/bin/prodigy-ap-rotate
install_user_file 0755 bin/gway-kiosk-status /home/arthe/.local/bin/gway-kiosk-status
install_user_file 0755 bin/kiosk-mode /home/arthe/.local/bin/kiosk-mode
install_user_file 0755 bin/arthexis-kiosk-login-session /home/arthe/.local/bin/arthexis-kiosk-login-session
install_root_file 0755 sbin/arthexis-hdmi-kiosk-layout /usr/local/sbin/arthexis-hdmi-kiosk-layout
install_root_file 0755 sbin/arthexis-hdmi-kiosk-session /usr/local/sbin/arthexis-hdmi-kiosk-session
install_root_file 0755 sbin/arthexis-vnc-kiosk-session /usr/local/sbin/arthexis-vnc-kiosk-session
install_root_file 0755 libexec/gway-boot-stagger-until /usr/local/libexec/gway-boot-stagger-until

install_user_file 0644 man/man1/gway-kiosk-status.1 /home/arthe/.local/share/man/man1/gway-kiosk-status.1
install_user_file 0644 man/man1/kiosk-mode.1 /home/arthe/.local/share/man/man1/kiosk-mode.1

install_root_file 0644 systemd/gway-kiosk-status.service /etc/systemd/system/gway-kiosk-status.service
install_root_file 0644 systemd/arthexis-hdmi-kiosk-layout.service /etc/systemd/system/arthexis-hdmi-kiosk-layout.service
install_root_file 0644 systemd/arthexis-hdmi-kiosk-layout.service.d/20-bastion-refresh-order.conf /etc/systemd/system/arthexis-hdmi-kiosk-layout.service.d/20-bastion-refresh-order.conf
install_root_file 0644 systemd/arthexis-hdmi-kiosk-layout.service.d/98-boot-stagger-30s.conf /etc/systemd/system/arthexis-hdmi-kiosk-layout.service.d/98-boot-stagger-30s.conf
install_root_file 0644 systemd/arthexis-hdmi-kiosk.service /etc/systemd/system/arthexis-hdmi-kiosk.service
install_root_file 0644 systemd/arthexis-hdmi-kiosk.service.d/98-boot-stagger-30s.conf /etc/systemd/system/arthexis-hdmi-kiosk.service.d/98-boot-stagger-30s.conf
install_root_file 0644 systemd/arthexis-vnc-kiosk.service /etc/systemd/system/arthexis-vnc-kiosk.service
install_root_file 0644 systemd/arthexis-vnc-kiosk.service.d/10-allow-kiosk-cdn.conf /etc/systemd/system/arthexis-vnc-kiosk.service.d/10-allow-kiosk-cdn.conf
install_root_file 0644 systemd/arthexis-vnc-kiosk.service.d/98-boot-stagger-30s.conf /etc/systemd/system/arthexis-vnc-kiosk.service.d/98-boot-stagger-30s.conf
install_root_file 0644 systemd/bastion-usb-refresh.service.d/60-kiosk-layout.conf /etc/systemd/system/bastion-usb-refresh.service.d/60-kiosk-layout.conf

install_root_file 0644 config/arthexis-kiosk/openbox-rc.xml /etc/arthexis-kiosk/openbox-rc.xml
install_user_file 0644 "desktop/Kiosk Mode.desktop" "/home/arthe/Desktop/Kiosk Mode.desktop"
install_user_file 0644 applications/kiosk-mode.desktop /home/arthe/.local/share/applications/kiosk-mode.desktop

if [[ "${install_config}" -eq 1 ]]; then
  install_config_file 0644 defaults/arthexis-hdmi-kiosk.example /etc/default/arthexis-hdmi-kiosk
  install_config_file 0644 defaults/arthexis-hdmi-kiosk-layout.example /etc/default/arthexis-hdmi-kiosk-layout
  install_config_file 0644 defaults/arthexis-vnc-kiosk.example /etc/default/arthexis-vnc-kiosk
  install_config_file 0640 config/arthexis-kiosk/vnc.conf.example /etc/arthexis-kiosk/vnc.conf
fi

sudo systemctl daemon-reload
printf 'installed gway-kiosk from %s\n' "${repo_dir}"
