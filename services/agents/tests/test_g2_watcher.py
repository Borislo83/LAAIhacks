"""`laai-glasses-executor watch` against the real orchestrator and migration SQL.

Same harness as test_g2_e2e (its own orchestrator and embedded Postgres, so other tests'
sessions do not show up in the watcher's session list): the web role PUTs a page, the
watcher proposes on its own, G2 approves, and the watcher's result reaches the display.
"""

import asyncio
import os
import sys
from datetime import timedelta

import aiohttp
import pytest
from test_g2_e2e import PRODUCT, base_url, by_type, demo_catalog, events, page_body, raw  # noqa: F401

from laai_agents.g2_sim import G2Client, render
from laai_agents.integration import GlassesAPIError, GlassesWorkflow, PageWatcher
from laai_agents.models import ConsiderRequest


# Imported fixtures: demo_catalog is autouse; base_url starts this module's own orchestrator.
@pytest.fixture
def api_url(base_url):  # noqa: F811
    return base_url


TOKENS = {"web": "demo-web", "agent": "demo-agent", "executor": "demo-executor"}


def watcher(api_url, tmp_path, **options):
    return PageWatcher(api_url, TOKENS, tmp_path / "watch.json", **options)


async def new_session(api_url):
    status, session = await raw(api_url, "demo-web", "POST", "/v1/sessions", {})
    assert status == 201
    return session["session_id"]


async def put_page(api_url, session_id, request_id, **page):
    status, ref = await raw(
        api_url,
        "demo-web",
        "PUT",
        f"/v1/sessions/{session_id}/page",
        {"schema_version": "1.0", "request_id": request_id, **page_body(**page)},
    )
    assert status == 200
    return ref["revision"]


async def display(api_url, session_id):
    status, body = await raw(
        api_url,
        "demo-g2",
        "GET",
        f"/v1/sessions/{session_id}/display",
        params={"after_revision": "0"},
    )
    assert status == 200
    return body


async def settle(worker, passes=3):
    """Run passes until the watcher has caught up with its own writes."""
    for _ in range(passes):
        await worker.watch_once()


async def test_page_put_watcher_proposes_g2_approves_result_on_display(
    api_url, tmp_path
):
    session_id = await new_session(api_url)
    worker = watcher(api_url, tmp_path)
    try:
        await settle(worker)  # Empty session: nothing to propose.
        assert (await display(api_url, session_id))["mode"] == "summary"

        await put_page(api_url, session_id, "watch-page-1")
        async with G2Client(api_url, "demo-g2", session_id) as g2:
            await worker.watch_once()
            listed = await g2.wait_for(lambda d: d["mode"] == "actions")
            assert [label for _, label in render(listed).items] == [
                "Compare prices and alternatives",
                "Check independent reviews",
            ]
            compare = by_type(listed, "compare_prices")
            assert compare["description"].startswith(f"Research {PRODUCT};")

            # Further passes see the watcher's own revision bump and propose nothing new.
            await settle(worker)
            again = await display(api_url, session_id)
            assert [a["action_id"] for a in again["actions"]] == [
                a["action_id"] for a in listed["actions"]
            ]

            await g2.refresh()
            await g2.decide(compare["action_id"], request_id="watch-approve-1")
            await worker.watch_once()
            done = await g2.wait_for(lambda d: d["mode"] == "result")
            result = by_type(done, "compare_prices")
            assert result["status"] == "completed"
            assert PRODUCT in result["result"]["message"]
            assert render(done).lines[0] == "Done: Compare prices and alternatives"
            [event] = await events(api_url, session_id)
            assert event["consumed_at"] and event["action_status"] == "completed"
    finally:
        await worker.close()


async def test_new_page_invalidates_and_gets_its_own_proposal(api_url, tmp_path):
    session_id = await new_session(api_url)
    worker = watcher(api_url, tmp_path)
    try:
        await put_page(api_url, session_id, "inv-page-1")
        await settle(worker)
        first = await display(api_url, session_id)
        old_ids = {a["action_id"] for a in first["actions"]}
        assert len(old_ids) == 2

        await put_page(
            api_url,
            session_id,
            "inv-page-2",
            page_id="page-bose",
            title="Bose QuietComfort Ultra Headphones",
        )
        await settle(worker)
        second = await display(api_url, session_id)
        assert second["page"]["page_id"] == "page-bose"
        assert len(second["actions"]) == 2
        assert not old_ids & {a["action_id"] for a in second["actions"]}
        assert (
            "Bose QuietComfort Ultra Headphones" in second["actions"][0]["description"]
        )

        # The same page PUT again (new snapshot) invalidates; it gets fresh action IDs.
        await put_page(
            api_url,
            session_id,
            "inv-page-3",
            page_id="page-bose",
            title="Bose QuietComfort Ultra Headphones",
        )
        await settle(worker)
        third = await display(api_url, session_id)
        assert len(third["actions"]) == 2
        assert not {a["action_id"] for a in second["actions"]} & {
            a["action_id"] for a in third["actions"]
        }

        # The API refuses an old page's action even at the current revision.
        status, body = await raw(
            api_url,
            "demo-g2",
            "POST",
            f"/v1/sessions/{session_id}/actions/{next(iter(old_ids))}/approve",
            {"request_id": "inv-old", "expected_revision": third["revision"]},
        )
        assert status == 409 and body["error"]["code"] == "CONFLICT"
    finally:
        await worker.close()


async def test_never_proposes_for_a_page_replaced_mid_proposal(api_url, tmp_path):
    session_id = await new_session(api_url)
    worker = watcher(api_url, tmp_path)
    real_api = worker.api
    replaced = []

    async def api(role, method, path, body=None, params=None):
        if role == "agent" and method == "PUT" and not replaced:
            # The glasses navigate between the watcher's display read and its proposal.
            replaced.append(
                await put_page(
                    api_url,
                    session_id,
                    "race-page-2",
                    page_id="page-article",
                    title="Market news",
                    kind="article",
                )
            )
        return await real_api(role, method, path, body, params)

    worker.api = api
    try:
        await put_page(api_url, session_id, "race-page-1")
        await worker.watch_once()
        assert replaced
        shown = await display(api_url, session_id)
        assert shown["page"]["page_id"] == "page-article" and shown["actions"] == []
        state = worker.state["sessions"][session_id]["proposal"]
        assert (
            state["page_id"] == "page-sony" and state["accepted_revision"] == "refused"
        )
        # Articles have no registered handler yet, so later passes leave the page alone.
        await settle(worker)
        assert (await display(api_url, session_id))["actions"] == []
    finally:
        await worker.close()


@pytest.mark.parametrize(
    "applied", [False, True], ids=["crash-before-put", "reply-lost"]
)
async def test_restart_resends_the_journaled_proposal_once(api_url, tmp_path, applied):
    session_id = await new_session(api_url)
    await put_page(api_url, session_id, f"restart-page-{applied}")
    crashed = watcher(api_url, tmp_path)
    real_api = crashed.api

    async def api(role, method, path, body=None, params=None):
        if role == "agent" and method == "PUT":
            if applied:
                await real_api(role, method, path, body, params)
            raise aiohttp.ClientConnectionError("connection lost")
        return await real_api(role, method, path, body, params)

    crashed.api = api
    await crashed.watch_once()
    await crashed.close()
    journaled = crashed.state["sessions"][session_id]["proposal"]
    assert journaled.get("accepted_revision") is None

    restarted = watcher(api_url, tmp_path)
    try:
        await settle(restarted)
        shown = await display(api_url, session_id)
        assert [a["action_id"] for a in shown["actions"]] == journaled["action_ids"]
        # A second restart also leaves the one proposal in place.
        await settle(watcher(api_url, tmp_path))
        assert (await display(api_url, session_id))["actions"] == shown["actions"]
    finally:
        await restarted.close()


async def test_skips_other_proposers_unsupported_kinds_and_stale_sessions(
    api_url, tmp_path
):
    # Another agent already proposed for this page: the watcher adds nothing.
    other = GlassesWorkflow(api_url, TOKENS, tmp_path / "other.json")
    proposed = await other.propose(page_body(), ConsiderRequest(product=PRODUCT))
    await other.close()
    before = (await display(api_url, proposed))["actions"]

    generic = await new_session(api_url)
    await put_page(api_url, generic, "generic-1", page_id="page-gen", kind="generic")
    fresh = await new_session(api_url)
    await put_page(api_url, fresh, "fresh-1", page_id="page-fresh")

    worker = watcher(api_url, tmp_path)
    try:
        worker.stale_after = timedelta(days=1)
        await settle(worker)
        assert (await display(api_url, proposed))["actions"] == before
        assert (await display(api_url, generic))["actions"] == []
        # The fresh product page was proposed; with no freshness window, nothing would be.
        assert len((await display(api_url, fresh))["actions"]) == 2

        idle = await new_session(api_url)
        await put_page(api_url, idle, "stale-2", page_id="page-idle")
        worker.stale_after = timedelta(0)
        await settle(worker)
        assert (await display(api_url, idle))["actions"] == []
    finally:
        await worker.close()


async def cli(env, *args):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "laai_agents.integration", *args, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await asyncio.wait_for(process.communicate(), 60)
    return process.returncode, out.decode(), err.decode()


async def test_cli_watch_once_proposes_and_refuses_a_propose_journal(api_url, tmp_path):
    session_id = await new_session(api_url)
    await put_page(api_url, session_id, "cli-page-1", page_id="page-cli")
    env = os.environ | {
        "GLASSES_API_URL": api_url,
        **{f"GLASSES_{role.upper()}_TOKEN": token for role, token in TOKENS.items()},
    }
    journal = str(tmp_path / "cli.json")
    code, out, err = await cli(env, "--journal", journal, "watch", "--once")
    assert code == 0, err
    assert out.startswith("Processed ")
    assert len((await display(api_url, session_id))["actions"]) == 2
    # A watcher journal cannot be reused for one-session propose/poll.
    code, _, err = await cli(env, "--journal", journal, "poll")
    assert code != 0 and "belongs to a watcher" in err


async def test_a_refused_proposal_is_not_retried_for_the_same_page(tmp_path):
    """No server: the API keeps showing the same page and refuses the proposal (409)."""
    from datetime import UTC, datetime

    worker = PageWatcher("http://127.0.0.1:9", TOKENS, tmp_path / "watch.json")
    puts = []

    async def api(role, method, path, body=None, params=None):
        if method == "GET":
            return {"page": {"page_id": "page-sony", "kind": "product", "title": "Sony WH-1000XM5"},
                    "actions": []}
        puts.append(body)
        raise GlassesAPIError(method, 409)

    worker.api = api
    listed = {"session_id": "s1", "revision": 1, "updated_at": datetime.now(UTC).isoformat()}
    try:
        workflow = worker.workflow("s1")
        for _ in range(3):
            assert await worker.propose_for_page(workflow, listed) is None
        assert len(puts) == 1
        assert workflow.state["proposal"]["accepted_revision"] == "refused"
    finally:
        await worker.close()
