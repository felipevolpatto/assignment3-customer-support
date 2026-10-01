"""Call the Security Judge over A2A message/send (J-1, J-4)."""

import json

import httpx

JUDGE_URL = "http://127.0.0.1:10002/"


class JudgeFailure(Exception):
    """The Judge could not be checked. Never treat this as allow (P-4)."""


def _text(result):
    parts = result.get("parts") or []
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            return part["text"]
    return ""


def parse_verdict(text):
    """A verdict is allow or block with a reason. Anything else is an error (J-3)."""
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JudgeFailure(
            "Security Judge returned an unparseable verdict. The request could not be checked."
        ) from exc
    verdict = data.get("verdict")
    reason = data.get("reason")
    if verdict not in {"allow", "block"} or not isinstance(reason, str) or not reason.strip():
        raise JudgeFailure(
            "Security Judge returned an unparseable verdict. The request could not be checked."
        )
    return {"verdict": verdict, "reason": reason.strip()}


async def ask_judge(message, turn_id):
    payload = {
        "jsonrpc": "2.0",
        "id": turn_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": turn_id,
                "parts": [{"kind": "text", "text": message}],
            }
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(JUDGE_URL, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.TimeoutException as exc:
        raise JudgeFailure(
            "Security Judge timed out. The request could not be checked."
        ) from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise JudgeFailure(
            "Security Judge unreachable: connection refused. The request could not be checked."
        ) from exc
    if body.get("error"):
        raise JudgeFailure(
            "Security Judge error: "
            + str(body["error"].get("message", body["error"]))
            + ". The request could not be checked."
        )
    return parse_verdict(_text(body.get("result") or {}))
