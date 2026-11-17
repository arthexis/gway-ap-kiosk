"""Pseudonymous registration identifiers for AP portal activity.

IDs describe a self-declared email and the most recently consenting device
holder. They do not prove human identity or identify end-to-end HTTPS traffic.
Legacy consent records without IDs remain readable, but are not silently
assigned guessed identities.
"""
from __future__ import annotations

import uuid
from typing import Any


def _valid_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    if parsed.version != 4 or str(parsed) != value:
        return None
    return value


def registration_ids(consents: list[dict[str, Any]], *, email: str,
                     mac_address: str) -> dict[str, str]:
    """Reuse stable user/device IDs from completed records; create a session."""
    user_id = None
    device_id = None
    for row in reversed(consents):
        if not isinstance(row, dict) or row.get("accept_terms") is not True:
            continue
        if user_id is None and row.get("email") == email:
            user_id = _valid_id(row.get("user_id"))
        if device_id is None and row.get("mac_address") == mac_address:
            device_id = _valid_id(row.get("device_id"))
        if user_id and device_id:
            break
    return {"user_id": user_id or str(uuid.uuid4()),
            "device_id": device_id or str(uuid.uuid4()),
            "session_id": str(uuid.uuid4())}


def latest_device_attribution(consents: list[dict[str, Any]],
                              mac_address: str | None) -> dict[str, str]:
    """Last completed consent on that MAC wins; do not infer from IP alone.

    Shared and MAC-randomized devices cannot prove which actual human is
    generating traffic. Only return identities explicitly present in the
    stored consent; historical untagged records stay unattributed.
    """
    if not mac_address:
        return {}
    for row in reversed(consents):
        if (not isinstance(row, dict) or row.get("mac_address") != mac_address
                or row.get("accept_terms") is not True):
            continue
        ids = {key: _valid_id(row.get(key)) for key in
               ("user_id", "device_id", "session_id")}
        if all(ids.values()):
            return {**ids, "attribution_basis": "last_device_consent"}
        return {}
    return {}
