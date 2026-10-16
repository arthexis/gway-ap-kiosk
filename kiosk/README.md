# GWAY HDMI Kiosk

The HDMI kiosk is a standalone local interface for kiosk and Wi-Fi AP status. It does not start or proxy Arthexis, OCPP, RFID, LCD, or VNC services.

## Runtime

- `bin/gway-kiosk-status`: loopback HTTP status server at `127.0.0.1:8899`; AP credentials are excluded from its responses.
- `bin/kiosk-mode`: local HDMI kiosk control with bastion-key safety checks.
- `bin/arthexis-kiosk-login-session`: legacy-named local login entrypoint.
- `sbin/arthexis-hdmi-kiosk-session` and `sbin/arthexis-hdmi-kiosk-layout`: legacy-named local HDMI entrypoints.

The `arthexis-` prefix on the retained executable and systemd names is historical naming only. These units must not depend on or start an Arthexis application service. Renaming them requires coordinated updates to systemd, desktop entries and deployment, and is separate from removing application dependencies.

## Installation

Use `../deploy.sh` with its Ansible playbook. The former `tools/install.sh` and Prodigy password helpers have been removed. This branch does not modify running appliances or provide migration and uninstall scripts.

## Verification

```sh
python3 -m pytest -q tests
./tools/verify.sh
```

The local status data endpoint is `http://127.0.0.1:8899/kiosk/status/data/`.
