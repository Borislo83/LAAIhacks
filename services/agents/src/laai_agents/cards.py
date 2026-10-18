"""ASI:One Interactive Cards: build card messages and read the selections that come back.

A card is a ChatMessage with narration (TextContent) plus one MetadataContent whose
values are all strings; the payload itself is JSON-encoded. ASI:One returns a click as
JSON (direct @mention) or as planner prose that still contains every selection value,
so every selection here carries a distinctive `focal_action` token.
"""

import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    TextContent,
)

ACTIONS = (
    "focal_submit_brief",
    "focal_buy_main",
    "focal_buy_alt",
    "focal_pick_listing",
    "focal_approve_cart",
    "focal_reject_cart",
    "focal_send_message",
    "focal_skip_message",
    "focal_start_watch",
    "focal_finish",
)
PRIORITIES = ("balanced", "price", "quality")


def text(body: str, end_session: bool = False) -> ChatMessage:
    content = [TextContent(type="text", text=body)]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=datetime.now(UTC), msg_id=uuid4(), content=content)


def card(
    narration: str, kind: str, payload: dict, terminal: bool = False, end_session: bool = False
) -> ChatMessage:
    metadata = {
        "card_protocol_version": "1",
        # Always "true": ASI:One only renders cards that set it; is_terminal disables input.
        "requires_card_interaction": "true",
        "card_kind": kind,
        "card_payload": json.dumps(payload),
        "preferred_drawer_width_px": "560",
    }
    if terminal:
        metadata["is_terminal"] = "true"
    return ChatMessage(
        timestamp=datetime.now(UTC),
        msg_id=uuid4(),
        content=[
            TextContent(type="text", text=narration),
            MetadataContent(type="metadata", metadata=metadata),
            *([EndSessionContent(type="end-session")] if end_session else []),
        ],
    )


def selection(action: str, **extra) -> dict:
    return {"focal_action": action, **extra}


def parse_reply(body: str) -> dict:
    """Card selections as a dict: JSON when ASI:One sends it, else pulled from prose."""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    found = {}
    lowered = body.lower()
    for action in ACTIONS:
        if action in lowered:
            found["focal_action"] = action
            break
    if url := re.search(r"https://(?:www\.)?amazon\.com/dp/[A-Z0-9]{10}", body, re.IGNORECASE):
        found["url"] = url[0]
    if budget := re.search(r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)", body):
        found["budget"] = budget[1].replace(",", "")
    for priority in PRIORITIES:
        if re.search(rf"\b{priority}\b", lowered):
            found["priority"] = priority
            break
    return found


def budget_in(body: str) -> str | None:
    """A budget from planner prose: "budget: 350", "$350", or a lone number (not "WH-1000XM5")."""
    for pattern in (
        r"budget\D{0,20}?(\d[\d,]*(?:\.\d+)?)",
        r"\$\s*(\d[\d,]*(?:\.\d+)?)",
        r"(?<![\w.-])(\d[\d,]*(?:\.\d+)?)(?![\w-])",
    ):
        for match in re.finditer(pattern, body, re.IGNORECASE):
            value = match[1].replace(",", "")
            if float(value) >= 5:
                return value
    return None


def money(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "n/a"


def code_block(data, limit: int = 900) -> str:
    rendered = json.dumps(data, indent=2, default=str)
    if len(rendered) > limit:
        rendered = rendered[:limit].rstrip() + "\n  …"
    return f"```json\n{rendered}\n```"


def brief_form(product: str, priority: str) -> ChatMessage:
    return card(
        f"Before I dispatch the team on **{product}**, two quick questions so the agents "
        "search with the right constraints.",
        "form",
        {
            "title": f"Shopping brief · {product}",
            "fields": [
                {"kind": "number", "name": "budget", "label": "Budget (USD)", "required": True},
                {
                    "kind": "select",
                    "name": "priority",
                    "label": "What matters most?",
                    "required": True,
                    "options": [
                        {"value": "balanced", "label": "Balanced"},
                        {"value": "price", "label": "Lowest price"},
                        {"value": "quality", "label": "Best quality"},
                    ],
                },
                {"kind": "text", "name": "use_case", "label": "What is it for? (optional)"},
            ],
            "submit_cta": {
                "label": "Dispatch agents",
                "selection": selection("focal_submit_brief", default_priority=priority),
            },
        },
    )
