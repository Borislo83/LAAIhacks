"""Per-user preferences and private notes; never an unscoped process-global memory."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ConfigDict, Field

from .models import InputModel, TaskKind


class Preferences(InputModel):
    display_density: Literal["comfortable", "compact"] = "comfortable"
    shopping_priority: Literal["balanced", "price", "quality"] = "balanced"
    proactive_enabled: bool = False
    notes: str = Field(default="", max_length=10000)


class SavedPreferences(Preferences):
    # A database row, not client input: columns added later (e.g. learned_since) are
    # ignored instead of failing every request that reads preferences.
    model_config = ConfigDict(extra="ignore")

    updated_at: datetime


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

    return routes
