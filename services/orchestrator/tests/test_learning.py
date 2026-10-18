import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from test_orchestrator import config

from laai_orchestrator.learning import Decision, decisions_from_rows, effective_priority, infer
from laai_orchestrator.main import create_app

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def history(*choices):
    """("price", True) means a price check approved; newest first."""
    return [
        Decision(lean, approved, NOW - timedelta(minutes=i))
        for i, (lean, approved) in enumerate(choices)
    ]


def test_needs_minimum_evidence():
    learned = infer(history(*[("price", True)] * 4))
    assert learned.priority == "balanced" and learned.decisions == 4
    assert "4 of the 5" in learned.reason


def test_approving_price_checks_learns_price_with_a_reason():
    learned = infer(history(*[("price", True)] * 4, ("quality", True)))
    assert learned.priority == "price"
    assert learned.reason == (
        "4 of your last 5 choices favoured price: you approved 4 price checks."
    )


def test_skipping_the_opposite_counts_and_is_explained():
    learned = infer(history(*[("quality", True)] * 3, ("price", False), ("price", True)))
    assert learned.priority == "quality"
    assert learned.reason == (
        "4 of your last 5 choices favoured quality: "
        "you approved 3 review lookups and skipped 1 price check."
    )


def test_mixed_history_stays_balanced():
    learned = infer(history(*[("price", True), ("quality", True)] * 4))
    assert learned.priority == "balanced"
    assert "mixed (4 favoured price, 4 quality)" in learned.reason


def test_only_the_recent_window_counts():
    # Ten recent quality choices outweigh any amount of older price history.
    learned = infer(history(*[("quality", True)] * 10, *[("price", True)] * 20))
    assert learned.priority == "quality" and learned.decisions == 10


def test_explicit_setting_always_wins():
    assert effective_priority("quality", "price") == ("quality", "setting")
    assert effective_priority("price", "balanced") == ("price", "setting")
    assert effective_priority("balanced", "price") == ("price", "learned")
    assert effective_priority("balanced", "balanced") == ("balanced", "default")


def test_rows_become_decisions():
    old, fresh = (NOW - timedelta(hours=1)).isoformat(), (NOW - timedelta(minutes=5)).isoformat()
    tasks = [
        {"kind": "price", "status": "succeeded", "created_at": fresh},
        {"kind": "reviews", "status": "predicted", "created_at": old},  # skipped
        {"kind": "reviews", "status": "predicted", "created_at": fresh},  # still undecided
        {"kind": "alternatives", "status": "succeeded", "created_at": fresh},  # neutral
    ]
    actions = [
        {"type": "find_reviews", "status": "completed", "proposed_at": fresh},
        {"type": "compare_prices", "status": "rejected", "proposed_at": fresh},
        {"type": "open_url", "status": "approved", "proposed_at": fresh},
    ]
    found = decisions_from_rows(tasks, actions, NOW)
    assert sorted((d.lean, d.approved) for d in found) == [
        ("price", False), ("price", True), ("quality", False), ("quality", True)
    ]
    # A reset ignores decisions made before it.
    assert decisions_from_rows(tasks, actions, NOW, since=NOW - timedelta(minutes=30)) == [
        d for d in found if d.at > NOW - timedelta(minutes=30)
    ]


def app_with(priority, notes="PRIVATE-NOTES"):
    user, seen = str(uuid4()), []
    stamp = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    prefs = {
        "display_density": "comfortable",
        "shopping_priority": priority,
        "proactive_enabled": True,
        "notes": notes,
        "updated_at": "2026-10-17T00:00:00Z",
    }

    def handle(request):
        seen.append(request)
        path = request.url.path
        if path == "/auth/v1/user":
            return httpx.Response(200, json={"id": user})
        if path == "/rest/v1/rpc/laai_preferences":
            return httpx.Response(200, json=prefs)
        if path == "/rest/v1/rpc/laai_reset_learned_priority":
            prefs["learned_since"] = datetime.now(UTC).isoformat()
            return httpx.Response(200, json=prefs)
        if path == "/rest/v1/laai_tasks":
            rows = [{"kind": "price", "status": "succeeded", "created_at": stamp}] * 5
            since = request.url.params.get("created_at")
            return httpx.Response(200, json=[] if since else rows)
        if path == "/rest/v1/laai_actions":
            return httpx.Response(200, json=[])
        if path == "/rest/v1/rpc/laai_create_context":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={"context_id": str(uuid4()), "prediction_source": body["p_source"], "tasks": []},
            )
        if path == "/rest/v1/rpc/laai_get_settings":
            return httpx.Response(
                200,
                json={
                    "enabled_agents": ["price", "alternatives", "reviews"],
                    "require_confirmation": True,
                    "projection_enabled": True,
                    "text_scale": 1,
                },
            )
        raise AssertionError(path)

    client = TestClient(create_app(config(), httpx.AsyncClient(transport=httpx.MockTransport(handle))))
    client.headers["Authorization"] = "Bearer token"
    return client, seen


def test_learned_endpoint_reports_reason_and_never_reads_notes():
    client, seen = app_with("balanced")
    with client:
        learned = client.get("/v1/preferences/learned").json()
    assert learned["priority"] == "price" and learned["effective"] == "price"
    assert learned["source"] == "learned"
    assert learned["reason"].startswith("5 of your last 5 choices favoured price")
    assert "PRIVATE-NOTES" not in json.dumps(learned)
    # History reads ask only for the columns learning needs.
    reads = [r for r in seen if r.url.path in ("/rest/v1/laai_tasks", "/rest/v1/laai_actions")]
    assert reads and all("notes" not in str(r.url) for r in reads)


def test_explicit_setting_is_reported_as_winning():
    client, _ = app_with("quality")
    with client:
        learned = client.get("/v1/preferences/learned").json()
    assert learned["priority"] == "price"
    assert (learned["effective"], learned["source"]) == ("quality", "setting")


def test_reset_ignores_earlier_history():
    client, _ = app_with("balanced")
    with client:
        learned = client.post("/v1/preferences/learned/reset").json()
    assert learned["priority"] == "balanced" and learned["since"]
    assert "Not enough history" in learned["reason"]


def test_learned_priority_reorders_suggested_tasks_only_when_balanced():
    page = {"url": "https://example.com/p", "product": "Sony XM5"}
    for setting, first in (("balanced", "price"), ("quality", "reviews")):
        client, seen = app_with(setting)
        with client:
            assert client.post("/v1/contexts", json=page).status_code == 201
        body = next(
            json.loads(r.content) for r in seen if r.url.path == "/rest/v1/rpc/laai_create_context"
        )
        assert body["p_tasks"][0]["kind"] == first
        # An explicit setting never needs a history read.
        assert any(r.url.path == "/rest/v1/laai_tasks" for r in seen) == (setting == "balanced")
