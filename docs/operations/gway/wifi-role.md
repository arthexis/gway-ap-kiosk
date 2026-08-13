# GWAY Wi-Fi Role Supervisor

The Wi-Fi role supervisor keeps a Control-node AP available while preserving a
bounded recovery path when the dedicated upstream Wi-Fi interface is down.

Default model:

- `AP_INTERFACE` hosts the local AP connection.
- `UPLINK_INTERFACE` carries upstream internet.
- The supervisor keeps the AP connection active while the uplink is connected.
- If the uplink is down and a configured fallback client SSID is visible or was
  seen recently, the supervisor may activate that fallback client profile on the
  AP interface.
- If fallback activation fails, the supervisor suppresses that SSID briefly and
  reverts the AP interface to the AP connection.

The supervisor does not rewrite AP portal, DHCP, DNS, or firewall bindings. If
the permanent AP interface changes, update those surfaces together.

## Repository Files

| Source path | Install target |
| --- | --- |
| `scripts/gway/arthexis_wifi_role.py` | `/usr/local/bin/arthexis-wifi-role` |
| `config/templates/arthexis-wifi-role.env.template` | `/etc/default/arthexis-wifi-role` after local edit |
| `config/systemd/gway/wifi-role/arthexis-wifi-role.service` | `/etc/systemd/system/arthexis-wifi-role.service` |
| `config/systemd/gway/wifi-role/arthexis-wifi-role.timer` | `/etc/systemd/system/arthexis-wifi-role.timer` |
| `config/networkmanager/dispatcher.d/90-arthexis-wifi-role` | `/etc/NetworkManager/dispatcher.d/90-arthexis-wifi-role` |

## Configuration

Create the AP NetworkManager profile and any fallback client profiles before
enabling the timer. Store Wi-Fi PSKs only in NetworkManager profiles or another
approved secret store, never in repository files.

Copy the public template and edit local values:

```bash
sudo install -m 0644 config/templates/arthexis-wifi-role.env.template \
  /etc/default/arthexis-wifi-role
sudo editor /etc/default/arthexis-wifi-role
```

Required fields:

- `AP_CONNECTION`: NetworkManager profile name for the local AP.
- `AP_INTERFACE`: interface that normally hosts the local AP.
- `UPLINK_INTERFACE`: interface that normally carries upstream internet.
- `SCAN_INTERFACE`: interface used for SSID scans; leave blank to scan the
  uplink interface.
- `PREFERRED_CLIENT_CONNECTIONS`: comma-separated fallback client profile names.
- `STATE_FILE`: runtime state path. Keep the default
  `/var/lib/arthexis-wifi-role/state.json` unless the service state directory
  also changes.

## Install

From the repository root:

```bash
sudo install -d -m 0755 /etc/systemd/system /etc/NetworkManager/dispatcher.d
sudo install -m 0755 scripts/gway/arthexis_wifi_role.py \
  /usr/local/bin/arthexis-wifi-role
sudo install -m 0644 config/systemd/gway/wifi-role/arthexis-wifi-role.service \
  /etc/systemd/system/arthexis-wifi-role.service
sudo install -m 0644 config/systemd/gway/wifi-role/arthexis-wifi-role.timer \
  /etc/systemd/system/arthexis-wifi-role.timer
sudo install -m 0755 config/networkmanager/dispatcher.d/90-arthexis-wifi-role \
  /etc/NetworkManager/dispatcher.d/90-arthexis-wifi-role
sudo systemctl daemon-reload
sudo systemctl enable --now arthexis-wifi-role.timer
```

Run one supervised pass after installation:

```bash
sudo systemctl start arthexis-wifi-role.service
```

## Validation

Check the current role split:

```bash
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status
iw dev
```

Run a dry decision pass:

```bash
sudo sh -c 'set -a; . /etc/default/arthexis-wifi-role; set +a; exec /usr/local/bin/arthexis-wifi-role --dry-run --verbose'
```

Check service and timer state:

```bash
systemctl status arthexis-wifi-role.service --no-pager
systemctl status arthexis-wifi-role.timer --no-pager
journalctl -u arthexis-wifi-role.service -n 50 --no-pager
```

Validate AP clients from the AP layer, not just service health:

```bash
set -a
. /etc/default/arthexis-wifi-role
set +a
iw dev "$AP_INTERFACE" station dump
cat "/var/lib/NetworkManager/dnsmasq-$AP_INTERFACE.leases"
ip neigh
curl -fsS http://127.0.0.1:9080/health
curl -fsS http://10.42.0.1/api/status
```

Expected result when the uplink is healthy: the AP interface stays on the AP
connection and the command prints `action=noop`.

## Rollback

Disable the supervisor and manually restore the AP profile:

```bash
set -a
. /etc/default/arthexis-wifi-role
set +a
sudo systemctl disable --now arthexis-wifi-role.timer
sudo systemctl stop arthexis-wifi-role.service
sudo nmcli connection up id "$AP_CONNECTION" ifname "$AP_INTERFACE"
```

If AP clients still cannot pass traffic after rollback, inspect the AP portal,
DHCP leases, neighbor table, and firewall/allowlist state before changing radio
roles again.
