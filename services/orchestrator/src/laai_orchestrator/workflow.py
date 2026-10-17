"""Shared v1.0 session API backed by transactional Supabase RPCs."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field, HttpUrl, field_validator

from .models import InputModel

Identifier = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")]


class Submission(InputModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: Identifier


class Fact(InputModel):
    label: str = Field(max_length=120)
    value: str = Field(max_length=1000)


class PageSubmission(Submission):
    page_id: Identifier
    url: HttpUrl
    kind: Literal["product", "article", "website", "reader", "other"]
    title: str = Field(min_length=1, max_length=500)
    summary: list[Annotated[str, Field(max_length=2000)]] = Field(default_factory=list, max_length=20)
    facts: list[Fact] = Field(default_factory=list, max_length=30)
    source_label: str = Field(max_length=200)
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("A timezone is required")
        return value


class Action(InputModel):
    action_id: Identifier
    type: Literal["compare_prices", "find_reviews", "open_url", "read_article", "custom"]
    label: str = Field(min_length=1, max_length=160)
    description: str = Field(max_length=2000)
    expires_at: datetime
    requires_approval: Literal[True] = True

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("A timezone is required")
        return value


class ActionsSubmission(Submission):
    page_id: Identifier
    actions: list[Action] = Field(max_length=20)

    @field_validator("actions")
    @classmethod
    def unique_ids(cls, actions):
        if len({a.action_id for a in actions}) != len(actions):
            raise ValueError("Action IDs must be unique")
        return actions


class Decision(Submission):
    expected_revision: int = Field(ge=1)


class ActionResult(InputModel):
    request_id: Identifier
    status: Literal["running", "completed", "failed"]
    message: str = Field(max_length=16000)
    result_url: HttpUrl | None = None

    @field_validator("result_url")
    @classmethod
    def https_only(cls, url):
        if url and url.scheme != "https":
            raise ValueError("Result links require HTTPS")
        return url


def router(current_user):
    routes = APIRouter()
    user_dependency = Depends(current_user)
    roles = {
        "create": {"web"}, "page": {"web"}, "actions": {"agent"},
        "display": {"web", "g2"}, "approve": {"g2"}, "reject": {"g2"},
        "events": {"executor"}, "result": {"executor"},
    }

    async def operate(request, user, operation, session_id=None, body=None):
        request.state.workflow_request_id = (body or {}).get("request_id")
        allowed = roles.get(operation)
        actor_roles = getattr(request.state, "actor_roles", {"web"})
        if allowed and not actor_roles.intersection(allowed):
            raise HTTPException(403, {"code": "FORBIDDEN", "message": "Caller role cannot perform this operation"})
        payload = body or {}
        result = await request.app.state.store.request(
            "POST", "rpc/laai_workflow",
            json={"p_user_id": str(user), "p_operation": operation,
                  "p_session_id": str(session_id) if session_id else None, "p_payload": payload},
        )
        if "error" in result:
            raise HTTPException(result.get("status", 409), result["error"])
        return result

    @routes.get("/healthz")
    async def health():
        return {"status": "ok"}

    @routes.post("/v1/sessions", status_code=201)
    async def create_session(request: Request, user: UUID = user_dependency):
        return await operate(request, user, "create")

    @routes.get("/v1/sessions/{session_id}")
    async def session_status(session_id: UUID, request: Request, user: UUID = user_dependency):
        return await operate(request, user, "status", session_id)

    @routes.put("/v1/sessions/{session_id}/page")
    async def page(session_id: UUID, body: PageSubmission, request: Request, user: UUID = user_dependency):
        return await operate(request, user, "page", session_id, body.model_dump(mode="json"))

    @routes.put("/v1/sessions/{session_id}/actions")
    async def actions(session_id: UUID, body: ActionsSubmission, request: Request, user: UUID = user_dependency):
        return await operate(request, user, "actions", session_id, body.model_dump(mode="json"))

    @routes.get("/v1/sessions/{session_id}/display")
    async def display(session_id: UUID, request: Request, after_revision: int = Query(default=0, ge=0), user: UUID = user_dependency):
        result = await operate(request, user, "display", session_id, {"after_revision": after_revision})
        return Response(status_code=204) if result.get("unchanged") else result

    @routes.post("/v1/sessions/{session_id}/actions/{action_id}/approve")
    async def approve(session_id: UUID, action_id: Identifier, body: Decision, request: Request, user: UUID = user_dependency):
        return await operate(request, user, "approve", session_id, {**body.model_dump(mode="json"), "action_id": action_id})

    @routes.post("/v1/sessions/{session_id}/actions/{action_id}/reject")
    async def reject(session_id: UUID, action_id: Identifier, body: Decision, request: Request, user: UUID = user_dependency):
        return await operate(request, user, "reject", session_id, {**body.model_dump(mode="json"), "action_id": action_id})

    @routes.get("/v1/sessions/{session_id}/events")
    async def events(session_id: UUID, request: Request, after: str = Query(default="", pattern=r"^\d{0,19}$"), user: UUID = user_dependency):
        return await operate(request, user, "events", session_id, {"after": after})

    @routes.post("/v1/sessions/{session_id}/actions/{action_id}/result")
    async def result(session_id: UUID, action_id: Identifier, body: ActionResult, request: Request, user: UUID = user_dependency):
        return await operate(request, user, "result", session_id, {**body.model_dump(mode="json"), "action_id": action_id})

    return routes


def install_workflow(app, current_user):
    from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def shared(request):
        return request.url.path.startswith("/v1/sessions")

    @app.exception_handler(StarletteHTTPException)
    async def contract_error(request, exc):
        if not shared(request):
            return await http_exception_handler(request, exc)
        codes = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "SESSION_NOT_FOUND",
                 409: "CONFLICT", 422: "INVALID_PAYLOAD", 429: "RATE_LIMITED", 503: "BACKEND_UNAVAILABLE"}
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": {
            "code": detail.get("code", codes.get(exc.status_code, "INVALID_PAYLOAD")),
            "message": detail.get("message", "Request failed"),
            "request_id": getattr(request.state, "workflow_request_id", None),
        }}, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_payload(request, exc):
        if not shared(request):
            return await request_validation_exception_handler(request, exc)
        return JSONResponse(status_code=422, content={"error": {
            "code": "INVALID_PAYLOAD", "message": "Request does not match the v1.0 schema",
            "request_id": exc.body.get("request_id") if isinstance(exc.body, dict) else None,
        }})

    app.include_router(router(current_user))
