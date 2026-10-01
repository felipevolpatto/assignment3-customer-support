"""Data Masker process. Agent card plus message/send on port 10003 (K-1, K-2)."""

import json
import re
import uuid

import uvicorn
from fastapi import FastAPI

PORT = 10003
# Emails, then card-like runs of 13–19 digits, then phone numbers with separators.
# Street addresses are left as written. The customer's own email is left as written.
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)")

app = FastAPI()


def mask_text(text, user_email):
    """Replace other people's emails, phones, and card numbers. Leave every other character."""
    counts = {"emails": 0, "phones": 0, "cards": 0}
    own = (user_email or "").casefold()

    def email(match):
        found = match.group(0)
        if own and found.casefold() == own:
            return found
        counts["emails"] += 1
        return "[EMAIL]"

    def card(match):
        counts["cards"] += 1
        return "[CARD]"

    def phone(match):
        counts["phones"] += 1
        return "[PHONE]"

    masked = PHONE.sub(phone, CARD.sub(card, EMAIL.sub(email, text)))
    return {"text": masked, **counts}


def _card():
    return {
        "name": "Data Masker",
        "description": "Masks emails, phone numbers, and card numbers in a support reply.",
        "url": f"http://127.0.0.1:{PORT}/",
        "version": "1.0.0",
        "protocolVersion": "0.3.0",
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "mask-reply",
                "name": "Mask a support reply",
                "description": "Return the reply with PII replaced, plus counts of each change.",
                "tags": ["privacy"],
            }
        ],
    }


@app.get("/.well-known/agent.json")
def agent_card():
    return _card()


def _message(params):
    message = (params or {}).get("message") or {}
    text = ""
    for part in message.get("parts") or []:
        if isinstance(part, dict) and part.get("text"):
            text = part["text"]
            break
    email = (message.get("metadata") or {}).get("user_email") or ""
    return text, email


@app.post("/")
async def message_send(body: dict):
    call_id = body.get("id")
    if body.get("method") != "message/send":
        return {
            "jsonrpc": "2.0",
            "id": call_id,
            "error": {"code": -32601, "message": "method not found"},
        }
    text, user_email = _message(body.get("params"))
    try:
        masked = mask_text(text, user_email)
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": call_id,
            "error": {"code": -32603, "message": str(exc)},
        }
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "result": {
            "kind": "message",
            "role": "agent",
            "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": json.dumps(masked)}],
        },
    }


def main():
    uvicorn.run(app, host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
