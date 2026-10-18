"""Client for the Glasses Team's v1.0 API; never implements a competing API/DB.

Only registered, read-only research handlers are supported. This client cannot approve,
open a URL, purchase, or execute arbitrary custom actions. A local journal preserves
request IDs/results across restarts. Production workers should use the DB team's
durable execution journal and the glasses team's outbox/ack agreement.

`watch` proposes actions for every page the glasses open, in every session of the
tokens' user, and runs the approved ones (propose + poll in one loop).
"""

import argparse
import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import aiohttp
from dotenv import load_dotenv

from . import catalog, handlers
from .models import (
    AlternativesRequest,
    ConsiderRequest,
    PriceCompareRequest,
    ReviewsRequest,
)


def result_message(results):
    lines = []
    for result in results:
        if result.error:
            raise RuntimeError(result.error)
        lines.append(result.summary)
        lines.extend(f"Source: {s.url}" for s in result.sources[:3])
        if result.fetched_at:
            lines.append("Retrieved: " + result.fetched_at)
    return "\n".join(lines)


class GlassesAPIError(RuntimeError):
    def __init__(self, method, status):
        # Do not echo upstream bodies, credentials or headers.
        super().__init__(f"Glasses API {method} failed: HTTP {status}")
        self.status = status


# Action states that still accept a result. Events carry the action's current status.
ACTIVE = {"approved", "running"}

LABELS = {
    "compare_prices": "Compare prices and alternatives",
    "find_reviews": "Check independent reviews",
}
# What `watch` proposes for each page kind the glasses send (product, article, generic).
# Only registered API handlers can appear here; article summaries need a new action type.
PROPOSALS = {"product": ("compare_prices", "find_reviews")}
PROPOSAL_TTL = timedelta(minutes=30)

log = logging.getLogger("laai_agents.integration")


def action_payload(action_id, plan, product, max_price):
    return {
        "action_id": action_id,
        "type": plan["handler"],
        "label": LABELS[plan["handler"]],
        "description": (
            f"Research {product}; alternative budget: "
            f"{max_price if max_price is not None else 'not set'} USD"
        ),
        "expires_at": plan["expires_at"],
        "requires_approval": True,
    }


def page_product(title: str) -> str:
    """The product a product page is about: its catalog name, else the title's lead phrase.

    Store titles append marketing text ("Sony WH-1000XM5 Wireless ... with Auto NC, ...").
    """
    known = catalog.find_product(title)
    # Only when the title names it in full: the catalog's loose match can map
    # "Bose QuietComfort Ultra Earbuds" to the Ultra Headphones.
    if known and known.name.lower() in " ".join(title.lower().split()):
        return known.name
    lead = re.split(r"\s[|–—-]\s|,|\(|\s+with\s+|:", " ".join(title.split()), maxsplit=1)[0]
    return " ".join((lead.strip() or title).split()[:8])


class GlassesWorkflow:
    def __init__(
        self,
        base_url: str,
        tokens: dict[str, str],
        journal: Path,
        state: dict | None = None,
        owner: "GlassesWorkflow | None" = None,
    ):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Glasses API requires HTTPS, except on localhost")
        self.base_url = base_url.rstrip("/")
        self.tokens, self.journal = tokens, journal
        # A watcher keeps one workflow per session inside its own journal (`state`) and
        # shares its HTTP client and journal writes (`owner`).
        self.owner = owner
        if state is not None:
            self.state = state
        elif journal.exists():
            self.state = json.loads(journal.read_text())
        else:
            self.state = {"cursor": "", "actions": {}, "outcomes": {}}
        self._session: aiohttp.ClientSession | None = None

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    def save(self):
        if self.owner is not None:
            return self.owner.save()
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.journal.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.state, stream, indent=2)
        temporary.replace(self.journal)

    async def api(self, role, method, path, body=None, params=None):
        if self.owner is not None:
            return await self.owner.api(role, method, path, body, params)
        token = self.tokens.get(role)
        if not token:
            raise RuntimeError(
                f"Configure GLASSES_{role.upper()}_TOKEN for this operation"
            )
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        async with self._session.request(
            method,
            self.base_url + path,
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            params=params,
            allow_redirects=False,
        ) as response:
            if response.status == 204:
                return None
            if response.status >= 300:
                raise GlassesAPIError(method, response.status)
            return await response.json()

    def path(self):
        return "/v1/sessions/" + quote(self.state["session_id"], safe="")

    async def propose(
        self, page: dict, request: ConsiderRequest, session_id: str | None = None
    ):
        if self.state.get("page") and self.state["page"] != page:
            raise ValueError(
                "Use a new journal per page context; the API invalidates old page actions"
            )
        if self.state.get("request") and self.state["request"] != request.model_dump():
            raise ValueError("Use a new journal when product or budget changes")
        if "session_id" not in self.state:
            if session_id:
                self.state["session_id"] = session_id
            else:
                # Contract does not specify idempotency for session creation. A lost
                # creation response can orphan a session, but cannot execute an action.
                session = await self.api("web", "POST", "/v1/sessions", {})
                self.state["session_id"] = session["session_id"]
            self.save()
        if "page" not in self.state:
            self.state.update(
                page=page,
                request=request.model_dump(),
                page_request_id=str(uuid4()),
                actions_request_id=str(uuid4()),
            )
            expires = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
            # The display orders actions by (proposed_at, action_id), and one proposal
            # shares proposed_at, so the index prefix keeps the G2 menu in this order.
            self.state["actions"] = {
                f"act-{index}-{uuid4()}": {
                    "handler": handler,
                    "page_id": page["page_id"],
                    "expires_at": expires,
                }
                for index, handler in enumerate(("compare_prices", "find_reviews"), 1)
            }
            self.save()
        await self.api(
            "web",
            "PUT",
            self.path() + "/page",
            {
                **self.state["page"],
                "schema_version": "1.0",
                "request_id": self.state["page_request_id"],
            },
        )
        await self.api(
            "agent",
            "PUT",
            self.path() + "/actions",
            {
                "schema_version": "1.0",
                "request_id": self.state["actions_request_id"],
                "page_id": page["page_id"],
                "actions": [
                    action_payload(action_id, plan, request.product, request.max_price)
                    for action_id, plan in self.state["actions"].items()
                ],
            },
        )
        return self.state["session_id"]

    async def execute(self, plan):
        # Watcher plans carry their own product; `propose` journals one request per page.
        request = (
            ConsiderRequest(product=plan["product"], max_price=plan.get("max_price"))
            if "product" in plan
            else ConsiderRequest(**self.state["request"])
        )
        if plan["handler"] == "find_reviews":
            return result_message(
                [await handlers.get_reviews(ReviewsRequest(product=request.product))]
            )
        if plan["handler"] == "compare_prices":
            price, alternatives = await asyncio.gather(
                handlers.compare_prices(PriceCompareRequest(product=request.product)),
                handlers.find_alternatives(
                    AlternativesRequest(
                        product=request.product,
                        max_price=request.max_price,
                    )
                ),
            )
            return result_message([price, alternatives])
        raise ValueError("No approved registered handler")

    async def fetch_events(self):
        payload = await self.api(
            "executor",
            "GET",
            self.path() + "/events",
            params={"after": self.state["cursor"]},
        )
        return payload if isinstance(payload, list) else payload["events"]

    async def still_approved(self, event):
        # Re-read the event feed (executor role) for the action's current status.
        current = next(
            (e for e in await self.fetch_events() if e["event_id"] == event["event_id"]),
            event,
        )
        return current.get("action_status", "approved") in ACTIVE

    def record_foreign(self, event):
        # Another proposer's action: not ours to run, ack or report on.
        self.state.setdefault("not_mine", {})[event["event_id"]] = {
            "action_id": event["action_id"],
            "recorded_at": datetime.now(UTC).isoformat(),
        }

    def record_invalidated(self, event):
        # The API invalidated this approval (page or proposal changed). Nothing may run
        # for it, and the API rejects its results, so record it and move past it.
        self.state.setdefault("invalidated", {})[event["event_id"]] = {
            "action_id": event["action_id"],
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        self.save()

    async def post_result(self, event, path, body):
        try:
            await self.api("executor", "POST", path, body)
        except GlassesAPIError as exc:
            # A 409 can mean the action was invalidated mid-run; only skip if so.
            if exc.status != 409 or await self.still_approved(event):
                raise
            self.record_invalidated(event)
            return False
        return True

    async def handle_approved(self, event, plan):
        event_id, action_id = event["event_id"], event["action_id"]
        if event_id in self.state.get("invalidated", {}):
            return
        path = self.path() + "/actions/" + quote(action_id, safe="") + "/result"
        if event_id not in self.state["outcomes"]:
            if event.get("action_status", "approved") not in ACTIVE:
                self.record_invalidated(event)
                return
            # Authenticated event carries the approval; product/handler/budget
            # come from the journal, never from event/webpage instructions.
            if datetime.fromisoformat(plan["expires_at"]) <= datetime.now(UTC):
                outcome = {
                    "status": "failed",
                    "message": "Action expired before execution",
                }
            else:
                running = {
                    "request_id": event_id + ":running",
                    "status": "running",
                    "message": "Researching approved product",
                }
                if not await self.post_result(event, path, running):
                    return
                try:
                    outcome = {
                        "status": "completed",
                        "message": await self.execute(plan),
                    }
                except Exception:  # noqa: BLE001 -- persist a safe failure result for the API
                    outcome = {
                        "status": "failed",
                        "message": "Live research unavailable",
                    }
            self.state["outcomes"][event_id] = {
                "request_id": event_id + ":outcome",
                **outcome,
            }
            self.save()
        await self.post_result(event, path, self.state["outcomes"][event_id])

    async def poll_once(self):
        events = await self.fetch_events()
        for event in events:
            if event["session_id"] != self.state["session_id"]:
                raise ValueError("Cross-session event rejected")
            plan = self.state["actions"].get(event["action_id"])
            if event["type"] == "action.approved":
                if not plan:
                    self.record_foreign(event)
                elif event["page_id"] != plan["page_id"]:
                    raise ValueError("Cross-page approved action; cursor not advanced")
                else:
                    await self.handle_approved(event, plan)
            # Never advance until the result is accepted or the approval is recorded as
            # invalidated or foreign. API retries use the same IDs.
            self.state["cursor"] = event["cursor"]
            self.save()
        return len(events)


class PageWatcher(GlassesWorkflow):
    """Proposes actions for each new page in every session of the tokens' user, then runs
    the approved ones.

    `GET /v1/sessions` lists the user's unexpired sessions with their revision. Every page
    PUT, proposal, approval and result bumps the revision, so only sessions whose revision
    changed are re-read (`/display`) and polled for approval events. A page needs a proposal
    when it is the session's active page, its kind has handlers (`PROPOSALS`), and it has no
    actions at all: a new page PUT invalidates the old page's actions, which drops them from
    the display, while any proposal (ours or another agent's, even rejected) keeps it from
    being proposed again. The API refuses proposals for a page that is no longer active.

    The journal keeps each session's proposal request ID and action IDs, so a restart
    resends the same proposal (applied once) instead of making a second one.
    """

    def __init__(self, base_url, tokens, journal, stale_after=timedelta(minutes=15)):
        super().__init__(base_url, tokens, journal)
        self.state.setdefault("sessions", {})
        self.stale_after = stale_after
        self.workflows: dict[str, GlassesWorkflow] = {}

    def workflow(self, session_id):
        state = self.state["sessions"].setdefault(
            session_id,
            {
                "session_id": session_id,
                "cursor": "",
                "actions": {},
                "outcomes": {},
                "seen_revision": 0,
            },
        )
        if session_id not in self.workflows:
            self.workflows[session_id] = GlassesWorkflow(
                self.base_url, self.tokens, self.journal, state, owner=self
            )
        return self.workflows[session_id]

    async def propose_for_page(self, workflow, listed):
        """Propose the page kind's actions for the active page if it has none yet."""
        state = workflow.state
        updated = datetime.fromisoformat(listed["updated_at"])
        if datetime.now(UTC) - updated > self.stale_after:
            return None  # Nobody has touched this session lately; its page is stale.
        display = await self.api(
            "web", "GET", workflow.path() + "/display", params={"after_revision": 0}
        )
        page = display and display.get("page")
        if not page or display["actions"]:
            return None
        kinds = PROPOSALS.get(page["kind"], ())
        if not kinds:
            return None
        pending = state.get("proposal")
        if (
            pending
            and pending["page_id"] == page["page_id"]
            and pending.get("accepted_revision") == "refused"
        ):
            return None  # The API refused this page's proposal; never retry it.
        if not (
            pending
            and pending["page_id"] == page["page_id"]
            and pending.get("accepted_revision") is None
        ):
            # A page with no actions after our accepted proposal was PUT again, which
            # invalidated them; it needs new action IDs (the API never revives old ones).
            product = page_product(page["title"])
            expires = (datetime.now(UTC) + PROPOSAL_TTL).isoformat()
            pending = state["proposal"] = {
                "page_id": page["page_id"],
                "product": product,
                "request_id": str(uuid4()),
                "action_ids": [],
            }
            for index, handler in enumerate(kinds, 1):
                action_id = f"act-{index}-{uuid4()}"
                pending["action_ids"].append(action_id)
                state["actions"][action_id] = {
                    "handler": handler,
                    "page_id": page["page_id"],
                    "expires_at": expires,
                    "product": product,
                }
            self.save()
        body = {
            "schema_version": "1.0",
            "request_id": pending["request_id"],
            "page_id": pending["page_id"],
            "actions": [
                action_payload(i, state["actions"][i], pending["product"], None)
                for i in pending["action_ids"]
            ],
        }
        try:
            response = await self.api("agent", "PUT", workflow.path() + "/actions", body)
        except GlassesAPIError as exc:
            if exc.status != 409:
                raise
            # The page changed under us, or the proposal expired: never retry it.
            pending["accepted_revision"] = "refused"
            self.save()
            log.info("proposal for %s refused (page no longer active)", page["page_id"])
            return None
        pending["accepted_revision"] = response["revision"]
        self.save()
        log.info("proposed %s for %s", ", ".join(kinds), page["page_id"])
        return pending["page_id"]

    async def watch_once(self):
        """One pass over the user's sessions. Returns how many sessions were processed."""
        listed = (await self.api("agent", "GET", "/v1/sessions"))["sessions"]
        live = {s["session_id"] for s in listed}
        for session_id in set(self.state["sessions"]) - live:
            # Expired: the API refuses every call for it, so its journal is done.
            del self.state["sessions"][session_id]
            self.workflows.pop(session_id, None)
        processed = 0
        for item in listed:
            workflow = self.workflow(item["session_id"])
            if item["revision"] == workflow.state["seen_revision"]:
                continue
            try:
                await self.propose_for_page(workflow, item)
                await workflow.poll_once()
            except Exception:  # One bad session must not stop the others.
                # Leave seen_revision behind so the next pass retries this session.
                log.exception("session %s failed; retrying next pass", item["session_id"])
                continue
            # Our own writes bump the revision again, so the next pass reads it once more.
            workflow.state["seen_revision"] = item["revision"]
            self.save()
            processed += 1
        return processed

    async def watch(self, interval=2.0):
        while True:
            try:
                await self.watch_once()
            except (GlassesAPIError, aiohttp.ClientError, TimeoutError) as exc:
                log.warning("listing sessions failed: %s", exc)
            await asyncio.sleep(interval)


def lock_exclusive(stream):
    """Fail fast (OSError) if another poller already holds this journal's lock."""
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


async def run_cli(args):
    tokens = {
        role: os.getenv(f"GLASSES_{role.upper()}_TOKEN", "")
        for role in ("web", "agent", "executor")
    }
    cls = PageWatcher if args.command == "watch" else GlassesWorkflow
    workflow = cls(os.environ["GLASSES_API_URL"], tokens, Path(args.journal))
    if args.command != "watch" and "sessions" in workflow.state:
        raise ValueError("This journal belongs to a watcher; use `watch` with it")
    if args.command == "watch" and "session_id" in workflow.state:
        raise ValueError("This journal belongs to one propose/poll session; use a new one")
    try:
        if args.command == "watch":
            if args.once:
                print(f"Processed {await workflow.watch_once()} sessions")
            else:
                await workflow.watch(args.interval)
        elif args.command == "propose":
            page = json.loads(Path(args.page).read_text())
            print(
                await workflow.propose(
                    page,
                    ConsiderRequest(product=args.product, max_price=args.budget),
                    args.session,
                )
            )
        else:
            print(f"Processed {await workflow.poll_once()} events")
    finally:
        await workflow.close()


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    propose = commands.add_parser("propose")
    propose.add_argument("--page", required=True)
    propose.add_argument("--product", required=True)
    propose.add_argument("--budget", type=float)
    propose.add_argument("--session")
    commands.add_parser("poll")
    watch = commands.add_parser(
        "watch", help="propose for every new page in the user's sessions, run approvals"
    )
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--once", action="store_true", help="one pass, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    lock = Path(args.journal + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as stream:
        lock_exclusive(stream)
        asyncio.run(run_cli(args))


if __name__ == "__main__":
    main()
