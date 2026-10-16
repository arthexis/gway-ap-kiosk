# gway-ap-kiosk

Standalone GWAY Wi-Fi access point and local HDMI kiosk components.

## Scope

This repository owns the local AP controller, onboarding portal and HDMI kiosk. It does **not** provide Arthexis services, OCPP, RFID, LCD output, VNC sessions, or a remote console.

## Components

- `scripts/ap/kiosk-ap`: discover the NetworkManager AP profile, report its status and perform explicitly authorized credential operations.
- `scripts/ap_portal_server.py` and `config/data/ap_portal/`: local AP onboarding portal.
- `kiosk/bin/gway-kiosk-status`: local status page, bound to `127.0.0.1:8899`.
- `kiosk/`: HDMI session, layout and operator-facing launcher.
- `ansible/playbooks/install.yml` and `deploy.sh`: deployment entrypoints.

The AP is selected from NetworkManager state rather than from a fixed SSID or the former Prodigy profile. Do not store credentials, real MAC allowlists, browser profiles, or device-specific secrets in Git.

## Deployment

Use the root-level Ansible-backed `deploy.sh`. Deployment is intended for a fresh appliance; there is no legacy migration or uninstall path. Validate Ansible and the complete CI suite before production use. Hardware validation is performed separately after deployment.

## Tests

```sh
python3 -m pytest -q tests
```

Credential disclosure and rotation require explicit privileges. The status HTTP endpoint must never expose AP credentials.
