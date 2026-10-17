from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Config
from .models import (
    ContextResponse,
    DispatchRequest,
    PageContext,
    Task,
    UserSettings,
    agent_payload,
)
from .predictor import predict
from .store import Store

bearer = HTTPBearer(auto_error=False)


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> UUID:
    if not credentials:
        raise HTTPException(401, "Supabase access token required")
    config = request.app.state.config
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
        return UUID(response.json()["id"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(503, "Supabase authentication unavailable") from exc


User = Annotated[UUID, Depends(current_user)]


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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

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
        prediction, source = await predict(page, config, request.app.state.http)
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

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8001)
