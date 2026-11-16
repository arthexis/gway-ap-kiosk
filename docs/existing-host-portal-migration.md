# Existing-host AP portal migration — issue #31

This is **not** the repository's fresh-appliance `deploy.sh` path. No change to the
active AP is permitted by merely running the read-only preflight.

## Phase 0 — establish a rollback boundary

1. Identify main commit, clean checkout, all current writers and lock holders.
2. Run `python3 scripts/portal_migration_preflight.py` on the existing host.
   Its JSON reports presence only; it intentionally does not inspect, log, or
   export identities, MACs, consent events, or stored credentials.
3. Separately inventory systemd unit definitions and drop-ins, the exact Nginx
   site and upstream, nftables ownership and hooks (including `inet host_security`
   and `inet gway_ap_portal`), the old sync helper and any shared `/opt/arthexis`
   dependencies. Record sanitized paths and effective owners, not private data.
4. Determine whether grants in `ap_allowed_macs` are independently managed.
   A portal-owned allowlist is **not** evidence that other grants may be removed.
5. With an approved maintenance/rollback window, make restricted offline backups
   of state, exact ruleset, service definitions and Nginx configuration. Verify
   restoration permissions and location **before** enabling the replacement.

## Required firewall contract before activation

The portal currently owns an nftables forward chain and `authorized_macs` set,
while the separate host firewall uses `ap_allowed_macs`. An nftables `accept`
in one base chain does **not** override a later `drop` in another base chain.
The replacement must preserve independently managed grants, require both
layers to agree for newly registered devices, and be fail-closed when the host
set or another prerequisite is missing. Use a coordinated, rollback-capable
transaction; do not flush or replace `inet host_security` or other tables.
Avoid asserting registration success until state, both firewall layers and
consent logging are consistent. Reconcile persisted grants on restart without
removing independent access.

## Deployment gates (not yet implemented)

- Install an immutable supported portal runtime, local-only `127.0.0.1:9080`,
  static assets, stable restricted state directory and `gway-ap-portal.service`.
- Use only supported CLI flags; never use retired checkout/venv paths or
  `--suite-login-host`. Ensure boot enablement, Nginx proxy and direct `/health`.
- Prove migration/rollback of authorization, trusted-device, consent and activity
  records in hermetic tests, without exposing their content in receipts.
- Confirm independent grants, AP clients, SSH, USB uplink, WireGuard,
  Watchtower providers, and the conditional gway-004/OCPP path in the field.
- Verify service restart and persistence before quarantining *exact* obsolete
  artifacts. Do not delete shared `/opt/arthexis` resources or old assets still
  referenced by another component.

**Hold:** Do not deploy, enable portal firewall sync, retire old artifacts or
close issue #31 on the basis of this preflight alone. Physical client acceptance
and sanitized installation/rollback receipts remain mandatory.
