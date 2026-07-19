"""Live run events — a tiny in-process pub/sub feeding the dashboard's SSE stream.

Each run gets its own queue per subscriber. `publish` is fire-and-forget (a
full queue just drops the event rather than blocking the build loop); the
dashboard reconnects and re-reads run state from the store if it misses
anything, so losing an occasional event under backpressure is an acceptable
trade for never stalling a build.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, AsyncIterator, Dict, List

_TERMINAL_EVENT_TYPES = {"complete", "error"}

_listeners: Dict[str, List["asyncio.Queue[str]"]] = defaultdict(list)


def publish(run_id: str, event: Dict[str, Any]) -> None:
    """Push one event to every live subscriber of `run_id`. Never blocks."""
    encoded = json.dumps(event, default=str)
    for queue in list(_listeners.get(run_id, [])):
        try:
            queue.put_nowait(encoded)
        except asyncio.QueueFull:
            continue


async def subscribe(run_id: str) -> AsyncIterator[str]:
    """Yield JSON-encoded events for `run_id` until a terminal event arrives."""
    queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=256)
    _listeners[run_id].append(queue)
    try:
        while True:
            raw = await queue.get()
            yield raw
            if json.loads(raw).get("type") in _TERMINAL_EVENT_TYPES:
                return
    finally:
        _listeners[run_id] = [q for q in _listeners[run_id] if q is not queue]
