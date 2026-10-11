# AP reveal / rotate migration (issue #3)

- `prodigy-ap-reveal` discloses the persisted NetworkManager profile PSK on stdout, without rotating, reapplying, or using the LCD. This is sensitive output; use only from an authorized local session and never log it.
- `prodigy-ap-rotate` explicitly rotates the password and reapplies the AP connection. It can disconnect clients. `--display` additionally invokes the LCD helper.
- `prodigy-ap-password boot-rotate` remains unchanged.
- Install the wrappers with executable mode (0755), preserving the previous deployed wrappers for rollback. Never install/restart while AP clients depend on connectivity.
- The kiosk reset endpoint has been selectively restored to the PR branch from the field branch, with a loopback-only POST and explicit `prodigy-ap-rotate --display` target. **Deploy the kiosk update and rotate wrapper before replacing the old reveal wrapper**; otherwise the deployed reset endpoint may invoke the newly read-only reveal helper. The kiosk command is configured at `/home/arthe/.local/bin/prodigy-ap-rotate`; install the executable there before enabling the updated kiosk.
- Credential source: the controller's reveal reads the persisted NetworkManager keyfile. Runtime NM state can diverge; verify consistency before treating this as an authoritative live QR source.
- Harden passwordless sudo separately; wrapper scripts are not an authorization boundary.
