from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from test_orchestrator import config

from laai_orchestrator.main import create_app

ALICE, BOB = str(uuid4()), str(uuid4())
DEFAULTS = {
    "display_density": "comfortable",
    "shopping_priority": "balanced",
    "proactive_enabled": False,
    "notes": "",
    "updated_at": "2026-10-17T00:00:00Z",
}


def test_preferences_notes_survive_application_restart_and_are_user_scoped():
    rows, calls = {}, []

    def handle(request):
        if request.url.path == "/auth/v1/user":
            identity = {"Bearer alice": ALICE, "Bearer bob": BOB}.get(
                request.headers.get("authorization")
            )
            return (
                httpx.Response(200, json={"id": identity})
                if identity
                else httpx.Response(401, json={})
            )
        import json

        body = json.loads(request.content)
        assert request.url.path == "/rest/v1/rpc/laai_preferences"
        uid = body["p_user_id"]
        rows.setdefault(uid, DEFAULTS.copy())
        if body["p_preferences"] is not None:
            rows[uid].update(body["p_preferences"])
        calls.append(uid)
        return httpx.Response(200, json=rows[uid])

    def app():
        return TestClient(
            create_app(config(), httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        )

    saved = {
        "display_density": "compact",
        "shopping_priority": "quality",
        "proactive_enabled": True,
        "notes": "Some random text\nUnicode: 香港 <script>alert(1)</script>",
    }
    with app() as client:
        assert client.get("/v1/preferences").status_code == 401
        assert client.put("/v1/preferences", json=saved).status_code == 401
        client.headers["Authorization"] = "Bearer alice"
        assert client.get("/v1/preferences").json() == DEFAULTS | {"learned_since": None}
        assert client.put("/v1/preferences", json=saved).json()["notes"] == saved["notes"]
        for bad in (
            {**saved, "user_id": BOB},
            {**saved, "notes": "a" * 10001},
            {**saved, "shopping_priority": "unknown"},
        ):
            assert client.put("/v1/preferences", json=bad).status_code == 422
    with app() as restarted:
        restarted.headers["Authorization"] = "Bearer alice"
        assert restarted.get("/v1/preferences").json()["notes"] == saved["notes"]
        restarted.headers["Authorization"] = "Bearer bob"
        assert restarted.get("/v1/preferences").json()["notes"] == ""
    assert calls.count(ALICE) == 3 and calls.count(BOB) == 1


async def test_proactive_and_priority_preferences_control_prediction():
    from laai_orchestrator.models import PageContext, TaskKind
    from laai_orchestrator.predictor import predict
    from laai_orchestrator.preferences import Preferences

    page = PageContext(url="https://example.com", product="Sony XM5")
    async with httpx.AsyncClient() as http:
        prediction, source = await predict(
            page, config(), http, Preferences(proactive_enabled=False)
        )
        assert prediction is None and source == "none"
        prediction, _ = await predict(
            page, config(), http, Preferences(proactive_enabled=True, shopping_priority="quality")
        )
        assert prediction.tasks[0] == TaskKind.REVIEWS


async def test_llm_receives_structured_priority_but_no_private_notes():
    from laai_orchestrator.chat import chat
    from laai_orchestrator.models import ChatMessage
    from laai_orchestrator.preferences import Preferences

    seen = []

    def handle(request):
        import json

        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hello"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await chat(
            [ChatMessage(role="user", content="Hello")],
            config(asi_one_api_key="key"),
            http,
            preferences=Preferences(shopping_priority="quality", notes="PRIVATE-NOTES-NEVER-SENT"),
        )
    import json

    assert "Saved shopping priority: quality" in seen[0]["messages"][0]["content"]
    assert "PRIVATE-NOTES-NEVER-SENT" not in json.dumps(seen)
