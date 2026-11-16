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

## Candidate staging and additive coordinator (Stage 1, not activated)

`ansible-playbook -i localhost, -c local ansible/playbooks/stage-portal.yml`
stages the Python standard-library server, three static assets, a service
candidate and an Nginx location candidate under a revision-scoped directory
in `/opt/gway-ap-kiosk/portal/candidates/`. It does **not** install or enable
the service, configure Nginx, create persistent state, import legacy records,
change nftables or start the portal. It is not an existing-host migration
command. A reviewed activation step must first verify source cleanliness,
candidate content hashes, stable state ownership and backup restore.

`scripts/portal_firewall_coordinator.py` adds a deliberately **unwired**
coordinated nftables grant primitive. It requires both pre-existing sets
(`inet host_security ap_allowed_macs` and
`inet gway_ap_portal authorized_macs`) of type `ether_addr`. Missing
or incompatible sets fail closed; successful additions use a single nft
batch and preserve independent grants. Its rollback API requires an explicit
exclusive-writer assertion; this is a caller contract, **not yet an
implemented host-wide lock**. Do not use it concurrently with the legacy
sync helper. Neither source file state nor other firewall policies are
rewritten by this primitive.

**Required before activation:** establish the actual host firewall owner,
handle base-chain verdicts (an accept in one chain cannot override a drop in
another), serialize registration and legacy-sync writers, coordinate consent
and authorization-file commit/rollback, verify crash recovery/reconciliation,
ensure idempotence and rollback of staged artifacts, and review physical
clients. The current server still uses its original table-replacing sync:
**do not enable the candidate unit or create the approval marker.**

## Legacy sync collision — verified in repository

`scripts/ap/gway-ap-authorized-firewall-sync` invokes `flush set inet
host_security ap_allowed_macs`, then repopulates from the portal-owned
authorization files, potentially erasing independently managed grants. It
also attempts `inet arthexis_ap_portal authorized_macs`, not the current
`inet gway_ap_portal` table. Its systemd path unit watches files in the
retired checkout. This is a **cutover blocker**: disable/replace the legacy
writer only in a separately reviewed transactional activation with an
exclusive writer lock, preserved independent grants and recovery proof.
No new script should race the old path/service.
