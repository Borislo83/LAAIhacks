"""Shared v1.0 session API backed by transactional Supabase RPCs."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from .models import InputModel
from .preferences import read_preferences

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
    kind: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    summary: list[Annotated[str, Field(max_length=2000)]] = Field(
        default_factory=list, max_length=20
    )
    facts: list[Fact] = Field(default_factory=list, max_length=30)
    source_label: str = Field(max_length=200)
    updated_at: AwareDatetime


class Action(InputModel):
    action_id: Identifier
    type: Literal["compare_prices", "find_reviews", "open_url", "read_article", "custom"]
    label: str = Field(min_length=1, max_length=160)
    description: str = Field(max_length=2000)
    expires_at: AwareDatetime
    requires_approval: Literal[True] = True


class ActionsSubmission(Submission):
    page_id: Identifier
    actions: list[Action] = Field(max_length=20)

    @field_validator("actions")
    @classmethod
    def unique_ids(cls, actions):
        if len({a.action_id for a in actions}) != len(actions):
            raise ValueError("Action IDs must be unique")
        return actions


class SessionCreate(InputModel):
    device_id: UUID | None = None


class DeviceRegistration(InputModel):
    # Omit to register a new device; send an owned ID to refresh last_seen_at.
    device_id: UUID | None = None


class Decision(Submission):
    expected_revision: int = Field(ge=1)


class ActionResult(Submission):
    status: Literal["running", "completed", "failed"]
    message: str = Field(max_length=16000)
    result_url: HttpUrl | None = None

    @field_validator("result_url")
    @classmethod
    def https_only(cls, url):
        if url and url.scheme != "https":
            raise ValueError("Result links require HTTPS")
        return url


class Output(BaseModel):
    # Keep fields the database adds beyond the contract (consumed_at, action_status, ...).
    model_config = ConfigDict(extra="allow")


class ErrorDetail(Output):
    code: str
    message: str
    request_id: str | None = None


class ErrorEnvelope(Output):
    error: ErrorDetail


class SessionRef(Output):
    schema_version: Literal["1.0"] = "1.0"
    session_id: UUID
    revision: int


class SessionStatus(SessionRef):
    active_page_id: str | None = None
    expires_at: datetime


class SessionSummary(Output):
    session_id: UUID
    revision: int
    active_page_id: str | None = None
    updated_at: datetime
    expires_at: datetime


class SessionList(Output):
    """The caller's unexpired sessions, newest activity first (at most 100)."""

    sessions: list[SessionSummary]


class Device(Output):
    device_id: UUID
    last_seen_at: datetime


class DisplayFact(Output):
    label: str
    value: str


class DisplayPage(Output):
    page_id: str
    title: str
    kind: str
    summary: list[str] = []
    facts: list[DisplayFact] = []
    source_label: str | None = None


class DisplayAction(Output):
    action_id: str
    type: str
    label: str
    description: str
    status: Literal[
        "proposed", "approved", "rejected", "running", "completed", "failed", "invalidated"
    ]
    requires_approval: bool = True
    expires_at: datetime
    result: dict[str, Any] | None = None


class DisplayPreferences(Output):
    display_density: Literal["comfortable", "compact"]


class DisplayState(SessionRef):
    page: DisplayPage | None = None
    actions: list[DisplayAction] = []
    mode: Literal["summary", "actions", "confirm", "loading", "result", "error", "reader"]
    # Always null today; reserved for server-side banners.
    notice: str | None = None
    # Omitted when preferences cannot be read, rather than failing the display.
    preferences: DisplayPreferences | None = None


class ApprovalResponse(Output):
    action_id: str
    status: Literal["approved", "rejected"]
    # Rejections emit no event.
    event_id: UUID | None = None


class Event(Output):
    event_id: UUID
    cursor: str
    type: str
    session_id: UUID
    action_id: str
    page_id: str
    occurred_at: datetime
    consumed_at: datetime | None = None
    action_status: str | None = None


class EventList(Output):
    events: list[Event]


class EventAck(Output):
    event_id: UUID
    cursor: str
    consumed_at: datetime | None = None


class ResultResponse(Output):
    action_id: str
    status: Literal["running", "completed", "failed"]


ERRORS = {
    status: {"model": ErrorEnvelope, "description": description}
    for status, description in {
        401: "Missing, invalid or expired access token",
        403: "Caller role cannot perform this operation",
        404: "Session, action, event or device not found",
        409: "Conflict, stale revision or expired action",
        422: "Request does not match the v1.0 schema",
        429: "Rate limited",
        503: "Backend unavailable",
    }.items()
}


ROLES = {
    "create": {"web"},
    # Agent-side watchers find each session's page PUTs by comparing revisions.
    "sessions": {"agent", "executor"},
    "page": {"web"},
    "actions": {"agent"},
    "display": {"web", "g2"},
    "approve": {"g2"},
    "reject": {"g2"},
    "events": {"executor"},
    "result": {"executor"},
    "ack": {"executor"},
    "device": {"g2"},
}


def route(model, operation, status_code=200, responses=None):
    """Route options shared by every session endpoint: typed body, errors and role note."""
    allowed = ROLES.get(operation)
    return {
        "response_model": model,
        # Never invent fields the database did not return (e.g. omitted preferences).
        "response_model_exclude_unset": True,
        "status_code": status_code,
        "responses": {**(responses or {}), **ERRORS},
        "description": "Allowed role(s): "
        + (", ".join(f"`{r}`" for r in sorted(allowed)) if allowed else "any role; owner only")
        + ".",
    }


def router(current_user):
    routes = APIRouter()
    user_dependency = Depends(current_user)

    async def operate(request, user, operation, session_id=None, body=None):
        request.state.workflow_request_id = (body or {}).get("request_id")
        allowed = ROLES.get(operation)
        actor_roles = getattr(request.state, "actor_roles", {"web"})
        if allowed and not actor_roles.intersection(allowed):
            raise HTTPException(
                403, {"code": "FORBIDDEN", "message": "Caller role cannot perform this operation"}
            )
        payload = body or {}
        result = await request.app.state.store.request(
            "POST",
            "rpc/laai_workflow",
            json={
                "p_user_id": str(user),
                "p_operation": operation,
                "p_session_id": str(session_id) if session_id else None,
                "p_payload": payload,
            },
        )
        if "error" in result:
            raise HTTPException(result.get("status", 409), result["error"])
        return result

    @routes.get("/healthz")
    async def health():
        return {"status": "ok"}

    @routes.post("/v1/devices", **route(Device, "device", 201))
    async def register_device(
        request: Request, body: DeviceRegistration | None = None, user: UUID = user_dependency
    ):
        payload = body.model_dump(mode="json", exclude_none=True) if body else {}
        return await operate(request, user, "device", body=payload)

    @routes.post("/v1/sessions", **route(SessionRef, "create", 201))
    async def create_session(
        request: Request, body: SessionCreate | None = None, user: UUID = user_dependency
    ):
        payload = body.model_dump(mode="json", exclude_none=True) if body else {}
        return await operate(request, user, "create", body=payload)

    @routes.get("/v1/sessions", **route(SessionList, "sessions"))
    async def list_sessions(request: Request, user: UUID = user_dependency):
        return await operate(request, user, "sessions")

    @routes.get("/v1/sessions/{session_id}", **route(SessionStatus, "status"))
    async def session_status(session_id: UUID, request: Request, user: UUID = user_dependency):
        return await operate(request, user, "status", session_id)

    @routes.put("/v1/sessions/{session_id}/page", **route(SessionRef, "page"))
    async def page(
        session_id: UUID, body: PageSubmission, request: Request, user: UUID = user_dependency
    ):
        return await operate(request, user, "page", session_id, body.model_dump(mode="json"))

    @routes.put("/v1/sessions/{session_id}/actions", **route(SessionRef, "actions"))
    async def actions(
        session_id: UUID, body: ActionsSubmission, request: Request, user: UUID = user_dependency
    ):
        return await operate(request, user, "actions", session_id, body.model_dump(mode="json"))

    @routes.get(
        "/v1/sessions/{session_id}/display",
        **route(DisplayState, "display", responses={204: {"description": "Display unchanged"}}),
    )
    async def display(
        session_id: UUID,
        request: Request,
        after_revision: int = Query(default=0, ge=0),
        user: UUID = user_dependency,
    ):
        result = await operate(
            request, user, "display", session_id, {"after_revision": after_revision}
        )
        if result.get("unchanged"):
            return Response(status_code=204)
        try:
            preferences = await read_preferences(request, user)
        except (HTTPException, ValidationError):
            # Preferences are cosmetic for G2; never fail the whole display over them.
            return result
        result["preferences"] = {"display_density": preferences.display_density}
        return result

    @routes.post(
        "/v1/sessions/{session_id}/actions/{action_id}/approve",
        **route(ApprovalResponse, "approve"),
    )
    async def approve(
        session_id: UUID,
        action_id: Identifier,
        body: Decision,
        request: Request,
        user: UUID = user_dependency,
    ):
        return await operate(
            request,
            user,
            "approve",
            session_id,
            {**body.model_dump(mode="json"), "action_id": action_id},
        )

    @routes.post(
        "/v1/sessions/{session_id}/actions/{action_id}/reject",
        **route(ApprovalResponse, "reject"),
    )
    async def reject(
        session_id: UUID,
        action_id: Identifier,
        body: Decision,
        request: Request,
        user: UUID = user_dependency,
    ):
        return await operate(
            request,
            user,
            "reject",
            session_id,
            {**body.model_dump(mode="json"), "action_id": action_id},
        )

    @routes.get("/v1/sessions/{session_id}/events", **route(EventList, "events"))
    async def events(
        session_id: UUID,
        request: Request,
        after: str = Query(default="", pattern=r"^\d{0,19}$"),
        user: UUID = user_dependency,
    ):
        if int(after or "0") > 9223372036854775807:
            raise HTTPException(
                422, {"code": "INVALID_PAYLOAD", "message": "Cursor is out of range"}
            )
        return await operate(request, user, "events", session_id, {"after": after})

    @routes.post("/v1/sessions/{session_id}/events/{event_id}/ack", **route(EventAck, "ack"))
    async def ack(session_id: UUID, event_id: UUID, request: Request, user: UUID = user_dependency):
        return await operate(request, user, "ack", session_id, {"event_id": str(event_id)})

    @routes.post(
        "/v1/sessions/{session_id}/actions/{action_id}/result",
        **route(ResultResponse, "result"),
    )
    async def result(
        session_id: UUID,
        action_id: Identifier,
        body: ActionResult,
        request: Request,
        user: UUID = user_dependency,
    ):
        # schema_version is accepted but not stored, so pre-1.0-field retries stay idempotent.
        payload = body.model_dump(mode="json", exclude={"schema_version"})
        return await operate(request, user, "result", session_id, {**payload, "action_id": action_id})

    return routes


CODES = {
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "INVALID_PAYLOAD",
    429: "RATE_LIMITED",
    503: "BACKEND_UNAVAILABLE",
}


def error(status, code, message, request_id=None, headers=None):
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
        headers=headers,
    )


def install_workflow(app, current_user):
    def shared(request):
        return request.url.path.startswith(("/v1/sessions", "/v1/devices"))

    @app.exception_handler(StarletteHTTPException)
    async def contract_error(request, exc):
        if not shared(request):
            return await http_exception_handler(request, exc)
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        fallback = "BACKEND_UNAVAILABLE" if exc.status_code >= 500 else "INVALID_PAYLOAD"
        return error(
            exc.status_code,
            detail.get("code", CODES.get(exc.status_code, fallback)),
            detail.get("message", "Request failed"),
            getattr(request.state, "workflow_request_id", None),
            exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_payload(request, exc):
        if not shared(request):
            return await request_validation_exception_handler(request, exc)
        if any(tuple(e.get("loc", ()))[:2] == ("path", "session_id") for e in exc.errors()):
            # Session IDs are UUIDs; anything else cannot name an existing session.
            return error(404, "SESSION_NOT_FOUND", "Session not found")
        return error(
            422,
            "INVALID_PAYLOAD",
            "Request does not match the v1.0 schema",
            exc.body.get("request_id") if isinstance(exc.body, dict) else None,
        )

    app.include_router(router(current_user))
