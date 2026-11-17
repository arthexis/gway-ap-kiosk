"""Consent-backed AP forwarding eligibility. No nft or host operations.

Legacy MAC allowlists and trusted devices do not prove mandatory user
registration. Fail closed on malformed, oversized or ambiguous consent data.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from portal_firewall_coordinator import MAC, CoordinationError

MAX_CONSENT_BYTES = 8 * 1024 * 1024


def _uuid4(value):
    if not isinstance(value, str):
        return False
    try:
        obj = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return obj.version == 4 and str(obj) == value


def valid_record(row):
    return (
        isinstance(row, dict)
        and row.get("accept_terms") is True
        and isinstance(row.get("terms_version"), str)
        and bool(row["terms_version"])
        and isinstance(row.get("accepted_at"), str)
        and bool(row["accepted_at"])
        and isinstance(row.get("mac_address"), str)
        and MAC.fullmatch(row["mac_address"]) is not None
        and all(_uuid4(row.get(x)) for x in
                ("user_id", "device_id", "session_id"))
    )


def consented_macs(records):
    """Only explicit, identifiable accepted registrations count as grants."""
    if not isinstance(records, list):
        raise CoordinationError("invalid consent inventory")
    result = set()
    for record in records:
        if not valid_record(record):
            # Historical consent is preserved for audit, but its lack of
            # attribution IDs never authorizes Internet automatically.
            if (isinstance(record, dict)
                    and record.get("accept_terms") is True
                    and isinstance(record.get("mac_address"), str)
                    and MAC.fullmatch(record["mac_address"]) is not None
                    and not any(key in record for key in
                                ("user_id", "device_id", "session_id"))):
                continue
            raise CoordinationError("invalid consent record requires review")
        result.add(record["mac_address"].lower())
    return result


def read_consented_macs(path: Path):
    """Read consent journal as a private, bounded, complete file.

    A missing journal grants nobody; malformed historical rows fail closed
    rather than silently authorizing the rest.
    """
    if path.is_symlink():
        raise CoordinationError("unsafe consent journal")
    try:
        with path.open("rb") as file:
            payload = file.read(MAX_CONSENT_BYTES + 1)
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise CoordinationError("consent journal unavailable") from exc
    if len(payload) > MAX_CONSENT_BYTES or (payload and not payload.endswith(b"\n")):
        raise CoordinationError("incomplete or oversized consent journal")
    try:
        records = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise CoordinationError("invalid consent journal") from exc
    return consented_macs(records)


def eligible_grants(authorized, trusted, consented):
    """Never promote a pre-existing allowlist into consent."""
    return (set(authorized) | set(trusted)) & set(consented)
