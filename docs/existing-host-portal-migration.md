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

## Stage 2 — opt-in integrated registration, still not activated

The server now accepts `--coordinated-firewall`; this opt-in changes startup
reconciliation and the consent-backed registration path to use additive
two-set grants under `authorization.lock`. Consent and activity must be
persisted before authorization-file changes and nft mutation. On a rejected
nft batch, those writes are compensated; an interrupted process needs
startup reconciliation to restore grants from durable consent/file state.
Existing independent grants are never removed by this implementation.

The legacy sync executable now refuses operation unless
`GWAY_AP_CUTOVER_APPROVED=1`, and when enabled delegates to an additive
reconciler using the same lock. **This environment variable alone is not a
validated cutover**. Previously deployed binaries, path units and unrelated
firewall writers do not necessarily honor this lock. The old service and
path watcher must be inventoried and disabled during a reviewed activation.

**Privilege gate:** the candidate unit runs as `arthe`; this alone does not
grant the ability to modify nftables. Do **not** grant unrestricted
CAP_NET_ADMIN or run the portal as root merely to make it work. A narrowly
authorized firewall writer (or broker) must be specified, installed and
tested, including authenticated requests, identity separation, and the same
serialization lock, before enabling the candidate.

**Residual crash limitations:** the records are currently append-only
without explicit fsync/transaction journal. Startup reconciliation must not
be treated as a full crash-consistency proof. Privilege separation,
filesystem crash recovery, dedicated-host scope verification, concurrent
unmanaged firewall writers, and physical client acceptance remain blocked.

## Stage 3 — restricted broker and durable uncertainty (offline only)

Candidate code includes `portal_nft_broker.py`, `portal_nft_client.py`
and `portal_grant_intent.py`. The browser-facing portal remains `User=arthe`
with `NoNewPrivileges=yes`, and its candidate unit requests additive grants
only through `/run/gway-ap-nft/broker.sock`. A separate *uninstalled*
root-owned broker candidate has only `CAP_NET_ADMIN` in its capability
bounding set; it authenticates the caller using kernel `SO_PEERCRED` with
an exact non-root UID, rejects commands other than bounded `add` and
read-only prerequisite `check`, and never accepts nft commands or set names
from the client. It serializes its nft batches on a root-owned lock.

The portal and replacement legacy sync helper also share
`authorization.lock` for state changes; the helper routes through the
broker and refuses use without the cutover gate. Both file locking and
broker locking must be respected by **all** deployed writers before this
can protect the host. The broker's socket is accessible only to the
configured non-root UID; the root-owned parent directory remains
non-writable by that UID.

A coordinated registration now creates and fsyncs a private
`authorization.pending` marker before changing persistent files. It fsyncs
consent, activity and allowlist before requesting the nft grant, and
fsyncs compensation before clearing uncertainty. If interrupted, a later
portal startup refuses service until an operator inspects state and both
sets. **Do not simply remove an uncertain marker**, because the nft batch
may have succeeded while the process was interrupted. The recovery workflow
requires verified state/backups and evidence about the actual firewall
grants. This is fail-closed crash detection, not fully automated rollback.

Open activation blockers: ensure old `.path` and service writers are
actually disabled, review privilege and socket directory ownership,
validate all nft base-chain policies and collision with external writers,
bound large grant inventories (broker currently allows at most 64 per
request), establish a trusted manual UNCERTAIN recovery receipt, migrate
existing state with verified ownership, and physically test AP clients.
**Do not stage on gway-001 or activate either candidate under issue #31
without a separate approved rollout.**

## Stage 4 — broker audit, bounded batches and UNCERTAIN receipt

The broker's AF_UNIX `SO_PEERCRED` checks the kernel-reported peer PID
and exact non-root UID; it does **not** authenticate the application binary,
its systemd unit, or a consent event. Any process running under that same
UID and able to connect to the socket can request grants. This remains a
**security blocker** until the service identity/socket authorization
boundary and the ownership of MAC provenance are reviewed. The root broker
must not be installed on a shared UID on the assumption that `SO_PEERCRED`
alone provides per-application authorization.

Protocol framing now requires a single complete JSON request and a
client half-close; trailing bytes, second frames, malformed credentials,
unknown operations and unsupported set names are rejected. One additive
nft transaction accepts at most **1024 distinct MACs**, with a maximum
32768-byte request, to support migration inventories larger than 64 without
partial batch commits. Inventories above those bounds are **not** split
implicitly: fail closed and require a reviewed alternative.

The shared state lock serializes cooperative portal/helper writes; the
root broker lock serializes broker requests. Neither lock protects against
unmodified host firewall services or other external nft writers. Preserve
all separately managed grants; never flush host_security.

After an ambiguous broker request (timeout, rejected reply, or lost
acknowledgment), the portal now **retains** `authorization.pending` even
when its file compensation succeeds. The server and reconciler refuse
automatic restart with that marker. An operator can obtain a sanitized,
read-only receipt using:

```sh
python3 scripts/portal_uncertain_receipt.py --state-dir /var/lib/gway-ap-kiosk/portal
```

The receipt includes only file presence/type/privacy status and explicit
unverified gates; it does not reveal MACs, email addresses, consent contents,
counts or hashes. It **cannot** clear the marker or certify the firewall.
A separately approved recovery must establish effective grants and base
chains, backup provenance, writer quiescence and verified consistency.
Automatic marker removal or revocation of grants in an ambiguous state
would risk removing an independent or already-committed authorization.

The **candidate portal unit** now sets `UMask=0077` to create newly written
consent/activity/authorization data as private files. This does not repair
permissions of imported records: validate existing ownership and mode during
migration and reject state with unsafe provenance. These service templates
remain uninstalled and unactivated.
