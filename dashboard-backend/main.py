"""
main.py
=======
FastAPI app entry point.
- Mounts all routers
- Starts RabbitMQ consumer background task on startup
- WebSocket endpoint for real-time dashboard updates
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Query
from fastapi.middleware.cors import CORSMiddleware

from database import init_db, get_db
from servicebus_consumer import start_consumer
from websocket_manager import manager
from auth import decode_token

from routes.auth      import router as auth_router
from routes.chats     import router as chats_router
from routes.templates import router as templates_router
from routes.bulk      import router as bulk_router
from routes.settings  import router as settings_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    log.info("Initialising database...")
    await init_db()
    log.info("Starting RabbitMQ consumer...")
    asyncio.create_task(start_consumer())
    yield
    # shutdown — nothing to clean up


app = FastAPI(
    title       = "WhatsApp CRM Dashboard API",
    version     = "1.0.0",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = False,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Routers ───────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(chats_router)
app.include_router(templates_router)
app.include_router(bulk_router)
app.include_router(settings_router)


# ── WebSocket — real-time message push ────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(
    ws: WebSocket,
    token: str = Query(...),
):
    """
    Connect with: ws://your-api/ws?token=<JWT>
    Receives JSON events when new messages arrive for this business.
    """
    try:
        payload     = decode_token(token)
        business_id = payload["business_id"]
    except Exception:
        await ws.close(code=4001)
        return

    await manager.connect(business_id, ws)
    try:
        while True:
            # keep connection alive — client can send pings
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(business_id, ws)


# ── Health check ──────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}