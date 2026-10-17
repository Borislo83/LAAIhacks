"""Relay real per-request agent events; final JSON endpoints remain compatible."""

import asyncio
from contextlib import suppress
from uuid import uuid4

import httpx


async def agent_request(route, payload, config, http, emit=None, timeout=120):
    base = str(config.agents_url).rstrip("/")
    if emit is None:
        return await http.post(base + "/" + route, json=payload, timeout=timeout)
    progress_id = uuid4().hex
    task = asyncio.create_task(
        http.post(base + "/" + route, json={**payload, "progress_id": progress_id}, timeout=timeout)
    )
    cursor = 0

    async def drain():
        nonlocal cursor
        try:
            response = await http.post(
                base + "/progress", json={"progress_id": progress_id, "after": cursor}, timeout=3
            )
            response.raise_for_status()
            for event in response.json()["events"]:
                if event["seq"] > cursor:
                    cursor = event["seq"]
                    emit({"type": "progress", **event})
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # A telemetry outage must not retry or duplicate the actual operation.
            pass

    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.4)
            await drain()
        return await task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def event_stream(work):
    queue = asyncio.Queue()

    async def run():
        try:
            result = await work(queue.put_nowait)
            queue.put_nowait({"type": "result", "data": result.model_dump(mode="json")})
        except Exception:  # noqa: BLE001 -- stream safe failure, never upstream secrets
            queue.put_nowait(
                {"type": "error", "message": "The request could not finish. Please try again."}
            )
        finally:
            queue.put_nowait(None)

    import json

    task = asyncio.create_task(run())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=10)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if event is None:
                break
            yield "data: " + json.dumps(event) + "\n\n"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
