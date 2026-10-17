# Repository boundary audit — issue #10 / PR #1

This is an implementation inventory, not a migration or uninstall procedure. No deployed node is touched.

## Retain
- AP / Wi-Fi configuration and captive portal owned by this repository.
- Explicit AP password rotation and non-mutating reveal, with careful credential handling.
- Local kiosk UI, kiosk-mode activation and necessary local desktop integration.
- Minimal service units required for the two functions above.

## Remove from the repository (not from already deployed devices)
| Component | Existing paths / references to inspect | Replacement owner |
|---|---|---|
| Charger field overlay and OCPP probe | `kiosk/bin/gway-kiosk-status`, `kiosk/bin/kiosk-mode`, `kiosk/tests/test_field_view.py`, kiosk defaults | `ocpp-csms` for OCPP; kiosk does not embed charger views |
| Arthexis console, reverse proxy, restart actions | `kiosk/bin/gway-kiosk-status`, `kiosk/systemd/gway-kiosk-status.service`, tests | No replacement in kiosk |
| LCD reader/history/rotation/display helpers | `kiosk/bin/gway-kiosk-status`, `scripts/ap/prodigy-password-lcd-60s`, LCD-related docs and integration | `gway-lcd-sound` |
| RFID reader, queue, dispatcher and services | `scripts/gway/rfid*`, `config/systemd/gway/rfid/`, `apps/cards/tests/`, `tests/test_rfid*`, RFID docs | `gway-rfid`; coordinate with its issue #9 |
| Kiosk-specific VNC | `kiosk/sbin/arthexis-vnc-kiosk-session`, `kiosk/systemd/arthexis-vnc-kiosk.service*`, `kiosk/config/arthexis-kiosk/vnc.conf.example`, installer and docs | Device desktop remote access, outside kiosk |
| Legacy suite-specific names, session paths, systemd bindings | `kiosk/`, `config/`, `docs/`, `README.md` | Retain only where required for independent kiosk operation |

## Implementation order and guards
1. Remove optional UI/HTTP integrations and their behavioral tests together; do not leave callable privileged endpoints.
2. Remove RFID and LCD implementation and references once owner boundaries are confirmed; do not change live units.
3. Remove dedicated VNC implementation and service configuration. Preserve only standard local desktop/kiosk activation.
4. Replace legacy installer with fresh-install Ansible in a later stage; do not make a temporary uninstall/migration script.
5. Test that AP and kiosk functions work in isolation, including no dependencies on Arthexis, OCPP, LCD, RFID or VNC.
6. Add repository to GUI Remote's automatic deploy list as one independently tracked task, invoking its Ansible entrypoint.

Scope: changes are limited to source, tests, documentation and future installation defaults. Deployed systems are managed separately. This audit intentionally leaves the current production scripts intact until the replacements can be tested as a coherent change.
