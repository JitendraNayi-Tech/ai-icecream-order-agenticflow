import asyncio
import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app import config, mcp_bridge
from app.orchestrator import Orchestrator
from app.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

log = logging.getLogger(__name__)
if config.LLM_BACKEND == "claude_code":
    log.info("LLM backend: Claude Code CLI (model %s, router %s); tools served at %s/mcp",
             config.CLAUDE_CODE_MODEL, config.CLAUDE_CODE_ROUTER_MODEL, config.APP_URL)
elif not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
    log.warning("LLM_BACKEND=api but ANTHROPIC_API_KEY is not set; agents will not reply.")
else:
    log.info("LLM backend: Claude API (model %s)", config.MODEL)

app = FastAPI(title="Ice Cream Order Agentic Flow")
store = Store()
orchestrator = Orchestrator(store)
app.include_router(mcp_bridge.build_router(orchestrator))


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/api/customers")
async def customers() -> list[dict]:
    return [c.model_dump() for c in store.customers.values()]


@app.get("/api/usage/{customer_id}")
async def usage_panel(customer_id: str) -> dict:
    """Same data as the cost panel: current order value vs AI cost, plus earlier orders."""
    return orchestrator.usage_panel(orchestrator.session(customer_id))


@app.websocket("/ws/{customer_id}")
async def chat(ws: WebSocket, customer_id: str) -> None:
    await ws.accept()
    try:
        session = orchestrator.session(customer_id)
    except KeyError:
        await ws.close(code=4404, reason="unknown customer")
        return

    # Replay history so a reconnect (page refresh) shows the conversation so far; frames
    # queued while disconnected are already part of that history.
    while not session.outbox.empty():
        session.outbox.get_nowait()
    for m in session.history:
        if not m["content"].startswith("[The customer just opened"):
            await ws.send_json({"type": "message", "agent": "You" if m["role"] == "user" else "Shop",
                                "text": m["content"]})

    await ws.send_json({"type": "usage", **orchestrator.usage_panel(session)})

    async def writer() -> None:
        while True:
            await ws.send_json(await session.outbox.get())

    pending: set[asyncio.Task] = set()

    async def reader() -> None:
        while True:
            data = await ws.receive_json()
            text = str(data.get("text", "")).strip()
            if text:
                # Handle in the background so live status updates keep flowing meanwhile.
                task = asyncio.create_task(orchestrator.handle(session, text))
                pending.add(task)
                task.add_done_callback(pending.discard)

    tasks = [asyncio.create_task(writer()), asyncio.create_task(reader()),
             asyncio.create_task(orchestrator.greet(session))]
    try:
        await asyncio.wait(tasks[:2], return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks[:2]:
            t.cancel()
