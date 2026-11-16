"""Small fail-closed RPC client for the restricted AP nft broker."""
from __future__ import annotations
import json
import socket
from pathlib import Path
from portal_firewall_coordinator import MAC, CoordinationError

class BrokerClient:
    def __init__(self, socket_path: str | Path, *, connector=None):
        self.socket_path = str(socket_path)
        self.connector = connector or socket.socket

    def _request(self, operation: str, values: set[str]):
        if operation not in ("add", "check"):
            raise CoordinationError("unsupported broker command")
        normalized = {str(x).lower() for x in values}
        if len(normalized) > 64 or any(not MAC.fullmatch(x) for x in normalized):
            raise CoordinationError("invalid AP grant")
        if operation == "check" and normalized:
            raise CoordinationError("invalid prerequisite query")
        if operation == "add" and not normalized:
            raise CoordinationError("empty AP grant")
        message = json.dumps({"op": operation, "macs": sorted(normalized)}).encode() + b"\n"
        try:
            with self.connector(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect(self.socket_path)
                sock.sendall(message)
                response = bytearray()
                while len(response) < 128:
                    chunk = sock.recv(128 - len(response))
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b"\n" in chunk:
                        break
        except (OSError, TimeoutError) as exc:
            raise CoordinationError("AP nft broker unavailable") from exc
        if bytes(response) != b'{"ok":true}\n':
            raise CoordinationError("AP nft broker rejected request")

    def add(self, macs: set[str]):
        self._request("add", set(macs))

    def snapshot(self):
        self._request("check", set())
        return {}  # Only prerequisite proof, never exposes host MACs.
