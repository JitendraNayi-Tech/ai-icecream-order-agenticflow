"""Minimal MCP (streamable HTTP, JSON responses) endpoint exposing each agent's tools.

Used by the Claude Code backend: `claude -p` connects to /mcp/{customer_id}/{agent} and
calls tools that operate on that customer's live session, exactly like the API backend.
Only the four methods Claude Code needs are implemented. Localhost/POC use only.
"""

import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.agents import AGENTS
from app.agents.base import AgentContext

log = logging.getLogger(__name__)

DEFAULT_PROTOCOL = "2025-06-18"


def build_router(orchestrator) -> APIRouter:
    router = APIRouter()

    def tools_for(customer_id: str, agent: str) -> dict:
        ctx = AgentContext(store=orchestrator.store, session=orchestrator.session(customer_id),
                           services=orchestrator)
        return {t.name: t for t in AGENTS[agent].make_tools(ctx)}

    @router.get("/mcp/{customer_id}/{agent}")
    async def no_stream() -> Response:
        return Response(status_code=405)  # no server-initiated SSE stream

    @router.post("/mcp/{customer_id}/{agent}")
    async def rpc(customer_id: str, agent: str, request: Request) -> Response:
        if agent not in AGENTS or customer_id not in orchestrator.store.customers:
            return JSONResponse({"error": "unknown agent or customer"}, status_code=404)
        msg = await request.json()
        method, params, msg_id = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if msg_id is None:  # notification (e.g. notifications/initialized)
            return Response(status_code=202)

        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "scoops-shop", "version": "0.1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": t.name, "description": t.description,
                                 "inputSchema": t.input_schema}
                                for t in tools_for(customer_id, agent).values()]}
        elif method == "tools/call":
            tool = tools_for(customer_id, agent).get(params.get("name"))
            if tool is None:
                return _error(msg_id, -32602, f"Unknown tool {params.get('name')}")
            try:
                output = await tool.call(params.get("arguments") or {})
                is_error = False
            except Exception as e:  # bad arguments etc.; let Claude see and fix it
                log.warning("tool %s failed: %s", tool.name, e)
                output, is_error = f"Tool error: {e}", True
            log.info("[%s/%s] %s(%s)", customer_id, agent, tool.name,
                     json.dumps(params.get("arguments") or {}))
            result = {"content": [{"type": "text", "text": str(output)}], "isError": is_error}
        else:
            return _error(msg_id, -32601, f"Method not found: {method}")
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})

    return router


def _error(msg_id, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})
