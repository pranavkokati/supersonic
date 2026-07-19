"""Completion notifications — a webhook, done honestly instead of five channels done badly.

Point `notify_webhook_url` at Zapier/Make/n8n/a Slack incoming webhook/anything
that accepts a POST, and the same completion payload fans out to email, Slack,
SMS, whatever — one well-defined integration point instead of bespoke,
half-working email/SMS code baked into the core loop.
"""

from __future__ import annotations

from typing import Any, Dict

from supersonic.config import UserSecrets
from supersonic.webhooks import fire_webhook


def notify_completion(secrets: UserSecrets, payload: Dict[str, Any]) -> bool:
    if not secrets.notify_webhook_url.strip():
        return False
    return fire_webhook(secrets.notify_webhook_url, "build.complete", payload, secret=secrets.webhook_secret)
