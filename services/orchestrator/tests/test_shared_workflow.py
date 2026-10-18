import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_orchestrator import USER, config

from laai_orchestrator import main
from laai_orchestrator.main import create_app

SESSION = str(uuid4())
PREFERENCES = {
    "display_density": "compact",
    "shopping_priority": "balanced",
    "proactive_enabled": False,
    "notes": "",
    "updated_at": "2026-10-17T00:00:00Z",
}


def rpc_response(call):
    """Realistic laai_workflow response shapes, keyed by operation."""
    operation, payload = call["p_operation"], call["p_payload"]
    ref = {"schema_version": "1.0", "session_id": SESSION, "revision": 1}
    if operation == "device":
        return {"device_id": payload.get("device_id", str(uuid4())), "last_seen_at": "2026-10-17T00:00:00Z"}
    if operation in ("approve", "reject"):
        event_id = str(uuid4()) if operation == "approve" else None
        status = "approved" if operation == "approve" else "rejected"
        return {"action_id": payload["action_id"], "status": status, "event_id": event_id}
    if operation == "result":
        return {"action_id": payload["action_id"], "status": payload["status"]}
    if operation == "ack":
        return {"event_id": payload["event_id"], "cursor": "1".zfill(19), "consumed_at": None}
    if operation == "events":
        return {"events": []}
    if operation == "sessions":
        return {"sessions": []}
    return ref


def client_for(roles=None, result=None, user_metadata=None, preferences=PREFERENCES):
    calls, auth = [], []

    def handle(request):
        if request.url.path == "/auth/v1/user":
            auth.append(request.headers.get("authorization"))
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
            calls.append(json.loads(request.content))
            return httpx.Response(200, json=result or rpc_response(calls[-1]))
        if request.url.path == "/rest/v1/rpc/laai_preferences":
            if preferences is None:
                return httpx.Response(500, json={"message": "private storage details"})
            return httpx.Response(200, json=preferences)
        raise AssertionError(str(request.url))

    app = create_app(config(), httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    app.state.auth_calls = auth
    return TestClient(app), calls


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


def test_executor_acknowledges_events_and_g2_registers_devices():
    event_id, device_id = str(uuid4()), str(uuid4())
    client, calls = client_for(["executor", "g2", "web"])
    with client:
        client.headers["Authorization"] = "Bearer valid"
        assert client.post(f"/v1/sessions/{SESSION}/events/{event_id}/ack").status_code == 200
        assert calls[-1]["p_operation"] == "ack"
        assert calls[-1]["p_payload"] == {"event_id": event_id}
        assert client.post(f"/v1/sessions/{SESSION}/events/junk/ack").status_code == 422
        assert client.post("/v1/devices").status_code == 201
        assert calls[-1]["p_operation"] == "device" and calls[-1]["p_payload"] == {}
        client.post("/v1/devices", json={"device_id": device_id})
        assert calls[-1]["p_payload"] == {"device_id": device_id}
        assert client.post("/v1/sessions", json={"device_id": device_id}).status_code == 201
        assert calls[-1]["p_operation"] == "create"
        assert calls[-1]["p_payload"] == {"device_id": device_id}
        response = client.post("/v1/devices", json={"device_id": "nope"})
        assert response.json()["error"]["code"] == "INVALID_PAYLOAD"


def test_ack_and_device_registration_are_role_scoped():
    client, calls = client_for(["web"])
    with client:
        client.headers["Authorization"] = "Bearer valid"
        assert client.post(f"/v1/sessions/{SESSION}/events/{uuid4()}/ack").status_code == 403
        assert client.post("/v1/devices").json()["error"]["code"] == "FORBIDDEN"
        assert calls == []


def test_agents_and_executors_list_their_sessions_for_page_watching():
    listed = {
        "session_id": SESSION,
        "revision": 4,
        "active_page_id": "page-1",
        "updated_at": "2026-10-17T00:00:00Z",
        "expires_at": "2026-10-18T00:00:00Z",
    }
    client, calls = client_for(["agent"], {"sessions": [listed]})
    with client:
        client.headers["Authorization"] = "Bearer valid"
        response = client.get("/v1/sessions")
        assert response.status_code == 200
        assert response.json()["sessions"][0]["revision"] == 4
        assert calls[-1]["p_operation"] == "sessions"
        assert calls[-1]["p_session_id"] is None and calls[-1]["p_user_id"] == str(USER)
    client, calls = client_for(["executor"])
    with client:
        client.headers["Authorization"] = "Bearer valid"
        assert client.get("/v1/sessions").json() == {"sessions": []}
    for roles in (["web"], ["g2"]):
        client, calls = client_for(roles)
        with client:
            client.headers["Authorization"] = "Bearer valid"
            assert client.get("/v1/sessions").json()["error"]["code"] == "FORBIDDEN"
            assert calls == []


def demo_client(host="127.0.0.1", **overrides):
    calls = []

    def handle(request):
        if request.url.path == "/rest/v1/rpc/laai_workflow":
            calls.append(json.loads(request.content))
            return httpx.Response(200, json=rpc_response(calls[-1]))
        # Demo tokens must never reach Supabase Auth.
        raise AssertionError(str(request.url))

    app = create_app(
        config(demo_auth_mode=True, demo_user_id=USER, **overrides),
        httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    return TestClient(app, client=(host, 50000)), calls


def test_demo_auth_maps_fixture_tokens_to_roles_on_loopback_only():
    client, calls = demo_client()
    with client:
        web = {"Authorization": "Bearer demo-web"}
        assert client.post("/v1/sessions", headers=web).status_code == 201
        assert calls[-1]["p_user_id"] == str(USER)
        approve = client.post(
            f"/v1/sessions/{SESSION}/actions/a1/approve",
            headers=web,
            json={"request_id": "tap-1", "expected_revision": 1},
        )
        assert approve.json()["error"]["code"] == "FORBIDDEN"
        approve = client.post(
            f"/v1/sessions/{SESSION}/actions/a1/approve",
            headers={"Authorization": "Bearer demo-g2"},
            json={"request_id": "tap-1", "expected_revision": 1},
        )
        assert approve.status_code == 200
        proxied = client.post(
            "/v1/sessions", headers={**web, "X-Forwarded-For": "203.0.113.9"}
        )
        assert proxied.json()["error"]["code"] == "UNAUTHORIZED"
        tunneled = client.post("/v1/sessions", headers={**web, "CF-Connecting-IP": "203.0.113.9"})
        assert tunneled.status_code == 401
    remote, calls = demo_client(host="203.0.113.9")
    with remote:
        response = remote.post("/v1/sessions", headers={"Authorization": "Bearer demo-all"})
        assert response.status_code == 401 and calls == []


def test_demo_tokens_are_rejected_when_demo_mode_is_off():
    client, calls = client_for()
    with client:
        response = client.post("/v1/sessions", headers={"Authorization": "Bearer demo-all"})
        assert response.status_code == 401 and calls == []


def test_demo_mode_requires_a_real_auth_user():
    with pytest.raises(ValueError):
        config(demo_auth_mode=True)


DISPLAY = {
    "schema_version": "1.0",
    "session_id": SESSION,
    "revision": 8,
    "page": {
        "page_id": "p1",
        "title": "Headphones",
        "kind": "product",
        "summary": ["ANC"],
        "facts": [{"label": "Price", "value": "$299.99 (demo)"}],
        "source_label": "Example Store",
    },
    "actions": [
        {
            "action_id": "a1",
            "label": "Compare",
            "type": "compare_prices",
            "description": "Check",
            "status": "completed",
            "requires_approval": True,
            "expires_at": "2099-01-01T00:00:00Z",
            "result": {"status": "completed", "message": "Done", "request_id": "r1"},
        }
    ],
    "notice": None,
    "mode": "result",
}


def test_display_keeps_db_extension_fields_and_adds_display_preferences():
    client, _ = client_for(["g2"], DISPLAY)
    with client:
        client.headers["Authorization"] = "Bearer valid"
        body = client.get(f"/v1/sessions/{SESSION}/display").json()
    assert body == {**DISPLAY, "preferences": {"display_density": "compact"}}


def test_display_omits_preferences_when_they_cannot_be_read():
    client, _ = client_for(["g2"], DISPLAY, preferences=None)
    with client:
        client.headers["Authorization"] = "Bearer valid"
        response = client.get(f"/v1/sessions/{SESSION}/display")
    assert response.status_code == 200
    assert response.json() == DISPLAY


def test_events_keep_consumed_at_and_action_status():
    event = {
        "event_id": str(uuid4()),
        "cursor": "1".zfill(19),
        "type": "action.approved",
        "session_id": SESSION,
        "action_id": "a1",
        "page_id": "p1",
        "occurred_at": "2026-10-17T20:00:15Z",
        "consumed_at": None,
        "action_status": "approved",
    }
    client, _ = client_for(["executor"], {"events": [event]})
    with client:
        client.headers["Authorization"] = "Bearer valid"
        assert client.get(f"/v1/sessions/{SESSION}/events").json() == {"events": [event]}


def test_result_accepts_schema_version_without_changing_the_stored_payload():
    client, calls = client_for(["executor"])
    with client:
        client.headers["Authorization"] = "Bearer valid"
        body = {"request_id": "r1", "status": "completed", "message": "Done"}
        response = client.post(
            f"/v1/sessions/{SESSION}/actions/a1/result", json={"schema_version": "1.0", **body}
        )
        assert response.status_code == 200
        assert response.json() == {"action_id": "a1", "status": "completed"}
        assert calls[-1]["p_payload"] == {**body, "result_url": None, "action_id": "a1"}
        bad = client.post(
            f"/v1/sessions/{SESSION}/actions/a1/result", json={"schema_version": "2.0", **body}
        )
        assert bad.json()["error"]["code"] == "INVALID_PAYLOAD"


def test_reject_and_approve_return_typed_decisions():
    client, _ = client_for(["g2"])
    with client:
        client.headers["Authorization"] = "Bearer valid"
        body = {"request_id": "tap-1", "expected_revision": 1}
        rejected = client.post(f"/v1/sessions/{SESSION}/actions/a1/reject", json=body).json()
        assert rejected == {"action_id": "a1", "status": "rejected", "event_id": None}
        approved = client.post(f"/v1/sessions/{SESSION}/actions/a1/approve", json=body).json()
        assert approved["status"] == "approved" and approved["event_id"]


def test_routing_errors_use_generic_codes_and_bad_session_ids_are_not_found():
    client, calls = client_for(["g2", "web"])
    with client:
        client.headers["Authorization"] = "Bearer valid"
        missing = client.get(f"/v1/sessions/{SESSION}/nope")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"
        method = client.delete(f"/v1/sessions/{SESSION}/display")
        assert method.status_code == 405
        assert method.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
        bad_id = client.get("/v1/sessions/not-a-uuid/display")
        assert bad_id.status_code == 404
        assert bad_id.json()["error"]["code"] == "SESSION_NOT_FOUND"
        assert calls == []


def test_openapi_declares_typed_responses_and_error_envelopes():
    client, _ = client_for()
    schema = client.app.openapi()
    display = schema["paths"]["/v1/sessions/{session_id}/display"]["get"]
    assert display["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DisplayState"
    }
    assert "204" in display["responses"]
    for status in ("401", "403", "404", "409", "422", "429", "503"):
        ref = display["responses"][status]["content"]["application/json"]["schema"]["$ref"]
        assert ref == "#/components/schemas/ErrorEnvelope"
    assert "g2" in display["description"]
    components = schema["components"]["schemas"]
    assert schema["paths"]["/v1/sessions"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/SessionList"}
    for name in ("SessionRef", "ApprovalResponse", "EventList", "Event", "ResultResponse"):
        assert name in components
    assert "schema_version" in components["ActionResult"]["properties"]


def test_verified_tokens_are_cached_briefly_and_failures_are_not(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(main, "monotonic", lambda: now[0])
    client, _ = client_for(["web"])
    auth = client.app.state.auth_calls
    with client:
        for _ in range(3):
            client.post("/v1/sessions", headers={"Authorization": "Bearer valid"})
        assert len(auth) == 1
        for _ in range(2):
            client.post("/v1/sessions", headers={"Authorization": "Bearer wrong"})
        assert len(auth) == 3
        now[0] += main.AUTH_CACHE_SECONDS + 1
        client.post("/v1/sessions", headers={"Authorization": "Bearer valid"})
        assert len(auth) == 4


def test_auth_cache_is_bounded():
    cache = main.AuthCache(max_entries=2)
    for token in ("a", "b", "c"):
        cache.put(token, USER, {"web"})
    assert cache.get("a") is None and cache.get("c") == (USER, {"web"})
