from __future__ import annotations

import hashlib
import json
from typing import Any


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def delivery_key(*, blog_id: str, destination_epoch: int, pair_id: str, unit_key: str, action: str, operation_id: str) -> str:
    return _digest({"blog_id": blog_id, "destination_epoch": destination_epoch, "pair_id": pair_id, "unit_key": unit_key, "action": action, "operation_id": operation_id})


def approval_fingerprint(payload: dict[str, Any]) -> str:
    """Bind a prepared mutation to its exact content, policy, adapter, schedule, and destination."""
    return _digest(payload)
