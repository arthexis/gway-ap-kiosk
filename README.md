# gway-ap-kiosk

GWAY-owned host integrations for access-point onboarding, kiosk display, RFID
mode routing, and AP/wifi helpers.

This repository exists to keep Raspberry Pi gateway behavior out of the
Arthexis suite repository. Arthexis may still be the upstream web endpoint or
database/API that a gateway talks to, but the local adapter logic belongs here.

## Contents

- `scripts/ap_portal_server.py` and `config/data/ap_portal/`: captive/AP portal
  source.
- `scripts/ap/`: AP password rotation and nftables authorization helpers.
- `scripts/gway/rfid*`: host-local RFID publisher, mode runner, registration,
  and action helpers.
- `config/systemd/`: redacted systemd units for AP, RFID, and wifi-role
  services.
- `config/nftables/`: redacted nftables templates. Do not commit live MAC
  allowlists.
- `kiosk/`: imported standalone GWAY kiosk repo snapshot.
- `docs/`: runbooks and man pages copied from the GWAY source promotion work.

## Boundaries

Keep these out of this repository:

- live RFID card IDs, card trees, scan JSON, and reader logs
- AP passwords, authorized MAC files, nftables live rules with real MACs
- VNC passwords, kiosk auto-display tokens, browser profiles, and caches
- Arthexis application code that is not a host adapter

The Arthexis repo should expose stable suite contracts. This repo owns the
GWAY-specific service units, local scripts, templates, and field runbooks.
