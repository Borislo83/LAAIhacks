from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class TaskKind(StrEnum):
    PRICE = "price"
    ALTERNATIVES = "alternatives"
    REVIEWS = "reviews"


class AgentKind(StrEnum):
    """Every routable specialist. Only the shopping TaskKinds are persisted as tasks."""

    PRICE = "price"
    ALTERNATIVES = "alternatives"
    REVIEWS = "reviews"
    YOUTUBE = "youtube"


class UserSettings(InputModel):
    enabled_agents: list[TaskKind] = Field(default_factory=lambda: list(TaskKind), max_length=3)
    require_confirmation: bool = True
    projection_enabled: bool = True
    text_scale: float = Field(default=1, ge=0.5, le=3)


class PageContext(InputModel):
    url: HttpUrl
    title: str = Field(default="", max_length=500)
    text: str = Field(default="", max_length=16000)
    product: str | None = Field(default=None, min_length=1, max_length=200)
    current_store: str | None = Field(default=None, min_length=1, max_length=200)
    current_price: float | None = Field(default=None, ge=0)
    max_price: float | None = Field(default=None, ge=0)


class Prediction(InputModel):
    product: str = Field(min_length=1, max_length=200)
    tasks: list[TaskKind] = Field(max_length=3)


class Task(BaseModel):
    id: UUID
    context_id: UUID
    kind: TaskKind
    status: Literal["predicted", "running", "succeeded", "failed"]
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ContextResponse(BaseModel):
    context_id: UUID
    prediction_source: Literal["asi_one", "rules", "none"]
    tasks: list[Task]


class DispatchRequest(InputModel):
    confirmed: bool = False


# The arguments each specialist accepts; shared by chat tool calls and page predictions.
# The first field is required. TaskKind and AgentKind values compare equal as strings.
FIELDS = {
    AgentKind.PRICE: ("product", "current_store", "current_price"),
    AgentKind.ALTERNATIVES: ("product", "max_price"),
    AgentKind.REVIEWS: ("product",),
    AgentKind.YOUTUBE: ("query", "kind"),
}


def agent_payload(kind: TaskKind, product: str, page: PageContext) -> dict[str, Any]:
    return {f: product if f == "product" else getattr(page, f) for f in FIELDS[kind]}


class ChatMessage(InputModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(InputModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)
    purchase_confirmation: str | None = Field(default=None, min_length=32, max_length=64)


class BuyerPreparation(InputModel):
    product: str | None = Field(default=None, min_length=1, max_length=500)
    url: str | None = Field(default=None, max_length=2048)
    quantity: int = Field(default=1, ge=1, le=20)


class ToolArgs(BaseModel):
    """Agent arguments chosen by the LLM; validated before any agent call."""

    product: str | None = Field(default=None, min_length=1, max_length=200)
    current_store: str | None = Field(default=None, max_length=200)
    current_price: float | None = Field(default=None, ge=0)
    max_price: float | None = Field(default=None, ge=0)
    # YouTube agent arguments.
    query: str | None = Field(default=None, min_length=1, max_length=300)
    kind: Literal["song", "video", "auto"] = "auto"

    def suits(self, agent: "AgentKind") -> bool:
        """Whether the agent's required argument is present."""
        return getattr(self, FIELDS[agent][0]) is not None


class AgentCall(BaseModel):
    kind: AgentKind | Literal["buyer", "message"]
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    ms: int


class ChatReply(BaseModel):
    reply: str
    calls: list[AgentCall]
    # Best price / alternative / recommended, each with an Amazon or Best Buy cart link
    # when the product comes from one of those stores.
    picks: list[dict[str, Any]] = Field(default_factory=list)
    source: Literal["asi_one", "rules", "coordinator", "buyer"]


class MessageRequest(BaseModel):
    """message_friend tool arguments chosen by the LLM."""

    request: str = Field(min_length=1, max_length=1000)


class MessageSend(InputModel):
    """A draft the signed-in user reviewed and confirmed, exactly as drafted.

    The agent sends only the draft's own text, so an edit must be re-drafted first.
    """

    draft_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    # The messaging agent's own limit; longer text would come back as an agent error.
    text: str = Field(min_length=1, max_length=1000)


class MessageSent(BaseModel):
    status: Literal["sent", "failed", "needs_permission", "open_link"]
    summary: str = Field(max_length=2000)
    # True when the agent only simulated the send (local development).
    dry_run: bool = False
    # open_link: an sms: URL that opens Messages with the approved draft; the user sends it.
    open_url: str | None = Field(default=None, max_length=3000, pattern=r"^sms:\+?[0-9A-Za-z@._%+-]+\?&body=")
