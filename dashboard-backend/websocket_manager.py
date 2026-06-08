"""
websocket_manager.py
====================
Manages active WebSocket connections per business.
Used to push real-time message updates to the dashboard.
"""

import json
import logging
from collections import defaultdict
from fastapi import WebSocket

log = logging.getLogger("websocket")


class ConnectionManager:
    def __init__(self):
        # business_id → list of active WebSocket connections
        self._connections: dict[str, list[WebSocket]] = defaultdict(list)

    async def connect(self, business_id: str, ws: WebSocket):
        await ws.accept()
        self._connections[business_id].append(ws)
        log.info(f"WS connected: business={business_id} total={len(self._connections[business_id])}")

    def disconnect(self, business_id: str, ws: WebSocket):
        self._connections[business_id].remove(ws)
        log.info(f"WS disconnected: business={business_id}")

    async def broadcast(self, business_id: str, payload: dict):
        """Send JSON payload to all dashboard tabs open for this business."""
        dead = []
        for ws in self._connections.get(business_id, []):
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections[business_id].remove(ws)


manager = ConnectionManager()