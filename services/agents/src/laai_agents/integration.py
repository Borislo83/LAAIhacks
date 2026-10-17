"""Client for the Glasses Team's v1.0 API; never implements a competing API/DB.

Only registered, read-only research handlers are supported. This client cannot approve,
open a URL, purchase, or execute arbitrary custom actions. A local journal preserves
request IDs/results across restarts. Production workers should use the DB team's
durable execution journal and the glasses team's outbox/ack agreement.
"""

import argparse
import asyncio
import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import aiohttp
from dotenv import load_dotenv

from . import handlers
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


class GlassesWorkflow:
    def __init__(self, base_url: str, tokens: dict[str, str], journal: Path):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Glasses API requires HTTPS, except on localhost")
        self.base_url = base_url.rstrip("/")
        self.tokens, self.journal = tokens, journal
        self.state = (
            json.loads(journal.read_text())
            if journal.exists()
            else {
                "cursor": "",
                "actions": {},
                "outcomes": {},
            }
        )

    def save(self):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.journal.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.state, stream, indent=2)
        temporary.replace(self.journal)

    async def api(self, role, method, path, body=None, params=None):
        token = self.tokens.get(role)
        if not token:
            raise RuntimeError(
                f"Configure GLASSES_{role.upper()}_TOKEN for this operation"
            )
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session,
            session.request(
                method,
                self.base_url + path,
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                params=params,
                allow_redirects=False,
            ) as response,
        ):
            if response.status == 204:
                return None
            if response.status >= 300:
                # Do not echo upstream bodies, credentials or headers.
                raise RuntimeError(
                    f"Glasses API {method} failed: HTTP {response.status}"
                )
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
            self.state["actions"] = {
                "act-" + str(uuid4()): {
                    "handler": handler,
                    "page_id": page["page_id"],
                    "expires_at": expires,
                }
                for handler in ("compare_prices", "find_reviews")
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
                    {
                        "action_id": action_id,
                        "type": plan["handler"],
                        "label": "Compare prices and alternatives"
                        if plan["handler"] == "compare_prices"
                        else "Check independent reviews",
                        "description": (
                            f"Research {request.product}; alternative budget: "
                            f"{request.max_price if request.max_price is not None else 'not set'} USD"
                        ),
                        "expires_at": plan["expires_at"],
                        "requires_approval": True,
                    }
                    for action_id, plan in self.state["actions"].items()
                ],
            },
        )
        return self.state["session_id"]

    async def execute(self, plan):
        request = ConsiderRequest(**self.state["request"])
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

    async def poll_once(self):
        payload = await self.api(
            "executor",
            "GET",
            self.path() + "/events",
            params={"after": self.state["cursor"]},
        )
        events = payload if isinstance(payload, list) else payload["events"]
        for event in events:
            if event["session_id"] != self.state["session_id"]:
                raise ValueError("Cross-session event rejected")
            action_id = event["action_id"]
            plan = self.state["actions"].get(action_id)
            if event["type"] == "action.approved":
                if not plan or event["page_id"] != plan["page_id"]:
                    raise ValueError(
                        "Unknown or cross-page approved action; cursor not advanced"
                    )
                event_id = event["event_id"]
                path = self.path() + "/actions/" + quote(action_id, safe="") + "/result"
                if event_id not in self.state["outcomes"]:
                    display = await self.api("web", "GET", self.path() + "/display")
                    current = next(
                        (
                            a
                            for a in display.get("actions", [])
                            if a["action_id"] == action_id
                        ),
                        None,
                    )
                    if (
                        not display.get("page")
                        or display["page"]["page_id"] != plan["page_id"]
                        or not current
                        or current["status"] not in {"approved", "running"}
                    ):
                        raise ValueError(
                            "Action is no longer approved in the active page context"
                        )
                    # Authenticated event carries the approval; product/handler/budget
                    # come from the journal, never from event/webpage instructions.
                    if datetime.fromisoformat(plan["expires_at"]) <= datetime.now(UTC):
                        outcome = {
                            "status": "failed",
                            "message": "Action expired before execution",
                        }
                    else:
                        await self.api(
                            "executor",
                            "POST",
                            path,
                            {
                                "request_id": event_id + ":running",
                                "status": "running",
                                "message": "Researching approved product",
                            },
                        )
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
                await self.api(
                    "executor", "POST", path, self.state["outcomes"][event_id]
                )
            # Never advance until the result is accepted. API retries use the same IDs.
            self.state["cursor"] = event["cursor"]
            self.save()
        return len(events)


async def run_cli(args):
    workflow = GlassesWorkflow(
        os.environ["GLASSES_API_URL"],
        {
            role: os.getenv(f"GLASSES_{role.upper()}_TOKEN", "")
            for role in ("web", "agent", "executor")
        },
        Path(args.journal),
    )
    if args.command == "propose":
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
    args = parser.parse_args()
    lock = Path(args.journal + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(run_cli(args))


if __name__ == "__main__":
    main()
