import hashlib
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from time import monotonic
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .chat import buyer_call, call_agent, chat
from .config import DEMO_TOKENS, LOOPBACK_CLIENTS, PROXY_HEADERS, Config
from .models import (
    FIELDS,
    AgentKind,
    BuyerPreparation,
    ChatReply,
    ChatRequest,
    ContextResponse,
    DispatchRequest,
    MessageSend,
    MessageSent,
    PageContext,
    Task,
    ToolArgs,
    UserSettings,
    agent_payload,
)
from .predictor import predict
from .preferences import read_preferences
from .preferences import router as preferences_router
from .progress import event_stream
from .store import Store
from .workflow import install_workflow

bearer = HTTPBearer(auto_error=False)
AUTH_CACHE_SECONDS = 30
ALL_ROLES = {"web", "agent", "g2", "executor"}

MESSAGE_SENDS_PER_MINUTE = 5


class RateLimit:
    """Sliding-window limit per user, in process. Enough for one orchestrator instance."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[UUID, deque[float]] = {}

    def allow(self, user: UUID) -> bool:
        now = monotonic()
        if len(self.hits) > 4096:  # Forget idle users so memory stays bounded.
            self.hits = {u: h for u, h in self.hits.items() if h and h[-1] > now - self.window}
        hits = self.hits.setdefault(user, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


class AuthCache:
    """Briefly remembers verified tokens so G2 polling does not call Supabase Auth each time.

    Keys are token hashes, never raw tokens. Only successful verifications are stored, so a
    revoked token can stay usable for at most AUTH_CACHE_SECONDS.
    """

    def __init__(self, max_entries=1024, ttl=AUTH_CACHE_SECONDS):
        self.entries: OrderedDict[str, tuple[float, UUID, frozenset[str]]] = OrderedDict()
        self.max_entries, self.ttl = max_entries, ttl

    @staticmethod
    def key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def get(self, token: str) -> tuple[UUID, set[str]] | None:
        key = self.key(token)
        entry = self.entries.get(key)
        if entry is None:
            return None
        if entry[0] <= monotonic():
            del self.entries[key]
            return None
        return entry[1], set(entry[2])

    def put(self, token: str, user: UUID, roles: set[str]) -> None:
        key = self.key(token)
        self.entries.pop(key, None)
        self.entries[key] = (monotonic() + self.ttl, user, frozenset(roles))
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> UUID:
    if not credentials:
        raise HTTPException(401, "Supabase access token required")
    config = request.app.state.config
    if config.demo_auth_mode and credentials.credentials in DEMO_TOKENS:
        # Proxied requests carry forwarding headers; never trust fixtures through them.
        local = request.client and request.client.host in LOOPBACK_CLIENTS
        proxied = PROXY_HEADERS.intersection(request.headers.keys())
        if not local or proxied:
            raise HTTPException(401, "Demo tokens are accepted only from this machine")
        request.state.actor_roles = set(DEMO_TOKENS[credentials.credentials])
        return config.demo_user_id
    cache = request.app.state.auth_cache
    if cached := cache.get(credentials.credentials):
        user, request.state.actor_roles = cached
        return user
    try:
        response = await request.app.state.http.get(
            str(config.supabase_url).rstrip("/") + "/auth/v1/user",
            headers={
                "apikey": config.supabase_publishable_key.get_secret_value(),
                "Authorization": f"Bearer {credentials.credentials}",
            },
        )
        if response.status_code in (401, 403):
            raise HTTPException(401, "Invalid or expired access token")
        response.raise_for_status()
        identity = response.json()
        # app_metadata is assigned by trusted server administration, unlike user_metadata.
        roles = identity.get("app_metadata", {}).get("laai_roles", ["web"])
        if not isinstance(roles, list):
            roles = []
        user = UUID(identity["id"])
        roles = {role for role in roles if isinstance(role, str)} & ALL_ROLES
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(503, "Supabase authentication unavailable") from exc
    cache.put(credentials.credentials, user, roles)
    request.state.actor_roles = roles
    return user


User = Annotated[UUID, Depends(current_user)]


async def chat_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> UUID | None:
    if not request.app.state.config.chat_auth_required:
        return None
    return await current_user(request, credentials)


def require_args(kind: AgentKind, body: ToolArgs) -> None:
    if not body.suits(kind):
        raise HTTPException(422, f"{kind.value} requires {FIELDS[kind][0]}")


def sse(events) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def create_app(config: Config | None = None, http: httpx.AsyncClient | None = None) -> FastAPI:
    config = config or Config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.http = http or httpx.AsyncClient(timeout=20, follow_redirects=False)
        app.state.store = Store(config, app.state.http)
        yield
        if http is None:
            await app.state.http.aclose()

    app = FastAPI(title="LAAI Orchestrator", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.auth_cache = AuthCache()
    app.state.message_sends = RateLimit(MESSAGE_SENDS_PER_MINUTE, 60)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/auth/config")
    async def auth_config():
        # Only browser-safe credentials. Never return the backend service key.
        return {
            "supabase_url": str(config.supabase_url).rstrip("/"),
            "supabase_publishable_key": config.supabase_publishable_key.get_secret_value(),
        }

    @app.post("/v1/agents/{kind}")
    async def direct_agent(kind: AgentKind, body: ToolArgs, request: Request, _: User):
        require_args(kind, body)
        return await call_agent(kind, body, config, request.app.state.http)

    @app.get("/v1/settings", response_model=UserSettings)
    async def get_settings(request: Request, user: User):
        return await request.app.state.store.settings(user)

    @app.put("/v1/settings", response_model=UserSettings)
    async def put_settings(settings: UserSettings, request: Request, user: User):
        return await request.app.state.store.save_settings(user, settings)

    @app.post("/v1/contexts", response_model=ContextResponse, status_code=201)
    async def context(page: PageContext, request: Request, user: User):
        store = request.app.state.store
        settings = await store.settings(user)
        preferences = await read_preferences(request, user)
        prediction, source = await predict(page, config, request.app.state.http, preferences)
        tasks = [
            {"kind": kind, "payload": agent_payload(kind, prediction.product, page)}
            for kind in (prediction.tasks if prediction else [])
            if kind in settings.enabled_agents
        ]
        saved = await store.save_context(user, page, source, tasks)
        return ContextResponse.model_validate(saved)

    @app.get("/v1/tasks", response_model=list[Task])
    async def tasks(
        request: Request,
        user: User,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return await request.app.state.store.tasks(user, limit, offset)

    @app.get("/v1/tasks/{task_id}", response_model=Task)
    async def task(task_id: UUID, request: Request, user: User):
        return await request.app.state.store.task(user, task_id)

    @app.post("/v1/tasks/{task_id}/dispatch", response_model=Task)
    async def dispatch(task_id: UUID, body: DispatchRequest, request: Request, user: User):
        store = request.app.state.store
        task = await store.claim(user, task_id, body.confirmed)
        result, error = None, None
        try:
            # Only an enum can select a route. No URL from page/LLM data is fetched.
            response = await request.app.state.http.post(
                str(config.agents_url).rstrip("/") + "/" + task.kind.value,
                json=task.payload,
                timeout=45,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
                raise TypeError("Invalid agent result")
        except (httpx.HTTPError, ValueError, TypeError):
            result = None
            error = "Agent unavailable or returned an invalid response"
        return await store.finish(user, task_id, result, error)

    @app.post("/v1/chat", response_model=ChatReply)
    async def chat_route(
        body: ChatRequest,
        request: Request,
        user: Annotated[UUID | None, Depends(chat_user)],
    ):
        if body.messages[-1].role != "user":
            raise HTTPException(422, "Last message must be from the user")
        preferences = await read_preferences(request, user) if user else None
        return await chat(
            body.messages,
            config,
            request.app.state.http,
            purchase_confirmation=body.purchase_confirmation,
            preferences=preferences,
        )

    @app.post("/v1/chat/stream", response_class=StreamingResponse)
    async def stream_chat(body: ChatRequest, request: Request, user: User):
        if body.messages[-1].role != "user":
            raise HTTPException(422, "Last message must be from the user")
        preferences = await read_preferences(request, user)
        return sse(
            event_stream(
                lambda emit: chat(
                    body.messages,
                    config,
                    request.app.state.http,
                    emit,
                    body.purchase_confirmation,
                    preferences=preferences,
                )
            )
        )

    @app.post("/v1/agents/{kind}/stream", response_class=StreamingResponse)
    async def stream_agent(
        kind: AgentKind,
        body: ToolArgs,
        request: Request,
        _: User,
    ):
        require_args(kind, body)
        return sse(
            event_stream(lambda emit: call_agent(kind, body, config, request.app.state.http, emit))
        )

    # open_url only appears for link delivery, so earlier clients see the same shape.
    @app.post("/v1/messages/send", response_model=MessageSent, response_model_exclude_none=True)
    async def send_message(body: MessageSend, request: Request, user: User):
        # Always a verified sign-in (never chat_user), so CHAT_AUTH_REQUIRED=false can't open it.
        if "web" not in request.state.actor_roles:
            raise HTTPException(403, "Only the signed-in owner can send messages")
        token = config.imessage_executor_token.get_secret_value()
        if not token:
            raise HTTPException(503, "Messaging executor not configured")
        if not request.app.state.message_sends.allow(user):
            raise HTTPException(429, "Too many messages; wait a minute and try again")
        try:
            response = await request.app.state.http.post(
                str(config.agents_url).rstrip("/") + "/imessage/send",
                json={"draft_id": body.draft_id, "text": body.text, "executor_token": token},
                timeout=60,
            )
            response.raise_for_status()
            # Re-validate so nothing but status and summary (never the token) reaches clients.
            return MessageSent.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                502, "Messaging agent unavailable; check Messages before trying again"
            ) from exc

    @app.post("/v1/buyer/prepare/stream", response_class=StreamingResponse)
    async def stream_buyer(body: BuyerPreparation, request: Request, _: User):
        if not body.product and not body.url:
            raise HTTPException(422, "Supply a product name or Amazon product URL")
        return sse(
            event_stream(
                lambda emit: buyer_call(body.model_dump(), config, request.app.state.http, emit)
            )
        )

    app.include_router(preferences_router(current_user))
    install_workflow(app, current_user)
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8001)
