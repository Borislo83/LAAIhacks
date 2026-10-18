"""Per-user preferences and private notes; never an unscoped process-global memory."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from .learning import LearnedPriority, effective_priority, learned_priority
from .models import InputModel, TaskKind


class Preferences(InputModel):
    display_density: Literal["comfortable", "compact"] = "comfortable"
    shopping_priority: Literal["balanced", "price", "quality"] = "balanced"
    proactive_enabled: bool = False
    notes: str = Field(default="", max_length=10000)


class SavedPreferences(Preferences):
    updated_at: datetime
    # Decisions before this reset are ignored when learning a priority (migration 006).
    learned_since: datetime | None = None


async def read_preferences(
    request: Request, user: UUID, replacement: Preferences | None = None
) -> SavedPreferences:
    """Read (initializing defaults once) or, with a replacement, save the user's record."""
    row = await request.app.state.store.request(
        "POST",
        "rpc/laai_preferences",
        json={
            "p_user_id": str(user),
            "p_preferences": replacement.model_dump(mode="json") if replacement else None,
        },
    )
    return SavedPreferences.model_validate(row)


async def learned_for(request: Request, user: UUID, saved: SavedPreferences) -> LearnedPriority:
    learned = await learned_priority(request.app.state.store, user, saved.learned_since)
    learned.effective, learned.source = effective_priority(
        saved.shopping_priority, learned.priority
    )
    return learned


async def effective_preferences(request: Request, user: UUID) -> SavedPreferences:
    """Saved preferences with the learned priority filled in where the user left "balanced"."""
    saved = await read_preferences(request, user)
    if saved.shopping_priority != "balanced":
        return saved  # The explicit setting wins; no need to read history.
    learned = await learned_for(request, user, saved)
    return saved.model_copy(update={"shopping_priority": learned.effective})


def preferred_tasks(tasks, priority):
    order = {
        "price": [TaskKind.PRICE, TaskKind.ALTERNATIVES, TaskKind.REVIEWS],
        "quality": [TaskKind.REVIEWS, TaskKind.ALTERNATIVES, TaskKind.PRICE],
        "balanced": list(TaskKind),
    }[priority]
    return sorted(tasks, key=order.index)


def preference_context(preferences):
    # Personal notes remain private storage, not instructions sent to models or agents.
    return (
        "\nSaved shopping priority: "
        + preferences.shopping_priority
        + (
            ". Prefer price and value when choosing recommendations."
            if preferences.shopping_priority == "price"
            else ". Prefer quality and independent review evidence when choosing recommendations."
            if preferences.shopping_priority == "quality"
            else ". Balance price and quality."
        )
        + " The current user request and explicit budget take precedence."
    )


def router(current_user):
    routes = APIRouter()
    user_dependency = Depends(current_user)

    @routes.get("/v1/preferences", response_model=SavedPreferences)
    async def get_preferences(request: Request, user: UUID = user_dependency):
        return await read_preferences(request, user)

    @routes.put("/v1/preferences", response_model=SavedPreferences)
    async def put_preferences(body: Preferences, request: Request, user: UUID = user_dependency):
        return await read_preferences(request, user, body)

    @routes.get("/v1/preferences/learned", response_model=LearnedPriority)
    async def get_learned(request: Request, user: UUID = user_dependency):
        return await learned_for(request, user, await read_preferences(request, user))

    @routes.post("/v1/preferences/learned/reset", response_model=LearnedPriority)
    async def reset_learned(request: Request, user: UUID = user_dependency):
        await request.app.state.store.request(
            "POST", "rpc/laai_reset_learned_priority", json={"p_user_id": str(user)}
        )
        return await learned_for(request, user, await read_preferences(request, user))

    return routes
