# AP reveal / rotate migration (issue #3)

- `prodigy-ap-reveal` discloses the persisted NetworkManager profile PSK on stdout, without rotating, reapplying, or using the LCD. This is sensitive output; use only from an authorized local session and never log it.
- `prodigy-ap-rotate` explicitly rotates the password and reapplies the AP connection. It can disconnect clients. `--display` additionally invokes the LCD helper.
- `prodigy-ap-password boot-rotate` remains unchanged.
- Install the wrappers with executable mode (0755), preserving the previous deployed wrappers for rollback. Never install/restart while AP clients depend on connectivity.
- The kiosk reset endpoint has been selectively restored to the PR branch from the field branch, with a loopback-only POST and explicit `prodigy-ap-rotate --display` target. **Deploy the kiosk update and rotate wrapper before replacing the old reveal wrapper**; otherwise the deployed reset endpoint may invoke the newly read-only reveal helper. The kiosk command is configured at `/home/arthe/.local/bin/prodigy-ap-rotate`; install the executable there before enabling the updated kiosk.
- Credential source: the controller's reveal reads the persisted NetworkManager keyfile. Runtime NM state can diverge; verify consistency before treating this as an authoritative live QR source.
- Harden passwordless sudo separately; wrapper scripts are not an authorization boundary.

## Explicit field migration and safety

Run `kiosk/tools/install.sh --migrate-ap` only during a maintenance window.
The option preflights the controller's reveal support and creates timestamped
backups of existing controller and reveal executables before replacing them.
Without `--migrate-ap`, the installer does not replace those executables.
It never rotates the password as part of installation.

AP rotation through the kiosk is **disabled by default**. To enable it for a
trusted local kiosk, configure `GWAY_KIOSK_ALLOW_AP_ROTATION=1` in the
kiosk service environment and restart that service deliberately. Do not expose
the status service through a reverse proxy; the endpoint also checks loopback,
Host and Origin, but these checks are defense in depth, not authentication.
Disable the option after maintenance. No credentials are returned by reset.

Rollback: restore the matching timestamped backups of
`/usr/local/sbin/prodigy-ap-password` and
`/home/arthe/.local/bin/prodigy-ap-reveal`; restore the previous kiosk
binary and rotation helper from deployment backups if needed. Avoid restarting
NetworkManager or rotating credentials as part of rollback.
