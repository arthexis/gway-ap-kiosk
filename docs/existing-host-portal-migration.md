# Existing-host AP portal migration — issue #31

This is **not** the repository's fresh-appliance `deploy.sh` path. No change to the
active AP is permitted by merely running the read-only preflight.

## Phase 0 — establish a rollback boundary

1. Identify main commit, clean checkout, all current writers and lock holders.
2. Run `python3 scripts/portal_migration_preflight.py` on the existing host.
   Its JSON reports presence only; it intentionally does not inspect, log, or
   export identities, MACs, consent events, or stored credentials.
   A `null` service or nftables result means the command failed or could not
   run, including due to missing privileges; it does not establish absence.
   Verify unknown results using an appropriately authorized read-only check.
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

## Stage 5 — dedicated identity and root-acknowledged single-writer cutover (not enabled)

Before **candidate staging**, a separately approved, non-login `gwayap`
service account must exist. The staging playbook fails closed if it cannot
resolve that UID and does not create it. The portal candidate runs only
under `gwayap`, not the shared `arthe` login. The broker is root-only and
checks the kernel-reported UID *and* the persisted authorized/trusted
allowlists in the fixed private `/var/lib/gway-ap-kiosk/portal` directory
before adding grants. File ownership must match the dedicated UID, modes
must disallow group/other access, and grant paths cannot be symlinks.
A request for a MAC absent from durable state is refused; both nft sets
remain additive and unrelated grants remain untouched.

This is **not application authentication**. A compromised process with
`gwayap` privileges could alter its own grant source. The security
boundary therefore depends on dedicated account isolation, immutable
root-owned installed code, controlled state writers and physical AP
registration tests. Nothing provisions that isolation automatically.

Broker startup now requires a separate root-owned, private
`/etc/gway-ap-kiosk/portal-writer-cutover.json` with schema 1,
`assertions` containing these exact boolean-true fields:

- `legacy_portal_service_disabled`
- `legacy_sync_path_disabled`
- `legacy_sync_service_disabled`
- `host_firewall_writer_quiesced`
- `independent_grants_backed_up`
- `dedicated_portal_uid_verified`
- `uncertain_recovery_documented`

The file must have root ownership, restrictive permissions and no
symlink. It is a *record of operator attestations*, not independent proof
that systemd units, nft base chains or unrelated writers are safe.
Do not create this file based only on offline tests.

### UNCERTAIN recovery receipt and backup evidence (manual, not automated)

1. Keep portal and legacy sync **stopped** with the marker intact.
   Confirm the management uplink before inspecting any runtime policies.
2. Capture a private, mode-restricted backup of the exact state directory,
   both relevant nft sets and full table/chain policy, Nginx site and
   systemd units. Record filesystem permissions and a separate restoration
   test in an approved maintenance window. Never put grants, consent logs,
   emails or raw backup hashes into GitHub comments.
3. Produce `portal_uncertain_receipt.py --state-dir <state>`; attach only
   its sanitized JSON, showing UNKNOWN/UNVERIFIED gates explicitly.
4. Under a *single verified writer*, compare the durable authorization
   and consent records against both nft sets and the independent grants
   inventory. Resolve whether the ambiguous grant was applied. Never
   delete a grant merely because the compensated consent file lacks it.
5. Obtain an independently reviewed resolution/rollback receipt including
   exact revision, backed-up location (sanitized), quiesced writers,
   retained foreign grants and service checks. Only a separately approved
   recovery procedure may clear the pending marker; **this PR contains no
   automatic marker-release command**.
6. If any piece of evidence is missing, keep **UNCERTAIN** and do not
   attempt portal restart, firewall flush or host deployment.

No step above has been run against `gway-001`; the playbook still only
stages unactivated files and neither the root contract nor dedicated
service account is created by this PR.

## Stage 6 — offline backup and strict sandbox restore validation

`scripts/portal_state_backup.py` provides an offline-only library for
`create_snapshot(source, destination)`, `verify_snapshot(snapshot)`,
and `restore_to_empty_sandbox(snapshot, target)`. It copies only the
explicitly listed portal state files into a new private directory with
mode 0600, fsyncs file content and directory entries, and records a
private integrity manifest. The manifest contains sensitive file digests;
**never post the manifest or backup itself to GitHub**. A comparison
failure, an unsafe symlink, unexpected ownership/permissions or an
oversized file blocks verification. This is a state-file integrity
check, **not** an nftables/chain backup, writer-quiescence proof or full
snapshot consistency certificate. State must be quiesced by an
independently verified operator before capture.

The restore function only populates a new private directory beneath
the system temporary root. It rejects existing targets, live service
paths, unsafe parents and tampered archives. It preserves
`authorization.pending` byte-for-byte and never approves runtime
restoration. No backup, restore, firewall or service operations have
been performed on a real node.

Broker provenance now compares open-descriptor metadata against the
current directory entry after each read to detect observed replacement
or in-place changes; this narrows TOCTOU, but a writable same-UID
allowlist and external writers remain a risk. Broker lock acquisition
is nonblocking and rejects an already held lock rather than waiting
without a bound. Neither protection replaces exclusive runtime writer
ownership. The pending marker is never removed by the backup or
operator receipt tools.

## Stage 7 — offline adversarial acceptance (no on-node execution)

The two-set coordinator now intentionally exposes **no grant deletion or
rollback API**. In particular, the former caller-provided
`exclusive_lock=True` authorization was not a trustworthy proof of
external writer exclusivity and has been removed. Any ambiguous nft grant
must retain UNCERTAIN; operator recovery must preserve third-party grants.

A private state snapshot now performs a second pass over the expected
files **before publishing a valid manifest**. It rejects observed changes,
including missing/added files and changed content across the capture.
This is not a true atomic multi-file snapshot: a cooperating writer must
be quiesced, and a hostile same-UID writer can defeat repeated observation.
Do not infer a firewall backup or production recovery authorization.

For offline acceptance only, the redacted receipt supports an optional
`--offline-snapshot <private-snapshot-directory>`. When provided, integrity
verification must succeed before it emits the receipt; the only added
claims are `offline_snapshot_integrity_verified` and
`offline_snapshot_pending_preserved`. Fields
`state_backup_verified`, `firewall_verified`, and
`operator_release_allowed` remain explicitly **false**. Neither snapshot
verification nor receipt generation alters `authorization.pending`.

The next runtime safety gates remain: provision and verify isolated
`gwayap` identity, prove the obsolete and external writers are
quiescent, inspect actual nft hooks and preserve independent grants,
obtain protected offline backup plus separate firewall/state backups,
validate private file ownership on the host, and independently review
the UNCERTAIN release procedure. No runtime or physical acceptance
is claimed by this Draft.


## Protected legacy import candidate (offline implementation)

`scripts/portal_legacy_import.py` can import exactly five allowlisted portal
state files from a legacy directory into a **new, absent** private destination.
Invoke only as root, after separately observing that legacy portal/path/sync
writers have stopped and completing independent restricted backups of nftables,
service definitions, Nginx and state. The command requires the dedicated
non-root `gwayap` account and verifies legacy source ownership, destination
parent ownership, no writable source directories, regular single-link legacy
files and no symlinks. It uses exclusive creation, mode 0600 for files,
mode 0700 for the destination, fsync and a second source read to detect
observed instability. It never edits the source, emits private payloads,
installs services, or accesses nftables.

Example **only for a reviewed maintenance window**, not an activation command:

```sh
sudo python3 scripts/portal_legacy_import.py \\
  --source /home/arthe/gway-ap-kiosk/.state/ap_portal \\
  --destination /var/lib/gway-ap-kiosk/portal --target-user gwayap
```

A partial destination on failure is **not** a valid migration or permission
to retry against the same path. An operator must quarantine it, re-establish
writer exclusivity and independently validate a rollback before a fresh
attempt. The redacted import receipt explicitly marks activation, firewall
backup and writer-quiescence as unverified. Read-twice does not establish an
atomic snapshot or guarantee against hostile same-UID writers. No live node
has been migrated by the addition of this candidate tool.

**Acceptance remains gated** on ownership of the external `host_security`
writer (which recreates its table), restoration of portal grants after that
restart without deleting foreign grants, documented nft base-chain verdicts,
separate firewall and state restore exercises, reviewed manual UNCERTAIN
recovery and physical AP/uplink acceptance. A green CI result on this importer
is not enough to activate the portal or close #31.

## Stage 5 — mandatory registration + host-security restart candidate (offline)

The opt-in `--mandatory-registration` mode is **required in the staged
service template** and refuses startup unless the coordinated firewall,
non-disabled sync and restricted nft broker are configured. The server's
coordinated registration writes durable consent with UUIDv4 user/device/
session identifiers **before** requesting a grant. Existing trusted or
authorized MACs alone no longer qualify for Internet access; old consent
records without identifiers remain intact but do not authorize forwarding.

The root broker validates persisted authorization **and** completed,
identified consent for each requested MAC. Before granting, its strict
coordinator requires a live `inet gway_ap_portal forward` chain with a
`wlan0` interface check, priority -5 (ahead of NetworkManager's ordinary
priority 0 forward accept), an authorized-MAC exception, scoped IPv4/IPv6
preauth TCP 80/443 destination exceptions, and a terminal drop. A missing or
widened chain fails the broker check. Exact nft JSON normalization must be
confirmed on the target kernel before activation; these checks do not prove
the entire host ruleset.

A **noninstalled candidate** `gway-ap-host-security-reconcile.service`
rebuilds only persisted, consenting grants using the same broker and
`authorization.pending` safeguard. Its candidate drop-in adds a
`host-security-firewall.service` post-start trigger, because that service
deletes and rebuilds the entire `inet host_security` table. The hook is
intentionally asynchronous (`systemctl --no-block`) to avoid a systemd
ordering cycle: integration must explicitly verify that it runs to
completion after each host-security recreation, preserves independently
managed grants, and reports failures. It **does not** restart the host
firewall or activate either service by staging.

The currently enabled NetworkManager AP forwarding/NAT independently
passes upstream Internet. If the captive nft base chain is absent or later
deleted, normal Internet may be available without registration. Therefore
**never treat application startup checks alone as fail-closed network
isolation**. A later reviewed activation must establish and prove the
forwarding deny policy before allowing unauthenticated clients, and must
test failure/restart paths on an actual Wi-Fi client.

The root-owned cutover assertion, backups, restricted state migration,
writer exclusivity, independent grants provenance, safe rollback and
physical AP/uplink/SSH/WireGuard/OCPP acceptance remain mandatory; this
offline increment does not complete them. No production service, nftables
hook or AP configuration is changed by merging the PR.


## Stage 6 — guarded existing-host activation (opt-in only)

Normal deploy.sh and stage-portal.yml remain nonactivating. The separate
ansible/playbooks/activate-portal.yml requires an exact 40-character
portal_revision and three explicit approvals: portal_cutover_approved,
portal_maintenance_window, and portal_rollback_operator_ready. It refuses
hosts other than gway-001. Merging this PR is NOT rollout authorization.

Preconditions: clean, matching Git SHA and staged modules; root-owned
cutover contract; clean authorization.pending; private migrated state under
gwayap; static/live host-security grant parity; existing valid Nginx proxy;
root-owned private backup parent; no foreign occupant of candidate paths.
Legacy MAC authorization alone does not grant forwarded Internet.

The playbook creates an exclusive private backup of the old nftables.conf
and live nft evidence, prepares a deterministic boot policy, checks nft
syntax before writes, then appends owned includes AFTER flush ruleset for
/etc/nftables.d/host-security.nft and gway-ap-captive.nft. It applies only
the portal table in-memory without restarting NetworkManager or host_security.
Broker, reconciler and portal start in a reviewed dependency order.

At nftables reload, the new script shares the broker writer lock, compares
live host grants against the owner-managed static source plus provable
consent-backed portal additions, refuses unknown grants, then reloads
and queues reconciliation. Host-security restarts trigger reconciler too.
No native nftables stop behavior is altered.

After direct and proxied health checks, a sanitized private receipt is saved.
On failure, Ansible rescue stops candidate services, removes its approval
marker, restores the original boot file and removes only its own files.
The portal-owned in-memory nft table is removed only if no new consent
appeared; otherwise it remains to prevent an unreviewed fail-open release.
Partial compensation or crash needs operator recovery, never auto-clear
UNCERTAIN. Do not claim general transactional rollback.

Idempotent reruns require the same revision, live captive chain, broker and
Nginx health. Real restart/reboot and unauthorized/registered Wi-Fi clients,
SSH, uplink, WireGuard and OCPP must be tested separately before #31 closes.
Do not retire old artifacts on CI evidence alone.
