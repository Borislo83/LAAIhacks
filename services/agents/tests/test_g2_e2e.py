"""End-to-end G2 handshake: all four contract roles against the real orchestrator process.

The orchestrator runs unmodified (DEMO_AUTH_MODE fixture tokens from loopback). Its
Supabase is supabase/tests/postgrest_stub.mjs: embedded PostgreSQL running every
migration in supabase/migrations, so approvals, revisions, idempotency and the outbox
are the real SQL. The executor is integration.GlassesWorkflow with the real handlers in
PRODUCT_DATA_MODE=demo (catalog data, no network). The G2 side is laai_agents.g2_sim.

Set G2_E2E_TRANSCRIPT=<file> to save every G2 exchange as JSON.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiohttp
import pytest
from aiohttp import web

from laai_agents.g2_sim import LIST_ITEM_BYTES, G2Client, G2Error, render
from laai_agents.integration import GlassesWorkflow
from laai_agents.models import ConsiderRequest

ROOT = Path(__file__).resolve().parents[3]
STUB = ROOT / "supabase/tests/postgrest_stub.mjs"
ALICE = "00000000-0000-4000-8000-000000000001"
PRODUCT = "Sony WH-1000XM5"
TRANSCRIPT = []

pytestmark = pytest.mark.skipif(
    not shutil.which("node")
    or not shutil.which("uv")
    or not (ROOT / "supabase/tests/node_modules/@electric-sql/pglite").exists(),
    reason="needs node, uv and `npm ci` in supabase/tests",
)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, process, timeout=90):
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{process.args[0]} exited: {process.stdout.read()}")
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(url)


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    stub = subprocess.Popen(
        ["node", str(STUB), "0"], cwd=STUB.parent, stdout=subprocess.PIPE, text=True
    )
    port = int(stub.stdout.readline().split()[-1])
    api_port = free_port()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"VIRTUAL_ENV"} and not k.startswith("UV_")
    } | {
        "SUPABASE_URL": f"http://127.0.0.1:{port}",
        "SUPABASE_PUBLISHABLE_KEY": "stub",
        "SUPABASE_SECRET_KEY": "stub",
        "DEMO_AUTH_MODE": "true",
        "DEMO_USER_ID": ALICE,
        "ASI_ONE_API_KEY": "",
    }
    orchestrator = subprocess.Popen(
        [
            "uv",
            "run",
            "--quiet",
            "--project",
            str(ROOT / "services/orchestrator"),
            "python",
            "-c",
            (
                "import uvicorn, sys; from laai_orchestrator.main import create_app; "
                "uvicorn.run(create_app(), host='127.0.0.1', port=int(sys.argv[1]), "
                "log_level='warning')"
            ),
            str(api_port),
        ],
        # A cwd without .env, so only the settings above apply.
        cwd=tmp_path_factory.mktemp("orchestrator"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{api_port}"
    try:
        wait_http(url + "/healthz", orchestrator)
        yield url
    finally:
        orchestrator.terminate()
        stub.terminate()
        orchestrator.wait(10)
        stub.wait(10)
        if path := os.getenv("G2_E2E_TRANSCRIPT"):
            Path(path).write_text(json.dumps(TRANSCRIPT, indent=2))


@pytest.fixture(autouse=True)
def demo_catalog(monkeypatch):
    monkeypatch.setenv("PRODUCT_DATA_MODE", "demo")


async def raw(base_url, token, method, path, body=None, params=None):
    async with (
        aiohttp.ClientSession() as http,
        http.request(
            method,
            base_url + path,
            json=body,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        ) as response,
    ):
        text = await response.text()
        return response.status, json.loads(text) if text else None


def page_body(
    page_id="page-sony", title="Sony WH-1000XM5 Wireless Headphones", kind="product"
):
    return {
        "page_id": page_id,
        "url": "https://example.com/sony-wh-1000xm5",
        "kind": kind,
        "title": title,
        "summary": ["Active noise cancellation", "Up to 30 hours battery"],
        "facts": [{"label": "Price", "value": "$399.99 (demo)"}],
        "source_label": "Example Store",
        "updated_at": "2026-10-17T20:00:00Z",
    }


def executor(base_url, tmp_path, name="journal"):
    return GlassesWorkflow(
        base_url,
        {"web": "demo-web", "agent": "demo-agent", "executor": "demo-executor"},
        tmp_path / f"{name}.json",
    )


async def proposed_session(base_url, tmp_path, name="journal"):
    """Web creates a session and PUTs the page; the agent proposes both handlers."""
    worker = executor(base_url, tmp_path, name)
    session_id = await worker.propose(
        page_body(), ConsiderRequest(product=PRODUCT, max_price=300)
    )
    return worker, session_id


def by_type(display, kind):
    return next(a for a in display["actions"] if a["type"] == kind)


async def events(base_url, session_id, after=""):
    status, payload = await raw(
        base_url,
        "demo-executor",
        "GET",
        f"/v1/sessions/{session_id}/events",
        params={"after": after},
    )
    assert status == 200
    return payload["events"]


async def test_contract_handshake_steps_1_to_5(base_url, tmp_path):
    # Step 1: web creates a session and PUTs a page; G2 sees the revision increment.
    status, session = await raw(base_url, "demo-web", "POST", "/v1/sessions", {})
    assert status == 201 and set(session) == {
        "schema_version",
        "session_id",
        "revision",
    }
    session_id = session["session_id"]
    async with G2Client(base_url, "demo-g2", session_id, log=TRANSCRIPT) as g2:
        first = await g2.poll()
        assert (
            first["revision"] == 1
            and first["mode"] == "summary"
            and first["page"] is None
        )
        assert await g2.poll() is None  # 204 while unchanged
        status, ref = await raw(
            base_url,
            "demo-web",
            "PUT",
            f"/v1/sessions/{session_id}/page",
            {"schema_version": "1.0", "request_id": "g2-e2e-page-1", **page_body()},
        )
        assert status == 200 and ref["revision"] == 2
        shown = await g2.poll()
        assert shown["revision"] == 2 and shown["mode"] == "summary"
        assert render(shown).lines[0] == "Sony WH-1000XM5 Wireless Headphones"

        # Step 2: the agent PUTs compare_prices and find_reviews; G2 lists both.
        worker = executor(base_url, tmp_path)
        assert (
            await worker.propose(
                page_body(), ConsiderRequest(product=PRODUCT, max_price=300), session_id
            )
            == session_id
        )
        listed = await g2.wait_for(lambda d: d["mode"] == "actions")
        frame = render(listed)
        assert [label for _, label in frame.items] == [
            "Compare prices and alternatives",
            "Check independent reviews",
        ]
        assert all(len(label.encode()) <= LIST_ITEM_BYTES for _, label in frame.items)

        # Step 3: the user confirms on G2 against the revision it displayed.
        compare = by_type(listed, "compare_prices")
        assert render(listed, compare["action_id"]).mode == "confirm"
        approval = await g2.decide(compare["action_id"], request_id="g2-e2e-approve-1")
        assert set(approval) == {"action_id", "status", "event_id"}
        assert approval["status"] == "approved" and approval["event_id"]

        # Step 4: the executor consumes action.approved, reports running, then completed.
        seen_running = []
        real_execute = worker.execute

        async def execute(plan):
            seen_running.append(await g2.wait_for(lambda d: d["mode"] == "loading"))
            return await real_execute(plan)

        worker.execute = execute
        assert await worker.poll_once() == 1
        assert by_type(seen_running[0], "compare_prices")["status"] == "running"
        assert (
            render(seen_running[0]).lines[0]
            == "Working: Compare prices and alternatives"
        )

        # Step 5: G2 sees the completion in a revised display.
        done = await g2.wait_for(lambda d: d["mode"] == "result")
        result = by_type(done, "compare_prices")
        assert result["status"] == "completed"
        assert PRODUCT in result["result"]["message"]
        lines = render(done).lines
        assert lines[0] == "Done: Compare prices and alternatives"
        # The other option stays approvable from the result screen.
        assert [label for _, label in render(done).items] == [
            "Check independent reviews"
        ]
        [event] = await events(base_url, session_id)
        assert event["consumed_at"] and event["action_status"] == "completed"
        await worker.close()


async def test_payload_shapes_match_the_contract(base_url, tmp_path):
    worker, session_id = await proposed_session(base_url, tmp_path)
    await worker.close()
    async with G2Client(base_url, "demo-g2", session_id) as g2:
        display = await g2.poll()
        assert set(display) == {
            "schema_version",
            "session_id",
            "revision",
            "page",
            "actions",
            "mode",
            "notice",
            "preferences",
        }
        assert display["schema_version"] == "1.0" and display["notice"] is None
        assert {"page_id", "title", "kind", "summary", "facts"} <= set(display["page"])
        for action in display["actions"]:
            assert {
                "action_id",
                "label",
                "description",
                "status",
                "requires_approval",
            } <= set(action)
            assert (
                action["status"] == "proposed" and action["requires_approval"] is True
            )
        status, body = await raw(
            base_url,
            "demo-g2",
            "GET",
            f"/v1/sessions/{session_id}/display",
            params={"after_revision": str(display["revision"])},
        )
        assert status == 204 and body is None
        action_id = display["actions"][0]["action_id"]
        await g2.decide(action_id)
        [event] = await events(base_url, session_id)
        assert {
            "event_id",
            "cursor",
            "type",
            "session_id",
            "action_id",
            "page_id",
            "occurred_at",
        } <= set(event)
        assert event["type"] == "action.approved" and event["session_id"] == session_id
        # Cursors are fixed-width numeric strings, so they also sort lexically.
        assert event["cursor"].isdigit() and len(event["cursor"]) == 19
        assert await events(base_url, session_id, event["cursor"]) == []
    # Every error is the contract envelope and echoes the request_id.
    status, body = await raw(
        base_url,
        "demo-g2",
        "POST",
        f"/v1/sessions/{session_id}/actions/{action_id}/approve",
        {"schema_version": "1.0", "request_id": "g2-e2e-stale", "expected_revision": 1},
    )
    assert status == 409 and body == {
        "error": {
            "code": "STALE_REVISION",
            "message": "Display changed; refresh and confirm again.",
            "request_id": "g2-e2e-stale",
        }
    }
    for token, method, path, expected in [
        (
            "demo-web",
            "POST",
            f"/v1/sessions/{session_id}/actions/{action_id}/approve",
            "FORBIDDEN",
        ),
        ("nope", "GET", f"/v1/sessions/{session_id}/display", "UNAUTHORIZED"),
        ("demo-g2", "GET", f"/v1/sessions/{uuid4()}/display", "SESSION_NOT_FOUND"),
        (
            "demo-g2",
            "POST",
            f"/v1/sessions/{session_id}/actions/missing/approve",
            "ACTION_NOT_FOUND",
        ),
    ]:
        body = {
            "schema_version": "1.0",
            "request_id": "g2-e2e-err",
            "expected_revision": 1,
        }
        status, payload = await raw(
            base_url, token, method, path, body if method == "POST" else None
        )
        assert payload["error"]["code"] == expected, (path, payload)
        assert set(payload["error"]) == {"code", "message", "request_id"}
    status, payload = await raw(
        base_url,
        "demo-g2",
        "POST",
        f"/v1/sessions/{session_id}/actions/{action_id}/approve",
        {"schema_version": "2.0", "request_id": "g2-e2e-v2", "expected_revision": 1},
    )
    assert status == 422 and payload["error"]["code"] == "INVALID_PAYLOAD"


async def test_page_change_invalidates_old_actions(base_url, tmp_path):
    worker, session_id = await proposed_session(base_url, tmp_path)
    await worker.close()
    async with G2Client(base_url, "demo-g2", session_id) as g2:
        listed = await g2.wait_for(lambda d: d["mode"] == "actions")
        old = by_type(listed, "compare_prices")["action_id"]
        status, _ = await raw(
            base_url,
            "demo-web",
            "PUT",
            f"/v1/sessions/{session_id}/page",
            {
                "request_id": "g2-e2e-nav",
                **page_body("page-bose", "Bose QuietComfort Ultra"),
            },
        )
        assert status == 200
        # The glasses still show the old option; the tap must not approve it.
        assert await g2.decide(old) is None
        assert g2.notices == ["That option is no longer available."]
        assert g2.display["page"]["page_id"] == "page-bose"
        # Invalidated actions are left out of the display entirely.
        assert g2.display["actions"] == []
        assert render(g2.display).items == [] and g2.display["mode"] == "summary"
    assert await events(base_url, session_id) == []


async def test_stale_approval_refreshes_and_confirms_again(base_url, tmp_path):
    worker, session_id = await proposed_session(base_url, tmp_path)
    await worker.close()
    async with (
        G2Client(base_url, "demo-g2", session_id) as glasses,
        G2Client(base_url, "demo-g2", session_id) as phone,
    ):
        shown = await glasses.wait_for(lambda d: d["mode"] == "actions")
        await phone.poll()
        stale_revision = glasses.revision
        # Another G2 surface changes the display before this one submits.
        assert (
            await phone.decide(by_type(shown, "find_reviews")["action_id"], "reject")
        )["status"] == "rejected"
        confirmations = []
        approval = await glasses.decide(
            by_type(shown, "compare_prices")["action_id"],
            request_id="g2-e2e-stale-tap",
            confirm=lambda display: confirmations.append(display["revision"]) or True,
        )
        assert approval["status"] == "approved"
        assert confirmations == [stale_revision + 1]


async def test_declining_after_refresh_approves_nothing(base_url, tmp_path):
    worker, session_id = await proposed_session(base_url, tmp_path)
    await worker.close()
    async with (
        G2Client(base_url, "demo-g2", session_id) as glasses,
        G2Client(base_url, "demo-g2", session_id) as phone,
    ):
        shown = await glasses.wait_for(lambda d: d["mode"] == "actions")
        await phone.poll()
        await phone.decide(by_type(shown, "find_reviews")["action_id"], "reject")
        assert (
            await glasses.decide(
                by_type(shown, "compare_prices")["action_id"],
                confirm=lambda display: False,
            )
            is None
        )
        assert glasses.notices == ["Cancelled after the display changed."]
    assert await events(base_url, session_id) == []


async def test_duplicate_request_id_returns_the_same_result(base_url, tmp_path):
    worker, session_id = await proposed_session(base_url, tmp_path)
    await worker.close()
    async with G2Client(base_url, "demo-g2", session_id) as g2:
        shown = await g2.wait_for(lambda d: d["mode"] == "actions")
        action_id = by_type(shown, "compare_prices")["action_id"]
        body = {
            "schema_version": "1.0",
            "request_id": "g2-e2e-dup",
            "expected_revision": g2.revision,
        }
        path = f"/v1/sessions/{session_id}/actions/{action_id}/approve"
        first = await raw(base_url, "demo-g2", "POST", path, body)
        again = await raw(base_url, "demo-g2", "POST", path, body)
        assert first[0] == again[0] == 200 and first[1] == again[1]
        # Same key, different input: CONFLICT, never a second approval.
        changed = await raw(
            base_url, "demo-g2", "POST", path, {**body, "expected_revision": 99}
        )
        assert changed[1]["error"]["code"] == "CONFLICT"
    assert len(await events(base_url, session_id)) == 1


async def test_reject_path_emits_no_event(base_url, tmp_path):
    worker, session_id = await proposed_session(base_url, tmp_path)
    async with G2Client(base_url, "demo-g2", session_id) as g2:
        shown = await g2.wait_for(lambda d: d["mode"] == "actions")
        reviews = by_type(shown, "find_reviews")["action_id"]
        response = await g2.decide(reviews, "reject")
        assert response == {
            "action_id": reviews,
            "status": "rejected",
            "event_id": None,
        }
        after = await g2.poll()
        assert by_type(after, "find_reviews")["status"] == "rejected"
        assert [label for _, label in render(after).items] == [
            "Compare prices and alternatives"
        ]
        # A rejected action can no longer be approved: the API itself refuses it (the
        # simulator's decide() wouldn't even send it, as it is no longer listed).
        from laai_agents.g2_sim import G2Error

        try:
            await g2.call("POST", f"{g2.path}/actions/{reviews}/approve", {
                "schema_version": "1.0", "request_id": "g2-reapprove-rejected",
                "expected_revision": g2.revision,
            })
        except G2Error as exc:
            refused = exc
        else:
            refused = None
        assert refused is not None and refused.status in (404, 409, 410), refused
        await g2.refresh()
        assert by_type(g2.display, "find_reviews")["status"] == "rejected"
    assert await events(base_url, session_id) == []
    assert await worker.poll_once() == 0
    await worker.close()


async def test_expired_action_is_not_approved(base_url, tmp_path):
    status, session = await raw(base_url, "demo-web", "POST", "/v1/sessions", {})
    session_id = session["session_id"]
    path = f"/v1/sessions/{session_id}"
    await raw(
        base_url,
        "demo-web",
        "PUT",
        path + "/page",
        {"request_id": "g2-e2e-p", **page_body()},
    )
    expires = (datetime.now(UTC) + timedelta(seconds=1.5)).isoformat()
    status, _ = await raw(
        base_url,
        "demo-agent",
        "PUT",
        path + "/actions",
        {
            "request_id": "g2-e2e-a",
            "page_id": "page-sony",
            "actions": [
                {
                    "action_id": "g2-e2e-expiring",
                    "type": "find_reviews",
                    "label": "Reviews",
                    "description": "Short-lived",
                    "expires_at": expires,
                    "requires_approval": True,
                }
            ],
        },
    )
    assert status == 200
    async with G2Client(base_url, "demo-g2", session_id) as g2:
        await g2.wait_for(lambda d: d["mode"] == "actions")
        await asyncio.sleep(2)
        assert await g2.decide("g2-e2e-expiring") is None
        assert g2.notices == ["That option expired."]


async def test_executor_redelivery_after_crash_and_ack(base_url, tmp_path):
    worker, session_id = await proposed_session(base_url, tmp_path)
    async with G2Client(base_url, "demo-g2", session_id) as g2:
        shown = await g2.wait_for(lambda d: d["mode"] == "actions")
        await g2.decide(by_type(shown, "find_reviews")["action_id"])
        await g2.poll()
        await g2.decide(by_type(g2.display, "compare_prices")["action_id"])

        # Crash after the outcome is journaled but before the API accepted it.
        class Crash(Exception):
            pass

        real_post = worker.post_result

        async def crash_on_outcome(event, path, body):
            if body["status"] != "running":
                raise Crash
            return await real_post(event, path, body)

        worker.post_result = crash_on_outcome
        with pytest.raises(Crash):
            await worker.poll_once()
        assert worker.state["cursor"] == ""
        await worker.close()

        # A restarted executor gets the same event again (at-least-once) and finishes it
        # with the journaled request IDs, so the API sees one logical result.
        restarted = executor(base_url, tmp_path)
        assert await restarted.poll_once() == 2
        assert await restarted.poll_once() == 0
        await restarted.close()
        done = await g2.wait_for(
            lambda d: all(a["status"] == "completed" for a in d["actions"])
        )
        assert done["mode"] == "result"
        assert len(render(done).lines) <= 10

    # Reads never consume: the full feed replays, every event marked consumed.
    feed = await events(base_url, session_id)
    assert len(feed) == 2 and all(e["consumed_at"] for e in feed)
    # Explicit ack is idempotent and does not bump the display revision.
    async with G2Client(base_url, "demo-g2", session_id) as g2:
        before = (await g2.poll())["revision"]
        ack = f"/v1/sessions/{session_id}/events/{feed[0]['event_id']}/ack"
        first = await raw(base_url, "demo-executor", "POST", ack)
        assert first[0] == 200 and first == await raw(
            base_url, "demo-executor", "POST", ack
        )
        assert await g2.poll() is None and g2.revision == before


async def test_cross_session_action_ids_are_rejected(base_url, tmp_path):
    worker_a, session_a = await proposed_session(base_url, tmp_path, "a")
    worker_b, session_b = await proposed_session(base_url, tmp_path, "b")
    async with G2Client(base_url, "demo-g2", session_a) as g2:
        await g2.poll()
        foreign = next(iter(worker_b.state["actions"]))
        # The simulator refuses to send IDs it was not shown...
        with pytest.raises(ValueError):
            await g2.decide(foreign)
        # ...and the server rejects them anyway.
        status, body = await raw(
            base_url,
            "demo-g2",
            "POST",
            f"/v1/sessions/{session_a}/actions/{foreign}/approve",
            {"request_id": "g2-e2e-cross", "expected_revision": g2.revision},
        )
        assert status == 404 and body["error"]["code"] == "ACTION_NOT_FOUND"
    assert await events(base_url, session_b) == []

    # An executor never acts on an event from another session.
    async def other_session_events():
        return [
            {
                "event_id": str(uuid4()),
                "cursor": "1".zfill(19),
                "type": "action.approved",
                "session_id": session_b,
                "action_id": foreign,
                "page_id": "page-sony",
            }
        ]

    worker_a.fetch_events = other_session_events
    with pytest.raises(ValueError, match="Cross-session"):
        await worker_a.poll_once()
    await worker_a.close()
    await worker_b.close()


@pytest.fixture
async def flaky_proxy(base_url):
    """Fronts the API and injects failures: 429/503 before, or a lost response after, forwarding."""
    plan, seen = [], []

    async def forward(request):
        body = await request.read()
        seen.append((request.method, request.path, json.loads(body) if body else None))
        fault = plan.pop(0) if plan and request.method == "POST" else None
        if fault == "429":
            return web.json_response(
                {
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "Too many requests",
                        "request_id": None,
                    }
                },
                status=429,
                headers={"Retry-After": "1"},
            )
        if fault == "503":
            return web.json_response(
                {
                    "error": {
                        "code": "BACKEND_UNAVAILABLE",
                        "message": "Down",
                        "request_id": None,
                    }
                },
                status=503,
            )
        forwarded = {
            k: v
            for k, v in request.headers.items()
            if k.lower() in {"authorization", "content-type"}
        }
        async with (
            aiohttp.ClientSession() as http,
            http.request(
                request.method, base_url + request.path_qs, data=body, headers=forwarded
            ) as upstream,
        ):
            payload = await upstream.read()
            if fault == "lost":
                raise web.HTTPServiceUnavailable()  # applied upstream, reply lost
            return web.Response(
                status=upstream.status,
                body=payload,
                content_type=upstream.content_type if payload else None,
            )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", forward)
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    yield f"http://127.0.0.1:{port}", plan, seen
    await runner.cleanup()


async def test_rate_limit_and_backend_unavailable_retry_idempotently(
    base_url, tmp_path, flaky_proxy
):
    proxy, plan, seen = flaky_proxy
    worker, session_id = await proposed_session(base_url, tmp_path)
    await worker.close()
    async with G2Client(proxy, "demo-g2", session_id, backoff=0.05) as g2:
        shown = await g2.wait_for(lambda d: d["mode"] == "actions")
        plan.extend(["429", "503", "lost"])
        approval = await g2.decide(
            by_type(shown, "compare_prices")["action_id"], request_id="g2-e2e-retry"
        )
        assert approval["status"] == "approved"
        posts = [body for method, _, body in seen if method == "POST"]
        assert len(posts) == 4 and all(b["request_id"] == "g2-e2e-retry" for b in posts)
    [event] = await events(base_url, session_id)
    assert event["event_id"] == approval["event_id"]

    # A backend that stays down surfaces BACKEND_UNAVAILABLE instead of hanging or looping.
    async with G2Client(proxy, "demo-g2", session_id, retries=2, backoff=0.01) as g2:
        await g2.poll()
        plan.extend(["503"] * 3)
        with pytest.raises(G2Error) as failure:
            await g2.decide(by_type(g2.display, "find_reviews")["action_id"])
        assert failure.value.code == "BACKEND_UNAVAILABLE"
