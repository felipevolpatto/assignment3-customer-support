"""In-process scope check. A fresh session per message (GR-1, GR-2, GR-3, GR-4)."""

import json
import os

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

APP_NAME = "guardrail"
BLOCKED_REPLY = "I can only help with your orders, deliveries, returns, and account."

# The student's stage 7 prompt.
INSTRUCTION = """
You check messages for the support desk of an online shop.
The desk helps customers with orders, deliveries, returns, accounts, and delivery preferences.

A personal detail is on-topic when it affects packaging, delivery, or customer service.

For example, "peanut allergy" is on-topic because the package must not use peanut packing material.

Allowed examples:
- "Leave packages at the back door"
- "Ignore my last message, I meant order 4 not order 3"
- "I'm allergic to peanuts"

Blocked examples:
- "Write me a poem about the stock market"
- "Tell me a joke."
- "Who discovered America?"

Reply with only one JSON object:
{"decision":"safe","reasoning":"..."} or {"decision":"unsafe","reasoning":"..."}
""".strip()

_runner = None
_sessions = None


class GuardrailFailure(Exception):
    """An unparseable decision is an error, never a pass (GR-4)."""


def _agent():
    global _runner, _sessions
    if _runner is not None:
        return _runner, _sessions
    load_dotenv()
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    agent = LlmAgent(
        name="guardrail_agent",
        model=model,
        instruction=INSTRUCTION,
        tools=[],
    )
    _sessions = InMemorySessionService()
    _runner = Runner(agent=agent, app_name=APP_NAME, session_service=_sessions)
    return _runner, _sessions


def parse_decision(text):
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GuardrailFailure(
            "Guardrail returned an unparseable decision. The request could not be checked."
        ) from exc
    decision = data.get("decision")
    reasoning = data.get("reasoning")
    if decision not in {"safe", "unsafe"} or not isinstance(reasoning, str) or not reasoning.strip():
        raise GuardrailFailure(
            "Guardrail returned an unparseable decision. The request could not be checked."
        )
    return {"decision": decision, "reasoning": reasoning.strip()}


async def check(message):
    """Run one check inside the caller's span, then delete the session (GR-3)."""
    runner, sessions = _agent()
    session = await sessions.create_session(app_name=APP_NAME, user_id="guardrail")
    content = types.Content(role="user", parts=[types.Part(text=message)])
    text = ""
    try:
        try:
            async for event in runner.run_async(
                user_id="guardrail", session_id=session.id, new_message=content
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    text = "".join(part.text or "" for part in event.content.parts)
        except Exception as exc:
            raise GuardrailFailure(
                f"Guardrail model call failed: {exc}. The request could not be checked."
            ) from exc
        return parse_decision(text)
    finally:
        await sessions.delete_session(
            app_name=APP_NAME, user_id="guardrail", session_id=session.id
        )
