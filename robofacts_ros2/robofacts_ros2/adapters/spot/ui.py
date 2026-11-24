"""The Spot adapter's part of the web interface."""
from __future__ import annotations

from typing import Dict, List, Optional


def lease_owners(output: str) -> Optional[List[Dict[str, str]]]:
    """Who holds each lease resource, from an action that echoes a Spot ``LeaseArray``.

    The ``lease`` action prints ``ros2 topic echo .../status/leases --once``. Read raw, the
    owner of the body lease is about the 26th line, and on 2026-08-10 the tablet held it
    through four trials without anybody seeing it. So the page states it in one line.
    Returns None when the output holds no lease message, so other actions are unchanged.
    """
    import yaml

    lines = output.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("resources:")), None)
    if start is None:
        return None
    body = []
    for line in lines[start:]:
        if line.startswith("---") or line.startswith("==="):
            break
        body.append(line)
    try:
        message = yaml.safe_load("\n".join(body)) or {}
    except yaml.YAMLError:
        return None
    owners = []
    for resource in message.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        owner = resource.get("lease_owner") or {}
        owners.append({"resource": str(resource.get("resource", "?")),
                       "client_name": str(owner.get("client_name") or ""),
                       "user_name": str(owner.get("user_name") or "")})
    return owners or None
