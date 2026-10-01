"""Recall and save customer memories. The agent has no memory tool (R-1, R-4, R-6)."""

import os

HEADER = "Relevant memories about this customer (from Mem0):"
# T-MEM-TOPK, T-MEM-MINSCORE, T-MEM-MAXCHARS. The cutoff stays at 0.25.
TOPK = 5
MINSCORE = 0.25
MAXCHARS = 500


class MemoryFailure(Exception):
    """Mem0 could not be checked. Never treat this as an empty memory (P-4)."""


def _client():
    key = os.environ.get("MEM0_API_KEY")
    if not key:
        raise MemoryFailure("MEM0_API_KEY is missing. The request could not be checked.")
    from mem0 import MemoryClient

    return MemoryClient(api_key=key)


def _rows(payload):
    if isinstance(payload, dict):
        payload = payload.get("results") or []
    if not isinstance(payload, list):
        raise MemoryFailure(
            "Mem0 recall returned an unparseable result. The request could not be checked."
        )
    rows = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        text = row.get("memory") if isinstance(row.get("memory"), str) else row.get("text")
        if not isinstance(text, str) or text == "":
            continue
        try:
            score = float(row.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        rows.append((text, score))
    return rows


def recall(user_id, message):
    """Search this user's memories and mark which ones enter the prompt (R-1, R-2, R-5)."""
    try:
        payload = _client().search(
            message,
            filters={"user_id": user_id},
            top_k=TOPK,
            threshold=0.0,
        )
    except MemoryFailure:
        raise
    except Exception as exc:
        raise MemoryFailure(
            f"Mem0 recall failed: {exc}. The request could not be checked."
        ) from exc
    memories = []
    for text, score in _rows(payload):
        if len(text) > MAXCHARS:
            memories.append({"memory": text, "score": score, "inserted": False, "reason": "too long"})
        elif score < MINSCORE:
            memories.append({"memory": text, "score": score, "inserted": False, "reason": "below cutoff"})
        else:
            memories.append({"memory": text, "score": score, "inserted": True, "reason": None})
    return memories


def with_memories(message, memories):
    """Put inserted memories above the customer's message (R-3)."""
    inserted = [item["memory"] for item in memories if item["inserted"]]
    if not inserted:
        return message
    lines = [HEADER, *[f"- {text}" for text in inserted], "", message]
    return "\n".join(lines)


def save_message(user_id, message):
    """Store the customer's message only. Mem0 extracts it in the background (R-4)."""
    try:
        _client().add(message, user_id=user_id)
    except MemoryFailure:
        raise
    except Exception as exc:
        raise MemoryFailure(
            f"Mem0 save failed: {exc}. The request could not be checked."
        ) from exc
