"""Recorded token accounting and execution spans; never estimate historical usage."""

import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")



def timestamp():
    return datetime.now(UTC).isoformat()


def usage_numbers(value):
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in TOKEN_KEYS if type(value.get(key)) is int and value[key] >= 0}
    if "total_tokens" not in result and all(key in result for key in ("input_tokens", "output_tokens")):
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def add_usage(total, value):
    for key, number in usage_numbers(value).items():
        total[key] = total.get(key, 0) + number


def begin_span(kind, stage, name, *, parent_id=None, context=None):
    return {
        "id": "span-" + uuid.uuid4().hex[:16],
        "kind": kind,
        "stage": stage,
        "name": name,
        "status": "running",
        "started_at": timestamp(),
        "finished_at": None,
        "parent_id": parent_id,
        "context": context or {},
        "usage": {},
        "usage_status": "pending" if kind == "agent" else "local",
    }


@contextmanager
def local_span(events, name, *, page=None):
    event = begin_span("tool", "reader", name, context={"page": page} if page else {})
    events.append(event)
    started = time.monotonic()
    try:
        yield event
        event["status"] = "completed"
    except BaseException:
        event["status"] = "failed"
        raise
    finally:
        event.update(finished_at=timestamp(), duration_ms=round((time.monotonic() - started) * 1000))
