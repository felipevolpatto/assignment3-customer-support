"""One turn of the support agent, as events. CLI and web only render them (P-1)."""

import asyncio
import json
import time
import uuid
from pathlib import Path

import yaml
from google.genai import types
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from guards.guardrail import BLOCKED_REPLY, GuardrailFailure, check as guardrail_check
from guards.judge_client import JudgeFailure, ask_judge
from guards.masker_client import MaskerFailure, ask_masker
from guards.sanitizer import check as sanitize_message
from support.memory import MemoryFailure, recall, save_message, with_memories
from support.telemetry import flush_traces, trace_url

REFUSAL = "I can't help with that request."

RUNS = Path("runs")
TOOLS_FILE = Path(__file__).resolve().parent.parent / "mcp_toolbox" / "tools.yaml"


def _tool_info():
    """Read each tool's SQL from tools.yaml, so the shown SQL is the SQL that runs."""
    info = {}
    for doc in yaml.safe_load_all(TOOLS_FILE.read_text()):
        if not doc or doc.get("kind") != "tool" or "statement" not in doc:
            continue
        statement = doc["statement"].strip()
        info[doc["name"]] = {
            "access": "READ" if statement.upper().startswith("SELECT") else "WRITE",
            "statement": statement,
            "params": [param["name"] for param in doc.get("parameters") or []],
        }
    return info


TOOL_INFO = _tool_info()


def _tool_failed(result):
    """A toolbox failure comes back as text, not as an error field."""
    if isinstance(result, str):
        return result.lower().startswith("error")
    if isinstance(result, dict):
        if result.get("error"):
            return True
        inner = result.get("result")
        return isinstance(inner, str) and inner.lower().startswith("error")
    return False


def unwrap(raw):
    """Toolbox wraps rows as a JSON string inside {"result": "..."}."""
    if isinstance(raw, dict) and "result" in raw:
        raw = raw["result"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def decode_args(args):
    """A model may send action-log parameters as a JSON string. Decode before emitting."""
    if not isinstance(args, dict):
        return {}
    decoded = {}
    for key, value in args.items():
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value
            if isinstance(parsed, (dict, list)):
                value = parsed
        decoded[key] = value
    return decoded


def _text(event):
    if not event.content or not event.content.parts:
        return ""
    return "".join(part.text or "" for part in event.content.parts)


def _tokens(event):
    usage = getattr(event, "usage_metadata", None)
    if usage is None:
        return 0, 0
    return (
        getattr(usage, "prompt_token_count", 0) or 0,
        getattr(usage, "candidates_token_count", 0) or 0,
    )


def _guard_step(key, status, detail, ms, span_name, kind):
    return {
        "type": "step",
        "key": key,
        "status": status,
        "detail": detail,
        "ms": ms,
        "span": span_name,
        "kind": kind,
    }


async def _guards(tracer, message, turn_id, steps, outcome):
    """Sanitize, then the Judge. A block or a Judge failure stops the turn (P-3, P-4)."""
    started = time.perf_counter()
    with tracer.start_as_current_span("security.sanitize") as span:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.GUARDRAIL.value
        )
        yield {"type": "stage", "key": "sanitize", "label": "Sanitizer"}
        allowed, detail = sanitize_message(message)
        ms = int((time.perf_counter() - started) * 1000)
        status = "passed" if allowed else "blocked"
        steps.append({"key": "sanitize", "status": status, "ms": ms})
        yield _guard_step("sanitize", status, detail, ms, "security.sanitize", "Python fn")
        if not allowed:
            outcome["blocked"] = ("sanitize", detail, ms)
            return

    started = time.perf_counter()
    with tracer.start_as_current_span("security.a2a_judge") as span:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.GUARDRAIL.value
        )
        yield {"type": "stage", "key": "judge", "label": "A2A Security Judge"}
        try:
            verdict = await ask_judge(message, turn_id)
        except JudgeFailure as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            outcome["failure"] = exc
            outcome["failure_step"] = "judge"
            return
        ms = int((time.perf_counter() - started) * 1000)
        if verdict["verdict"] == "block":
            steps.append({"key": "judge", "status": "blocked", "ms": ms})
            yield _guard_step("judge", "blocked", verdict["reason"], ms, "security.a2a_judge", "A2A")
            outcome["blocked"] = ("judge", verdict["reason"], ms)
            return
        detail = "allow: " + verdict["reason"]
        steps.append({"key": "judge", "status": "passed", "ms": ms})
        yield _guard_step("judge", "passed", detail, ms, "security.a2a_judge", "A2A")

    started = time.perf_counter()
    with tracer.start_as_current_span("guardrail.check") as span:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.GUARDRAIL.value
        )
        yield {"type": "stage", "key": "guardrail", "label": "Guardrail"}
        try:
            decision = await guardrail_check(message)
        except GuardrailFailure as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            outcome["failure"] = exc
            outcome["failure_step"] = "guardrail"
            return
        ms = int((time.perf_counter() - started) * 1000)
        if decision["decision"] == "unsafe":
            steps.append({"key": "guardrail", "status": "blocked", "ms": ms})
            yield _guard_step(
                "guardrail", "blocked", decision["reasoning"], ms, "guardrail.check", "in-process"
            )
            outcome["blocked"] = ("guardrail", decision["reasoning"], ms)
            outcome["reply"] = BLOCKED_REPLY
            return
        detail = "safe: " + decision["reasoning"]
        steps.append({"key": "guardrail", "status": "passed", "ms": ms})
        yield _guard_step("guardrail", "passed", detail, ms, "guardrail.check", "in-process")


def _recall_detail(memories):
    if not memories:
        return "nothing to recall"
    inserted = sum(1 for item in memories if item["inserted"])
    return f"inserted {inserted}, skipped {len(memories) - inserted}"


def _recall_event(memories, ms):
    event = _guard_step("recall", "passed", _recall_detail(memories), ms, "memory.recall", "Python fn")
    event["memories"] = memories
    return event


async def _complete(tracer, runner, *, user_id, session_id, message, model, turn_id, steps, stats, tool_calls, tool_ids, elapsed):
    """Recall, then the agent, then mask, then save. A failure stops the rest (P-2, P-3)."""
    yield {"type": "stage", "key": "recall", "label": "Memory recall"}
    started = time.perf_counter()
    with tracer.start_as_current_span("memory.recall") as recall_span:
        recall_span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.RETRIEVER.value
        )
        try:
            memories = await asyncio.to_thread(recall, user_id, message)
        except MemoryFailure as exc:
            recall_span.record_exception(exc)
            recall_span.set_status(Status(StatusCode.ERROR, str(exc)))
            stats["failure"] = exc
            stats["failure_step"] = "recall"
            return
        for index, item in enumerate(memories):
            recall_span.set_attribute(f"retrieval.documents.{index}.document.content", item["memory"])
            recall_span.set_attribute(f"retrieval.documents.{index}.document.score", item["score"])
    ms = int((time.perf_counter() - started) * 1000)
    steps.append({"key": "recall", "status": "passed", "ms": ms})
    yield _recall_event(memories, ms)

    yield {"type": "stage", "key": "agent", "label": "Support agent"}
    agent_started = time.perf_counter()
    content = types.Content(role="user", parts=[types.Part(text=with_memories(message, memories))])
    response = ""
    try:
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=content
        ):
            added_in, added_out = _tokens(event)
            stats["tokens_in"] += added_in
            stats["tokens_out"] += added_out
            calls = list(event.get_function_calls() or [])
            responses = list(event.get_function_responses() or [])
            if calls or (event.content and event.content.parts and not responses):
                stats["llm_calls"] += 1
                decision = "call " + calls[0].name if calls else "final answer"
                yield {
                    "type": "llm",
                    "model": model,
                    "decision": decision,
                    "tokens_in": added_in,
                    "tokens_out": added_out,
                    "ms": elapsed(),
                    "span": "call_llm",
                }
            for call in calls:
                tool_ids[call.id] = stats["next_tool_id"]
                info = TOOL_INFO.get(call.name, {"access": "READ", "statement": "", "params": []})
                yield {
                    "type": "tool_call",
                    "id": stats["next_tool_id"],
                    "name": call.name,
                    "args": decode_args(dict(call.args or {})),
                    "info": {"kind": "MCP", **info},
                    "span": f"execute_tool {call.name}",
                }
                stats["next_tool_id"] += 1
            for reply in responses:
                tool_id = tool_ids.get(reply.id, stats["next_tool_id"])
                result = unwrap(getattr(reply, "response", None))
                failed = _tool_failed(result)
                ok = not failed
                took = elapsed()
                tool_calls.append({
                    "name": reply.name,
                    "ok": ok,
                    "ms": took,
                    **({} if ok else {"error": str(result)}),
                })
                yield {
                    "type": "tool_result",
                    "id": tool_id,
                    "name": reply.name,
                    "ok": ok,
                    "ms": took,
                    "result": result,
                }
            if event.is_final_response():
                response = _text(event)
    except Exception as exc:
        stats["failure"] = exc
        stats["failure_step"] = "agent"
        return
    agent_ms = int((time.perf_counter() - agent_started) * 1000)
    steps.append({"key": "agent", "status": "passed", "ms": agent_ms})
    yield _guard_step("agent", "passed", "agent finished", agent_ms, "invoke_agent support_agent", "in-process")

    yield {"type": "stage", "key": "mask", "label": "A2A Data Masker"}
    started = time.perf_counter()
    with tracer.start_as_current_span("security.a2a_mask") as mask_span:
        mask_span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.GUARDRAIL.value
        )
        mask_span.set_attribute(SpanAttributes.INPUT_VALUE, response)
        try:
            masked = await ask_masker(response, user_id, turn_id)
        except MaskerFailure as exc:
            mask_span.record_exception(exc)
            mask_span.set_status(Status(StatusCode.ERROR, str(exc)))
            stats["failure"] = exc
            stats["failure_step"] = "mask"
            return
        response = masked["text"]
        mask_span.set_attribute(SpanAttributes.OUTPUT_VALUE, response)
    ms = int((time.perf_counter() - started) * 1000)
    steps.append({"key": "mask", "status": "passed", "ms": ms})
    yield _guard_step("mask", "passed", masked["detail"], ms, "security.a2a_mask", "A2A")

    yield {"type": "stage", "key": "save", "label": "Memory save"}
    started = time.perf_counter()
    with tracer.start_as_current_span("memory.save") as save_span:
        save_span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.TOOL.value
        )
        save_span.set_attribute(SpanAttributes.INPUT_VALUE, message)
        try:
            await asyncio.to_thread(save_message, user_id, message)
        except MemoryFailure as exc:
            save_span.record_exception(exc)
            save_span.set_status(Status(StatusCode.ERROR, str(exc)))
            stats["failure"] = exc
            stats["failure_step"] = "save"
            return
    ms = int((time.perf_counter() - started) * 1000)
    steps.append({"key": "save", "status": "passed", "ms": ms})
    yield _guard_step("save", "passed", "saved the user message", ms, "memory.save", "Python fn")
    stats["response"] = response


async def run_turn(runner, *, user_id, session_id, message, model):
    """Yield the events of one turn and write runs/<turn_id>.json before the last one."""
    turn_id = "turn_" + uuid.uuid4().hex[:12]
    started = time.perf_counter()
    tokens_in = tokens_out = 0
    llm_calls = 0
    tool_calls = []
    tool_ids = {}
    next_tool_id = 1
    clock = started

    def elapsed():
        nonlocal clock
        now = time.perf_counter()
        ms = int((now - clock) * 1000)
        clock = now
        return ms

    tracer = trace.get_tracer("support.pipeline")
    failure = None
    failure_step = "agent"
    blocked = None
    response = ""
    steps = []
    # The ADK runner must be iterated inside this span, or invoke_agent becomes its own root.
    with tracer.start_as_current_span("agent.turn") as span:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND, OpenInferenceSpanKindValues.CHAIN.value
        )
        span.set_attribute(SpanAttributes.INPUT_VALUE, message)
        span.set_attribute(SpanAttributes.USER_ID, user_id)
        trace_id = format(span.get_span_context().trace_id, "032x")
        yield {
            "type": "trace",
            "turn_id": turn_id,
            "trace_id": trace_id,
            "url": trace_url(trace_id),
        }
        outcome = {}
        async for event in _guards(tracer, message, turn_id, steps, outcome):
            yield event
        blocked = outcome.get("blocked")
        if outcome.get("failure") is not None:
            failure = outcome["failure"]
            failure_step = outcome.get("failure_step", "judge")
            span.record_exception(failure)
            span.set_status(Status(StatusCode.ERROR, str(failure)))
        elif blocked is not None:
            response = outcome.get("reply") or REFUSAL
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, response)
        else:
            stats = {
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "llm_calls": llm_calls,
                "next_tool_id": next_tool_id,
                "failure": None,
                "failure_step": "agent",
                "response": "",
            }
            async for event in _complete(
                tracer, runner,
                user_id=user_id, session_id=session_id, message=message, model=model,
                turn_id=turn_id, steps=steps, stats=stats, tool_calls=tool_calls,
                tool_ids=tool_ids, elapsed=elapsed,
            ):
                yield event
            tokens_in = stats["tokens_in"]
            tokens_out = stats["tokens_out"]
            llm_calls = stats["llm_calls"]
            response = stats["response"]
            if stats["failure"] is not None:
                failure = stats["failure"]
                failure_step = stats["failure_step"]
                span.record_exception(failure)
                span.set_status(Status(StatusCode.ERROR, str(failure)))
            else:
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, response)

    try:
        flush_traces()
    except Exception as exc:
        if failure is None:
            failure = exc

    if failure is not None:
        record = _record(
            turn_id, trace_id, user_id, message, "error", None, started,
            tokens_in, tokens_out, [], llm_calls,
        )
        record["steps"] = steps
        _write(record)
        yield {
            "type": "error",
            "step": failure_step,
            "status": 502,
            "error": str(failure),
            "terminated": "error",
        }
        return

    if blocked is not None:
        key = blocked[0]
        record = _record(
            turn_id, trace_id, user_id, message, "blocked", key, started,
            tokens_in, tokens_out, [], llm_calls,
        )
        record["steps"] = steps
        _write(record)
        yield {
            "type": "final",
            "blocked": True,
            "blocked_at": key,
            "response": response,
            "terminated": "blocked",
            "wall_clock_ms": record["wall_clock_ms"],
            "tokens": record["tokens"],
        }
        return

    record = _record(
        turn_id, trace_id, user_id, message, "done", None, started,
        tokens_in, tokens_out, tool_calls, llm_calls,
    )
    record["steps"] = steps
    _write(record)
    yield {
        "type": "final",
        "blocked": False,
        "blocked_at": None,
        "response": response,
        "terminated": "done",
        "wall_clock_ms": record["wall_clock_ms"],
        "tokens": record["tokens"],
    }


def _record(turn_id, trace_id, user_id, message, terminated, blocked_at, started, tokens_in, tokens_out, tool_calls, llm_calls):
    return {
        "turn_id": turn_id,
        "trace_id": trace_id,
        "user": user_id,
        "message": message,
        "terminated": terminated,
        "blocked_at": blocked_at,
        "wall_clock_ms": int((time.perf_counter() - started) * 1000),
        "tokens": {"in": tokens_in, "out": tokens_out},
        "steps": [],
        "tool_calls": tool_calls,
        "llm_calls": llm_calls,
    }


def _write(record):
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{record['turn_id']}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
