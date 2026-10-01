"""Call the Data Masker over A2A message/send (K-1, K-3)."""

import json

import httpx

MASKER_URL = "http://127.0.0.1:10003/"


class MaskerFailure(Exception):
    """The Masker could not be checked. Never treat this as unchanged text (P-4)."""


def _text(result):
    for part in result.get("parts") or []:
        if isinstance(part, dict) and part.get("text"):
            return part["text"]
    return ""


def detail_for(counts):
    """The step detail names what changed, or says nothing was masked (K-3)."""
    labels = []
    emails = counts["emails"]
    phones = counts["phones"]
    cards = counts["cards"]
    if emails:
        labels.append(f"{emails} email" + ("" if emails == 1 else "s"))
    if phones:
        labels.append(f"{phones} phone number" + ("" if phones == 1 else "s"))
    if cards:
        labels.append(f"{cards} card number" + ("" if cards == 1 else "s"))
    if not labels:
        return "nothing to mask"
    if len(labels) == 1:
        return "masked " + labels[0]
    return "masked " + ", ".join(labels[:-1]) + " and " + labels[-1]


def parse_mask(text):
    """A mask result is the new text plus three counts. Anything else is an error."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MaskerFailure(
            "Data Masker returned an unparseable result. The request could not be checked."
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise MaskerFailure(
            "Data Masker returned an unparseable result. The request could not be checked."
        )
    counts = {}
    for key in ("emails", "phones", "cards"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MaskerFailure(
                "Data Masker returned an unparseable result. The request could not be checked."
            )
        counts[key] = value
    return {"text": data["text"], "detail": detail_for(counts)}


async def ask_masker(reply, user_email, turn_id):
    payload = {
        "jsonrpc": "2.0",
        "id": turn_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": turn_id,
                "parts": [{"kind": "text", "text": reply}],
                "metadata": {"user_email": user_email},
            }
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(MASKER_URL, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.TimeoutException as exc:
        raise MaskerFailure(
            "Data Masker timed out. The request could not be checked."
        ) from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise MaskerFailure(
            "Data Masker unreachable: connection refused. The request could not be checked."
        ) from exc
    if body.get("error"):
        raise MaskerFailure(
            "Data Masker error: "
            + str(body["error"].get("message", body["error"]))
            + ". The request could not be checked."
        )
    return parse_mask(_text(body.get("result") or {}))
