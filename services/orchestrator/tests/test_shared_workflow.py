from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from test_orchestrator import USER, config

from laai_orchestrator.main import create_app

SESSION = str(uuid4())


def client_for(roles=None, result=None, user_metadata=None):
    calls = []

    def handle(request):
        if request.url.path == "/auth/v1/user":
            if request.headers.get("authorization") != "Bearer valid":
                return httpx.Response(401, json={})
            return httpx.Response(
                200,
                json={
                    "id": str(USER),
                    "app_metadata": {} if roles is None else {"laai_roles": roles},
                    "user_metadata": user_metadata or {},
                },
            )
        if request.url.path == "/rest/v1/rpc/laai_workflow":
            import json

            calls.append(json.loads(request.content))
            return httpx.Response(200, json=result or {"session_id": SESSION, "revision": 1})
        raise AssertionError(str(request.url))

    return TestClient(
        create_app(config(), httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    ), calls


def test_shared_errors_and_caller_roles_are_verified_server_side():
    client, calls = client_for(user_metadata={"laai_roles": ["g2", "executor", "agent"]})
    with client:
        assert client.post("/v1/sessions").json()["error"]["code"] == "UNAUTHORIZED"
        client.headers["Authorization"] = "Bearer valid"
        assert client.post("/v1/sessions").status_code == 201
        assert calls[-1]["p_user_id"] == str(USER)
        response = client.post(
            f"/v1/sessions/{SESSION}/actions/a1/approve",
            json={"request_id": "tap-1", "expected_revision": 1},
        )
        assert response.status_code == 403
        assert response.json()["error"] == {
            "code": "FORBIDDEN",
            "message": "Caller role cannot perform this operation",
            "request_id": "tap-1",
        }
        assert client.get(f"/v1/sessions/{SESSION}/events").status_code == 403
        assert len(calls) == 1


def test_g2_revision_approval_and_db_error_envelope():
    client, calls = client_for(
        ["g2"],
        {
            "error": {
                "code": "STALE_REVISION",
                "message": "Display changed; refresh and confirm again.",
            },
            "status": 409,
        },
    )
    with client:
        client.headers["Authorization"] = "Bearer valid"
        result = client.post(
            f"/v1/sessions/{SESSION}/actions/a1/approve",
            json={"schema_version": "1.0", "request_id": "tap-1", "expected_revision": 7},
        )
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "STALE_REVISION"
        assert calls[-1]["p_operation"] == "approve"
        assert calls[-1]["p_payload"]["expected_revision"] == 7
        assert client.post("/v1/sessions").status_code == 403


def test_display_204_and_executor_events():
    client, _ = client_for(["web"], {"unchanged": True})
    with client:
        client.headers["Authorization"] = "Bearer valid"
        response = client.get(f"/v1/sessions/{SESSION}/display?after_revision=7")
        assert response.status_code == 204 and not response.content
    client, calls = client_for(["executor"], {"events": []})
    with client:
        client.headers["Authorization"] = "Bearer valid"
        assert client.get(f"/v1/sessions/{SESSION}/events?after=000001").json() == {"events": []}
        assert calls[-1]["p_payload"] == {"after": "000001"}
        assert client.get(f"/v1/sessions/{SESSION}/events?after=invalid").status_code == 422


def test_action_and_url_validation_prevents_unapproved_operations():
    client, calls = client_for(["agent", "executor"])
    with client:
        client.headers["Authorization"] = "Bearer valid"
        action = {
            "action_id": "a1",
            "type": "compare_prices",
            "label": "Compare",
            "description": "Check",
            "expires_at": "2099-01-01T00:00:00Z",
            "requires_approval": False,
        }
        response = client.put(
            f"/v1/sessions/{SESSION}/actions",
            json={"request_id": "a-1", "page_id": "p1", "actions": [action]},
        )
        assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_PAYLOAD"
        action["requires_approval"] = True
        assert (
            client.put(
                f"/v1/sessions/{SESSION}/actions",
                json={"request_id": "a-1", "page_id": "p1", "actions": [action, action]},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/v1/sessions/{SESSION}/actions/a1/result",
                json={
                    "request_id": "result-1",
                    "status": "completed",
                    "message": "Done",
                    "result_url": "http://unsafe.example",
                },
            ).status_code
            == 422
        )
        assert calls == []
