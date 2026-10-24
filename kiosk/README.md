# GWAY HDMI Kiosk

The HDMI kiosk is a standalone local interface for kiosk and Wi-Fi AP status. It does not start or proxy Arthexis, OCPP, RFID, LCD, or VNC services.

## Runtime

- `bin/gway-kiosk-status`: loopback HTTP status server at `127.0.0.1:8899`; AP credentials are excluded from its responses. Its bottom-left panel rotates the published default LCD system-info frames read-only and, only when the Bastion verifier confirms the key, offers the existing guarded `Kiosk Mode Off` action; its bottom-right panel lists the existing FTP drop directory (`/srv/prodigy-ftp/drop`) with in-page refresh and file metadata.
- `bin/kiosk-mode`: local HDMI kiosk control with bastion-key safety checks.
- `sbin/gway-hdmi-kiosk-session` and `sbin/gway-hdmi-kiosk-layout`: legacy-named local HDMI entrypoints.

The `arthexis-` prefix on the retained HDMI executable and systemd names is historical naming only. These units must not depend on or start an Arthexis application service. Renaming them requires coordinated updates to systemd, desktop entries and deployment, and is separate from removing application dependencies.

## Installation

Use `../deploy.sh` with its Ansible playbook. The former `tools/install.sh` and Prodigy password helpers have been removed. This branch does not modify running appliances or provide migration and uninstall scripts.

## Verification

```sh
python3 -m pytest -q tests
./tools/verify.sh
```

The local status data endpoint is `http://127.0.0.1:8899/kiosk/status/data/`.
The kiosk does not operate, reconfigure, or proxy the FTP service; it only reads the drop directory available to the kiosk user. It likewise reads the LCD publisher's public system-info frame file and does not control the LCD service. The Desktop launcher is installed as `Kiosk Mode On` and invokes the existing Bastion-guarded `kiosk-mode on` command. The same-origin `Kiosk Mode Off` control uses that existing command with a narrowly scoped Polkit permission to start only `gway-hdmi-kiosk-layout.service`; the status service retains `NoNewPrivileges=yes`.
