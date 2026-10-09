"""Web view of the same turn the CLI runs (P-1, W-1, W-2)."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from toolbox_core import ToolboxClient

from guards.sanitizer import MAX_CHARS
from support.cli import TOOLBOX_URL, session_for
from support.memory import ping as mem0_ping
from support.pipeline import run_turn, unwrap
from support.telemetry import setup_telemetry

PAGE = Path(__file__).with_name("page.html").read_text()
SESSIONS = {}
toolbox = None


@asynccontextmanager
async def lifespan(app):
    global toolbox
    load_dotenv()
    setup_telemetry()
    client = ToolboxClient(TOOLBOX_URL)
    try:
        await client.__aenter__()
    except Exception:
        client = None
    toolbox = client
    yield
    if toolbox is not None:
        await toolbox.__aexit__(None, None, None)
        toolbox = None


app = FastAPI(lifespan=lifespan)


async def _reachable(url):
    async with httpx.AsyncClient(timeout=2) as client:
        await client.get(url)


async def _database():
    if toolbox is None:
        raise RuntimeError("Toolbox is down, so the database could not be checked.")
    tool = await toolbox.load_tool("verify-login")
    await tool(email="health@local", password="health")


async def _check(name, call):
    try:
        await call
        return name, "ok"
    except Exception as exc:
        return name, str(exc)


@app.get("/health")
async def health():
    checks = await asyncio.gather(
        _check("db", _database()),
        _check("toolbox", _reachable(TOOLBOX_URL)),
        _check("judge", _reachable("http://127.0.0.1:10002/.well-known/agent.json")),
        _check("masker", _reachable("http://127.0.0.1:10003/.well-known/agent.json")),
        _check("mem0", asyncio.wait_for(asyncio.to_thread(mem0_ping), timeout=5)),
        _check("phoenix", _reachable("http://127.0.0.1:6006/healthz")),
    )
    body = {"status": "ok", "model": os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")}
    body.update(dict(checks))
    failed = any(value != "ok" for key, value in checks)
    if failed:
        body["status"] = "error"
    return JSONResponse(body, status_code=503 if failed else 200)


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.post("/api/login")
async def login(body: dict):
    if toolbox is None:
        return JSONResponse({"error": "Toolbox is down."}, status_code=502)
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    tool = await toolbox.load_tool("verify-login")
    rows = unwrap(await tool(email=email, password=password))
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        return JSONResponse({"error": "Invalid email or password."}, status_code=401)
    row = rows[0]
    runner, session_id, model = await session_for(toolbox, email)
    SESSIONS[email] = {"runner": runner, "session_id": session_id, "model": model}
    return {
        "user_id": row.get("email", email),
        "full_name": row.get("full_name", ""),
        "is_premium": bool(row.get("is_premium_customer")),
    }


@app.post("/api/logout")
async def logout(body: dict):
    SESSIONS.pop(str(body.get("user_id") or ""), None)
    return {"ok": True}


@app.post("/api/chat")
async def chat(body: dict):
    message = body.get("message")
    user_id = str(body.get("user_id") or "")
    if not isinstance(message, str) or not message.strip():
        return JSONResponse({"error": "message is empty"}, status_code=400)
    if len(message) > MAX_CHARS:
        return JSONResponse({"error": "message too long"}, status_code=413)
    session = SESSIONS.get(user_id)
    if session is None:
        return JSONResponse({"error": "Not logged in."}, status_code=401)

    async def events():
        async for event in run_turn(
            session["runner"],
            user_id=user_id,
            session_id=session["session_id"],
            message=message,
            model=session["model"],
        ):
            yield (json.dumps(event) + "\n").encode()

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
