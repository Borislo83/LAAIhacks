"""Memory agent: the team's long-term memory of what each shopper prefers and decided.

Preferences come from the Focal Studio profile in Supabase (laai_user_preferences,
read-only) when SUPABASE_URL, SUPABASE_SECRET_KEY and FOCAL_MEMORY_USER_ID are set.
Past research and decisions are kept in the agent's own storage, keyed by a hash of the
chat sender, so nothing personal leaves the agent. Every reply includes the request it
made and the response it got, so the chat can show the call.
"""

import hashlib
import os
import time

import aiohttp
from pydantic.v1 import Field
from uagents import Context, Model

from . import net
from .agents import make_agent

HISTORY = 10
PREFERENCE_FIELDS = "shopping_priority,proactive_enabled,notes,updated_at"


class MemoryRequest(Model):
    user: str = Field(min_length=1, max_length=200)
    # Set to save a decision; omit to only recall.
    remember: dict | None = None


class MemoryResponse(Model):
    source: str
    preferences: dict = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)
    request: str
    response: dict = Field(default_factory=dict)
    status: int | None = None
    summary: str


def user_key(user: str) -> str:
    return "memory:" + hashlib.sha256(user.encode()).hexdigest()[:24]


async def profile() -> tuple[str, int | None, dict]:
    """Studio preferences from Supabase as (request line, HTTP status, body)."""
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SECRET_KEY", "")
    user_id = os.getenv("FOCAL_MEMORY_USER_ID", "")
    path = f"/rest/v1/laai_user_preferences?select={PREFERENCE_FIELDS}&user_id=eq.<profile>"
    if not (base and key and user_id):
        return f"GET {path}", None, {}
    headers = {"apikey": key, "Accept": "application/json"}
    if key.startswith("eyJ"):  # legacy service_role JWT
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with net.session().get(
            f"{base}/rest/v1/laai_user_preferences",
            params={"select": PREFERENCE_FIELDS, "user_id": f"eq.{user_id}"},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as response:
            rows = await response.json(content_type=None)
            body = rows[0] if isinstance(rows, list) and rows else {}
            return f"GET {path}", response.status, body
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return f"GET {path}", None, {}


async def handle(ctx: Context, req: MemoryRequest) -> MemoryResponse:
    key = user_key(req.user)
    # Latest decision per product; drop placeholder names an extractor may return.
    history, seen = [], set()
    for entry in ctx.storage.get(key) or []:
        product = str(entry.get("product") or "")
        if product.lower() in seen or "no product" in product.lower():
            continue
        seen.add(product.lower())
        history.append(entry)
    if req.remember:
        history = ([{**req.remember, "at": int(time.time())}] + history)[:HISTORY]
        ctx.storage.set(key, history)
        return MemoryResponse(
            source="agent_storage",
            history=history,
            request=f"PUT agent-storage/{key}",
            response={"saved": req.remember, "entries": len(history)},
            status=200,
            summary=f"Saved to memory ({len(history)} entries for this shopper).",
        )
    request, status, preferences = await profile()
    source = "supabase" if status == 200 and preferences else "agent_storage"
    if not preferences and history:
        last = history[0]
        preferences = {"shopping_priority": last.get("priority", "balanced")}
    summary = (
        f"Profile loaded from {'Focal Studio (Supabase)' if source == 'supabase' else 'agent memory'}; "
        f"{len(history)} past decision(s)."
    )
    return MemoryResponse(
        source=source,
        preferences=preferences,
        history=history[:5],
        request=request,
        response={"preferences": preferences, "recent": history[:5]},
        status=status,
        summary=summary,
    )


def build_memory_agent(port: int | None = None):
    from uagents import Protocol

    from .chat import build_chat_protocol

    agent = make_agent(
        "focal-memory", os.getenv("MEMORY_AGENT_SEED", "laai memory agent dev seed"), port
    )
    proto = Protocol(name="Memory", version="0.1.0")

    @proto.on_message(MemoryRequest, replies=MemoryResponse)
    async def on_request(ctx: Context, sender: str, req: MemoryRequest):
        await ctx.send(sender, await handle(ctx, req))

    async def answer(ctx: Context, text: str) -> str:
        return (await handle(ctx, MemoryRequest(user="chat"))).summary

    agent.include(proto, publish_manifest=True)
    agent.include(build_chat_protocol(answer, extract_product=False), publish_manifest=True)
    return agent
