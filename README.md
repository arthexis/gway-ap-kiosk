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

## Immutable deployment migration — increment 1 (staging only)

Prepare a verified immutable application payload without changing the active AP,
kiosk service executables, systemd units, sudoers, Polkit rules, user files,
network configuration, or persistent data:

```sh
./deploy.sh --stage-release
```

This requires the non-root operator and Ansible privilege escalation for
root-owned `/opt/gway-ap-kiosk/releases/<full-git-SHA>`. It stages six
application executables, checks staged/source SHA-256 digests, validates
the revision marker, and leaves the existing `/usr/local` executables
untouched. Repeating against the same clean source revision verifies the
existing immutable release rather than modifying it.

The normal `./deploy.sh` installation remains unchanged in increment 1.
**Do not run `--prune --apply` for AP Kiosk yet**; release activation,
rollback, and retention follow in separately reviewed increments. A staged
revision is not proof of an activated or healthy revision.
