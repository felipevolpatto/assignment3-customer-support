"""CLI for the support agent. It only renders events the pipeline yields (P-1, C-2, C-3)."""

import argparse
import asyncio
import getpass
import json
import os
import sys

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from toolbox_core import ToolboxClient

from support.pipeline import run_turn, unwrap
from support.telemetry import phoenix_is_up, setup_telemetry

APP_NAME = "customer-support"
TOOLBOX_URL = "http://127.0.0.1:5000"
INSTRUCTION = """
You are the support agent for this online shop. You help the logged-in customer
with their orders, deliveries, returns, and account.

Use get-order-status for one order, find-customer-orders for their history, and
action-log to record a change they asked for. action-log does not change the order.

Order facts come only from a tool result in this turn. Trust tools over memories
for order facts. If a tool returns nothing, say the order is not on their account.
Do not invent products, prices, or statuses.
""".strip()


def render(event):
    """One readable line, in the shape sketched in BUILD_LOG stage 4."""
    kind = event["type"]
    if kind == "trace":
        return f"trace   {event['trace_id']}  {event['url']}"
    if kind == "stage":
        return f"stage   {event['key']}"
    if kind == "step":
        if event["key"] == "recall":
            memories = event.get("memories") or []
            if not memories:
                return f"recall   {event.get('detail', '')}"
            lines = []
            for item in memories:
                flag = "inserted" if item["inserted"] else f"skipped ({item.get('reason')})"
                lines.append(f"memory  {flag}  score {item['score']}  {item['memory']}")
            return "\n".join(lines)
        label = "blocked" if event["status"] == "blocked" else "passed"
        return f"{label:<7} {event['key']:<10} {event.get('detail', '')}  {event.get('ms')} ms"
    if kind == "tool_call":
        args = "  ".join(f"{key}={value}" for key, value in event["args"].items())
        return f"tool    {event['name']}  {args}".rstrip()
    if kind == "tool_result":
        return f"tool    {event['name']}  ok={str(event['ok']).lower()}  {event['ms']} ms"
    if kind == "llm":
        return f"llm     {event['decision']}  {event['ms']} ms"
    if kind == "final":
        return event["response"]
    if kind == "error":
        return f"error   {event['step']}  {event['error']}"
    return json.dumps(event)


def demo_password(email):
    """Seed passwords are the first name, the word before the first dot."""
    return email.split("@", 1)[0].split(".", 1)[0].lower()


async def login(toolbox, email, password):
    tool = await toolbox.load_tool("verify-login")
    rows = unwrap(await tool(email=email, password=password))
    if not rows:
        raise SystemExit("Invalid email or password.")


async def session_for(toolbox, email):
    tools = await toolbox.load_toolset(
        "support-agent",
        bound_params={"customer_email": email, "user_email": email},
    )
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    agent = LlmAgent(
        name="support_agent",
        # SPEC names gemini-2.5-flash, which Google no longer serves to new keys.
        model=model,
        instruction=INSTRUCTION,
        tools=tools,
    )
    service = InMemorySessionService()
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=service)
    session = await service.create_session(app_name=APP_NAME, user_id=email)
    return runner, session.id, model


async def emit(runner, email, session_id, message, model, events_only):
    async for event in run_turn(
        runner, user_id=email, session_id=session_id, message=message, model=model
    ):
        if events_only:
            print(json.dumps(event), flush=True)
        else:
            line = render(event)
            if line:
                print(line, flush=True)
        if event["type"] in {"final", "error"}:
            return event["type"]
    return "error"


async def main():
    load_dotenv()
    if not os.environ.get("GOOGLE_API_KEY"):
        raise SystemExit("GOOGLE_API_KEY is missing. Add it to .env and try again.")
    if not phoenix_is_up():
        raise SystemExit("Phoenix is down. Start it with ./run.sh up.")
    # The tracer has to exist before the agent is built, or Gemini calls are not traced.
    setup_telemetry()

    parser = argparse.ArgumentParser()
    parser.add_argument("--user", help="logged-in email; skips the prompts")
    parser.add_argument("--events", action="store_true", help="print raw NDJSON only")
    args = parser.parse_args()
    piped = not sys.stdin.isatty()

    if args.user:
        email = args.user.strip()
        password = demo_password(email)
    else:
        email = input("email: ").strip()
        password = getpass.getpass("password: ")

    async with ToolboxClient(TOOLBOX_URL) as toolbox:
        await login(toolbox, email, password)
        runner, session_id, model = await session_for(toolbox, email)
        if piped:
            message = sys.stdin.read().strip()
            if not message:
                raise SystemExit("message is empty")
            kind = await emit(runner, email, session_id, message, model, args.events)
            raise SystemExit(0 if kind == "final" else 1)

        if not args.events:
            print("Logged in. Type quit to stop.")
        while True:
            message = input("" if args.events else "You: ").strip()
            if message.lower() in {"quit", "exit", "q"}:
                break
            if message:
                await emit(runner, email, session_id, message, model, args.events)


if __name__ == "__main__":
    asyncio.run(main())
