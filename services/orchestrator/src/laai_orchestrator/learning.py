"""Learned shopping priority: a small, explainable guess from the user's own approve/skip history.

Only task and action decisions are read, never private notes. The explicit setting always
wins; the learned value applies only while the user leaves shopping_priority on "balanced".
"""

from datetime import UTC, datetime, timedelta
from typing import Literal, NamedTuple
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel

Lean = Literal["price", "quality"]
Priority = Literal["balanced", "price", "quality"]

WINDOW = 10  # Only the most recent decisions count, so the guess follows changing habits.
MIN_DECISIONS = 5
MIN_SHARE = 0.7  # e.g. 4 of 5, or 7 of 10.
# A suggested task nobody dispatched for this long counts as skipped.
STALE_AFTER = timedelta(minutes=30)

TASK_LEAN: dict[str, Lean] = {"price": "price", "reviews": "quality"}
ACTION_LEAN: dict[str, Lean] = {"compare_prices": "price", "find_reviews": "quality"}
APPROVED_TASK = {"running", "succeeded", "failed"}
APPROVED_ACTION = {"approved", "running", "completed", "failed"}
NOUN = {"price": "price check", "quality": "review lookup"}


class Decision(NamedTuple):
    lean: Lean
    approved: bool
    at: datetime


class LearnedPriority(BaseModel):
    priority: Priority
    reason: str
    decisions: int
    since: datetime | None = None
    # What Focal actually uses, after the explicit setting has had its say.
    effective: Priority = "balanced"
    source: Literal["setting", "learned", "default"] = "default"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def infer(decisions: list[Decision]) -> LearnedPriority:
    """Approving a lean, or skipping its opposite, is one vote for that lean."""
    recent = sorted(decisions, key=lambda d: d.at, reverse=True)[:WINDOW]
    if len(recent) < MIN_DECISIONS:
        return LearnedPriority(
            priority="balanced",
            decisions=len(recent),
            reason=f"Not enough history yet: {len(recent)} of the {MIN_DECISIONS} "
            "approve or skip choices needed.",
        )
    other: dict[Lean, Lean] = {"price": "quality", "quality": "price"}
    votes = {lean: 0 for lean in other}
    for d in recent:
        votes[d.lean if d.approved else other[d.lean]] += 1
    top: Lean = max(votes, key=votes.__getitem__)
    if votes[top] / len(recent) < MIN_SHARE:
        return LearnedPriority(
            priority="balanced",
            decisions=len(recent),
            reason=f"Your last {len(recent)} choices were mixed "
            f"({votes['price']} favoured price, {votes['quality']} quality).",
        )
    approved = sum(d.approved and d.lean == top for d in recent)
    skipped = sum(not d.approved and d.lean == other[top] for d in recent)
    parts = [f"approved {_plural(approved, NOUN[top])}"] if approved else []
    if skipped:
        parts.append(f"skipped {_plural(skipped, NOUN[other[top]])}")
    return LearnedPriority(
        priority=top,
        decisions=len(recent),
        reason=f"{votes[top]} of your last {len(recent)} choices favoured {top}: "
        f"you {' and '.join(parts)}.",
    )


def decisions_from_rows(
    tasks: list[dict], actions: list[dict], now: datetime, since: datetime | None = None
) -> list[Decision]:
    found = []
    for row in tasks:
        lean, at = TASK_LEAN.get(row.get("kind")), _time(row.get("created_at"))
        if not lean or not at:
            continue
        if row.get("status") in APPROVED_TASK:
            found.append(Decision(lean, True, at))
        elif row.get("status") == "predicted" and now - at >= STALE_AFTER:
            found.append(Decision(lean, False, at))
    for row in actions:
        lean, at = ACTION_LEAN.get(row.get("type")), _time(row.get("proposed_at"))
        if not lean or not at:
            continue
        if row.get("status") in APPROVED_ACTION:
            found.append(Decision(lean, True, at))
        elif row.get("status") == "rejected":
            found.append(Decision(lean, False, at))
    return [d for d in found if since is None or d.at > since]


def _time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def effective_priority(explicit: Priority, learned: Priority) -> tuple[Priority, str]:
    """The user's explicit setting always wins; learning only fills in "balanced"."""
    if explicit != "balanced":
        return explicit, "setting"
    if learned != "balanced":
        return learned, "learned"
    return "balanced", "default"


async def learned_priority(store, user: UUID, since: datetime | None = None) -> LearnedPriority:
    """Read recent decisions; a history outage leaves Focal balanced rather than failing."""
    window = {"limit": WINDOW * 3}
    created = {"created_at": f"gt.{since.isoformat()}"} if since else {}
    proposed = {"proposed_at": f"gt.{since.isoformat()}"} if since else {}
    try:
        tasks = await store.request(
            "GET",
            "laai_tasks",
            params={
                "select": "kind,status,created_at",
                "user_id": f"eq.{user}",
                "kind": "in.(price,reviews)",
                "order": "created_at.desc",
                **created,
                **window,
            },
        )
        actions = await store.request(
            "GET",
            "laai_actions",
            params={
                "select": "type,status,proposed_at,laai_sessions!inner(user_id)",
                "laai_sessions.user_id": f"eq.{user}",
                "type": "in.(compare_prices,find_reviews)",
                "status": "in.(approved,running,completed,failed,rejected)",
                "order": "proposed_at.desc",
                **proposed,
                **window,
            },
        )
    except HTTPException:
        return LearnedPriority(
            priority="balanced", decisions=0, since=since,
            reason="Your history is unavailable right now, so Focal stays balanced.",
        )
    learned = infer(decisions_from_rows(tasks, actions, datetime.now(UTC), since))
    learned.since = since
    return learned
