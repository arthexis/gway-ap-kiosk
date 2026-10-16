# GWAY AP Kiosk

This repository owns the local Wi-Fi access point and HDMI kiosk interface. It does not implement RFID scanning, OCPP, Arthexis console/proxy, LCD output, or a dedicated VNC session.

## Runtime

- `bin/gway-kiosk-status`: local status HTTP server on `127.0.0.1:8899`, displaying kiosk and AP state without disclosing the AP password.
- `bin/kiosk-mode`: operator control for HDMI kiosk mode, subject to the bastion-key safety checks.
- `bin/arthexis-kiosk-login-session`: existing login-session entrypoint (legacy filename; no Arthexis service dependency intended).
- `sbin/arthexis-hdmi-kiosk-session` and `sbin/arthexis-hdmi-kiosk-layout`: existing HDMI session and layout entrypoints (legacy filenames).
- `systemd/`, `defaults/`, `desktop/` and `applications/`: service definitions, non-secret examples and launchers.

Legacy executable and unit filenames are retained until the independent AP/kiosk installation contract is finalized; they do not imply that the removed Arthexis service is installed or started.

## Installation status

The existing `kiosk/tools/install.sh` is a legacy installer, **not** the intended fresh-install contract. It should not be used to provision a new appliance. A root-level `deploy.sh` backed by Ansible is required before this PR is merged. No uninstall, migration, or live-device changes are included in this PR.

## Verify

Run the local static and functional checks:

```bash
./kiosk/tools/verify.sh
python3 -m pytest -q tests/test_kiosk_independence.py tests/test_split_repo_integrity.py
```

The local status endpoint, when its service is running, is `http://127.0.0.1:8899/kiosk/status/data/`. No external suite endpoints are exposed by the kiosk status server.
