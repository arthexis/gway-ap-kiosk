# gway-kiosk

Host-local kiosk runtime for GWAY-001.

This repository snapshots the kiosk work installed on this host:

- `bin/gway-kiosk-status`: local status web server on `127.0.0.1:8899`.
- `bin/kiosk-mode`: operator toggle for HDMI kiosk mode while the bastion key is verified.
- `bin/arthexis-kiosk-login-session`: binds locked-down Arthexis kiosk sessions to Chromium profiles.
- `sbin/arthexis-hdmi-kiosk-session`: HDMI Chromium kiosk session.
- `sbin/arthexis-hdmi-kiosk-layout`: bastion-key-aware HDMI layout controller.
- `sbin/arthexis-vnc-kiosk-session`: VNC kiosk browser session.
- `systemd/`: deployed units and drop-ins.
- `defaults/*.example`: non-secret host config templates.
- `config/arthexis-kiosk/openbox-rc.xml`: VNC Openbox policy.
- `config/arthexis-kiosk/vnc.conf.example`: VNC server template without the password.
- `config/labwc/window-rules.xml`: labwc kiosk window-rule snippet.
- `desktop/` and `applications/`: local launchers for `kiosk-mode`.

The deployed status page currently refreshes every 30 seconds and includes local TODO status. The email watcher is intentionally outside this repository except where `gway-kiosk-status` reports its timer state.

## Live charger field view

The optional field view is fail-closed. It can display the local Arthexis OCPP
dashboard only after all of these are true: `KIOSK_FIELD_VIEW_ENABLED=1`, an
operator arms it with `kiosk-mode field on` while the bastion key is present,
the configured Ethernet interface has carrier, and Arthexis reports a live
OCPP websocket. The iframe is removed again within the configured three-second
poll interval when any condition fails.

Set `KIOSK_FIELD_VIEW_CHARGER_IDS` to a comma-separated field-test allowlist
when only selected chargers should enable the view. The kiosk accepts only
loopback HTTP URLs for both the liveness contract and the embedded view.

To prepare a field test, set `KIOSK_FIELD_VIEW_ENABLED=1` in
`/etc/default/arthexis-hdmi-kiosk`, then reload the status service and arm the
temporary view after the charger is connected:

```bash
sudo systemctl restart gway-kiosk-status.service
kiosk-mode field on
```

Use `kiosk-mode field off` to remove the view immediately. No persistent
configuration is changed by either operator command.

## Install

Review `defaults/*.example` and install host-local secrets separately before using a fresh node. The repository does not include the VNC password or RFID auto-display token.

```bash
./tools/install.sh
```

By default, `tools/install.sh` installs scripts, man pages, systemd units, launchers, and non-secret application config. It does not replace existing files under `/etc/default` or `/etc/arthexis-kiosk/vnc.conf`.

To seed missing example configs:

```bash
./tools/install.sh --install-config
```

To replace existing example-backed configs, use:

```bash
./tools/install.sh --install-config --overwrite-config
```

After installation, the helper runs `systemctl daemon-reload`. It does not restart the kiosk services automatically.

## Verify

```bash
./tools/verify.sh
```

The verifier compiles Python and shell files, runs `gway-kiosk-status --self-test`, renders man pages with warnings enabled when `man` is available, validates the desktop launcher when `desktop-file-validate` is available, and checks systemd unit syntax through a temporary tree.

Live checks on this host:

```bash
systemctl is-active gway-kiosk-status.service arthexis-hdmi-kiosk.service arthexis-vnc-kiosk.service
python3 - <<'PY'
import json
from urllib.request import urlopen
payload = json.load(urlopen("http://127.0.0.1:8899/kiosk/status/data/", timeout=5))
print(payload["refresh_seconds"])
PY
```
