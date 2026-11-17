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


def verify_persisted_grants(state_dir: Path, requested: set[str], *, uid: int,
                            require_consent: bool = False) -> None:
    """Match grants to a pinned, private state-directory descriptor.

    This verifies persisted records, not application identity. Dedicated UID
    and full single-writer cutover remain required.
    """
    try:
        directory_fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise BrokerRejected("unsafe state directory") from exc
    try:
        directory_stat = os.fstat(directory_fd)
        if directory_stat.st_uid != uid or directory_stat.st_mode & 0o077:
            raise BrokerRejected("unsafe state directory ownership")

        def read_private(name: str, *, marker: bool = False) -> str | None:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise BrokerRejected("unsafe pending marker" if marker else "unsafe grant file") from exc
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
                    raise BrokerRejected("unsafe pending marker" if marker else "unsafe grant file")
                with os.fdopen(fd, "rb", closefd=False) as reader:
                    payload = reader.read(1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    raise BrokerRejected("oversize private state")
                after = os.fstat(fd)
                try:
                    entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise BrokerRejected("state changed during verification") from exc
                def fingerprint(st):
                    return (st.st_dev, st.st_ino, st.st_size,
                            st.st_mtime_ns, st.st_ctime_ns)
                if (fingerprint(info) != fingerprint(after)
                        or fingerprint(after) != fingerprint(entry)):
                    raise BrokerRejected("state changed during verification")
                try:
                    return payload.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise BrokerRejected("invalid state encoding") from exc
            finally:
                os.close(fd)

        read_private("authorization.pending", marker=True)
        allowed = set()
        for name in ("authorized_macs.txt", "trusted_macs.txt"):
            data = read_private(name)
            if data is None:
                continue
            for line in data.splitlines():
                token = line.split("#", 1)[0].strip().split()
                if not token:
                    continue
                if not MAC.fullmatch(token[0]):
                    raise BrokerRejected("invalid state grant")
                allowed.add(token[0].lower())
        if not requested <= allowed:
            raise BrokerRejected("requested MAC lacks persisted authorization")
        if require_consent:
            from portal_captive_policy import consented_macs
            consent_text = read_private("consents.jsonl")
            try:
                records = [json.loads(row) for row in (consent_text or "").splitlines()]
                consented = consented_macs(records)
            except (ValueError, CoordinationError, TypeError) as exc:
                raise BrokerRejected("untrusted consent inventory") from exc
            if not requested <= consented:
                raise BrokerRejected("requested MAC lacks completed consent")
    finally:
        os.close(directory_fd)


def serve_request(conn, *, uid: int, coordinator, lock_path: Path, state_dir: Path | None = None,
                  lock_owner_uid: int = 0, require_consent: bool = False) -> None:
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
        lock_meta = os.fstat(fd)
        # The root broker must not trust a pre-planted lock owned by the
        # portal UID or an independently mutable/hard-linked inode.
        if (not stat.S_ISREG(lock_meta.st_mode)
                or lock_meta.st_uid != lock_owner_uid
                or lock_meta.st_mode & 0o077
                or lock_meta.st_nlink != 1):
            raise BrokerRejected("untrusted broker lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BrokerRejected("another authorized firewall writer is active") from exc
        if op == "check":
            coordinator.snapshot()
        else:
            if state_dir is not None:
                verify_persisted_grants(state_dir, macs, uid=uid,
                                        require_consent=require_consent)
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
                        serve_request(conn, uid=uid, coordinator=NftGrantCoordinator(require_captive=True),
                                      lock_path=lock_path, state_dir=state_dir,
                                      require_consent=True)
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
