"""Bounded, ephemeral progress keyed by an unguessable per-request capability."""

import time
from collections import OrderedDict
from contextvars import ContextVar
from functools import wraps

from pydantic.v1 import Field
from uagents import Model

active = ContextVar("agent_progress", default=(None, "coordinator"))
_runs = OrderedDict()


class ProgressQuery(Model):
    progress_id: str = Field(min_length=32, max_length=64)
    after: int = Field(default=0, ge=0)


class ProgressBatch(Model):
    events: list[dict] = Field(default_factory=list)


def publish(progress_id, kind, stage, message, **details):
    if not progress_id:
        return
    cutoff = time.monotonic() - 600
    for key in list(_runs):
        if _runs[key]["updated"] < cutoff:
            del _runs[key]
    if progress_id not in _runs:
        if len(_runs) >= 128:
            _runs.popitem(last=False)
        _runs[progress_id] = {"updated": time.monotonic(), "events": [], "seq": 0}
    run = _runs[progress_id]
    run["updated"] = time.monotonic()
    run["seq"] += 1
    run["events"].append(
        {"seq": run["seq"], "kind": kind, "stage": stage, "message": message, **details}
    )
    run["events"] = run["events"][-200:]


def report(stage, message):
    progress_id, kind = active.get()
    publish(progress_id, kind, stage, message)


def read(progress_id, after):
    run = _runs.get(progress_id)
    return [e for e in run["events"] if e["seq"] > after] if run else []


def tracked(kind):
    def decorate(fn):
        @wraps(fn)
        async def run(req):
            token = active.set((getattr(req, "progress_id", None), kind))
            started = time.perf_counter()
            subject = getattr(req, "product", None) or getattr(req, "query", None)
            report("started", f"Working on {subject or 'Amazon listing'}")
            try:
                result = await fn(req)
                publish(
                    getattr(req, "progress_id", None),
                    kind,
                    "failed" if result.error else "completed",
                    result.error or "Research complete",
                    result=result.model_dump(),
                    ms=round((time.perf_counter() - started) * 1000),
                )
                return result
            except Exception:
                report("failed", "Agent could not finish this request")
                raise
            finally:
                active.reset(token)

        return run

    return decorate
