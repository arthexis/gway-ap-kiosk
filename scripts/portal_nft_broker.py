"""Restricted offline-candidate nft broker. Not installed or activated.

Only accepts one framed JSON request, only additive grants to the two fixed
AP sets, only from a configured unprivileged UID over AF_UNIX SO_PEERCRED.
No arbitrary nft commands, file paths, deletions or table creation.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import socket
import stat
import struct
from pathlib import Path

from portal_firewall_coordinator import MAC, NftGrantCoordinator, CoordinationError

MAX_REQUEST = 32768
MAX_MACS = 1024


class BrokerRejected(RuntimeError):
    pass


def authorize_peer(conn, *, allowed_uid: int) -> None:
    if not hasattr(socket, "SO_PEERCRED"):
        raise BrokerRejected("peer credentials unsupported")
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        if len(creds) != struct.calcsize("3i"):
            raise BrokerRejected("invalid peer credentials")
        pid, uid, _gid = struct.unpack("3i", creds)
    except (OSError, struct.error) as exc:
        raise BrokerRejected("peer credentials unavailable") from exc
    if pid <= 0:
        raise BrokerRejected("invalid peer process")
    if uid != allowed_uid or uid == 0:
        raise BrokerRejected("peer not permitted")


def decode_request(data: bytes) -> set[str]:
    if not data or len(data) > MAX_REQUEST or data.count(b"\n") != 1 or not data.endswith(b"\n"):
        raise BrokerRejected("invalid request framing")
    try:
        payload = json.loads(data[:-1].decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BrokerRejected("invalid request") from exc
    if not isinstance(payload, dict) or set(payload) != {"op", "macs"} or not isinstance(payload["op"], str) or payload["op"] not in {"add", "check"}:
        raise BrokerRejected("unsupported operation")
    values = payload["macs"]
    if not isinstance(values, list) or len(values) > MAX_MACS or (payload["op"] == "add" and not values) or (payload["op"] == "check" and values):
        raise BrokerRejected("invalid grant batch")
    if any(not isinstance(m, str) or not MAC.fullmatch(m) for m in values):
        raise BrokerRejected("invalid grant")
    normalized = {m.lower() for m in values}
    if len(normalized) != len(values):
        raise BrokerRejected("duplicate grant")
    return normalized


def verify_persisted_grants(state_dir: Path, requested: set[str], *, uid: int) -> None:
    """Authorize only MACs already durable in the dedicated portal state.

    This is a provenance check, not proof that the caller is the portal app.
    Caller must not supply the directory path. Dedicated UID provisioning,
    private files and exclusive writer cutover remain mandatory.
    """
    from portal_authorization_reconcile import load_macs
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise BrokerRejected("state directory unavailable")
    directory = state_dir.stat()
    if directory.st_uid != uid or directory.st_mode & 0o077:
        raise BrokerRejected("unsafe state directory ownership")
    from portal_grant_intent import GrantIntent
    # An in-progress portal grant carries a durable pending marker.
    # Do NOT require its absence; doing so would deadlock legitimate grants.
    pending = GrantIntent(state_dir).path
    if pending.is_symlink():
        raise BrokerRejected("unsafe pending marker")
    if pending.exists():
        st = pending.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
            raise BrokerRejected("unsafe pending marker")
    allowed = set()
    for name in ("authorized_macs.txt", "trusted_macs.txt"):
        path = state_dir / name
        if path.is_symlink():
            raise BrokerRejected("unsafe grant file")
        if path.exists():
            st = path.stat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
                raise BrokerRejected("unsafe grant file")
        allowed.update(load_macs(path))
    if not requested <= allowed:
        raise BrokerRejected("requested MAC lacks persisted authorization")


def serve_request(conn, *, uid: int, coordinator, lock_path: Path, state_dir: Path | None = None) -> None:
    authorize_peer(conn, allowed_uid=uid)
    message = bytearray()
    # Read until clean EOF, not just the first newline: reject second frames
    # and trailing bytes instead of silently processing the first command.
    while len(message) <= MAX_REQUEST:
        fragment = conn.recv(min(1024, MAX_REQUEST + 1 - len(message)))
        if not fragment:
            break
        message.extend(fragment)
    if len(message) > MAX_REQUEST:
        raise BrokerRejected("request exceeds maximum")
    raw = bytes(message)
    macs = decode_request(raw)
    op = json.loads(raw)["op"]
    # Broker-controlled lock distinct from the portal's state-file lock:
    # does not deadlock with the portal holding its own lock over the RPC.
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BrokerRejected("invalid broker lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        if op == "check":
            coordinator.snapshot()
        else:
            if state_dir is not None:
                verify_persisted_grants(state_dir, macs, uid=uid)
            coordinator.add(macs)
    finally:
        os.close(fd)
    conn.sendall(b'{"ok":true}\n')


def run_server(*, socket_path: Path, lock_path: Path, uid: int, state_dir: Path | None = None, cutover_contract: Path | None = None) -> None:
    if os.geteuid() != 0 or uid <= 0:
        raise BrokerRejected("dedicated root broker and non-root peer required")
    if state_dir is None:
        raise BrokerRejected("pinned state directory required")
    if cutover_contract is None:
        raise BrokerRejected("root-owned cutover contract required")
    from portal_cutover_contract import check_contract
    check_contract(cutover_contract)
    if socket_path.exists() or socket_path.is_symlink():
        raise BrokerRejected("socket exists; refusing replacement")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(socket_path))
        os.chown(socket_path, uid, -1)
        os.chmod(socket_path, 0o600)  # restricted to configured peer UID
        listener.listen(4)
        try:
            while True:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(3)
                    try:
                        serve_request(conn, uid=uid, coordinator=NftGrantCoordinator(),
                                      lock_path=lock_path, state_dir=state_dir)
                    except (BrokerRejected, CoordinationError, OSError, ValueError):
                        # No MACs or nft stderr in response.
                        try:
                            conn.sendall(b'{"ok":false}\n')
                        except OSError:
                            pass
        finally:
            socket_path.unlink(missing_ok=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket", type=Path, required=True)
    p.add_argument("--lock", type=Path, required=True)
    p.add_argument("--uid", type=int, required=True)
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--cutover-contract", type=Path, required=True)
    args = p.parse_args()
    run_server(socket_path=args.socket, lock_path=args.lock, uid=args.uid, state_dir=args.state_dir, cutover_contract=args.cutover_contract)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
