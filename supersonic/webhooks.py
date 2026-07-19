"""Outbound webhooks on build events."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)


def sign_payload(secret: str, body: bytes) -> str:
    if not secret:
        return ""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def fire_webhook(url: str, event: str, payload: Dict[str, Any], *, secret: str = "") -> bool:
    if not url.strip():
        return False
    body = {"event": event, **payload}
    raw = json.dumps(body, default=str).encode()
    headers = {"Content-Type": "application/json"}
    sig = sign_payload(secret, raw)
    if sig:
        headers["X-Sonic-Signature"] = sig
    try:
        r = httpx.post(url.strip(), content=raw, headers=headers, timeout=15.0)
        ok = r.status_code < 400
        if not ok:
            logger.warning("webhook %s returned %s", url, r.status_code)
        return ok
    except Exception as e:
        logger.warning("webhook failed: %s", e)
        return False
