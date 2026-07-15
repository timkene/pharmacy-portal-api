import asyncio
import json
from typing import Dict, Set


class SSEManager:
    def __init__(self):
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}

    def subscribe(self, order_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(order_id, set()).add(q)
        return q

    def unsubscribe(self, order_id: str, q: asyncio.Queue) -> None:
        if order_id in self._subscribers:
            self._subscribers[order_id].discard(q)

    async def broadcast(self, order_id: str, event: str, data: dict) -> None:
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        for q in list(self._subscribers.get(order_id, [])):
            await q.put(msg)


sse_manager = SSEManager()
