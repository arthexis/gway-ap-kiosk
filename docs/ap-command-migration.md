# AP reveal / rotate migration (issue #3)

- `prodigy-ap-reveal` discloses the persisted NetworkManager profile PSK on stdout, without rotating, reapplying, or using the LCD. This is sensitive output; use only from an authorized local session and never log it.
- `prodigy-ap-rotate` explicitly rotates the password and reapplies the AP connection. It can disconnect clients. `--display` additionally invokes the LCD helper.
- `prodigy-ap-password boot-rotate` remains unchanged.
- Install the wrappers with executable mode (0755), preserving the previous deployed wrappers for rollback. Never install/restart while AP clients depend on connectivity.
- The kiosk reset endpoint on GWAY-001 currently points to the old reveal executable. **Do not deploy the new reveal wrapper until the reset endpoint has been migrated to rotate** and tests updated. The endpoint is not present in the checked-in `main` kiosk status script; the field checkout contains changes not represented on main.
- Credential source: the controller's reveal reads the persisted NetworkManager keyfile. Runtime NM state can diverge; verify consistency before treating this as an authoritative live QR source.
- Harden passwordless sudo separately; wrapper scripts are not an authorization boundary.
