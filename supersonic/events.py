"""Run event bus for live SSE streaming."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, AsyncIterator, Dict, List

_subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)


def publish(run_id: str, event: Dict[str, Any]) -> None:
    payload = json.dumps(event, default=str)
    for q in list(_subscribers.get(run_id, [])):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


async def subscribe(run_id: str) -> AsyncIterator[str]:
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
    _subscribers[run_id].append(q)
    try:
        while True:
            msg = await q.get()
            yield msg
            data = json.loads(msg)
            if data.get("type") in ("complete", "error"):
                break
    finally:
        _subscribers[run_id] = [x for x in _subscribers[run_id] if x is not q]
