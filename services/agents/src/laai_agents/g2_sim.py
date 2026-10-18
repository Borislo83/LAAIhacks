"""Simulated G2 glasses client for the shared v1.0 session API.

It behaves the way the contract says the G2 app does: it polls the display with
`after_revision`, renders every `mode`, approves or rejects exactly one listed action
against the revision it showed, retries with the same `request_id`, and recovers from
STALE_REVISION, ACTION_EXPIRED, CONFLICT, RATE_LIMITED and BACKEND_UNAVAILABLE.
It never invents actions: only IDs present in the last display can be chosen.

    laai-g2-sim --base-url http://127.0.0.1:8001 --token demo-g2 --session <id> --approve 1

Point --base-url at our orchestrator or at any server implementing the contract.
"""

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from uuid import uuid4

import aiohttp

MODES = {"summary", "actions", "confirm", "loading", "result", "error", "reader"}
# Even Hub firmware limits (the simulator enforces them too): list items are at most
# 63 UTF-8 bytes and 20 entries; the 576x288 lens fits about 10 lines of ~48 characters.
LIST_ITEM_BYTES = 63
LIST_ITEMS = 20
LINE_CHARS = 48
SCREEN_LINES = 10
# Errors worth retrying with the same request_id; anything else is final for this tap.
RETRYABLE = {"RATE_LIMITED", "BACKEND_UNAVAILABLE"}


class G2Error(RuntimeError):
    def __init__(self, status, code, message, request_id=None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.request_id = (
            status,
            code,
            message,
            request_id,
        )
        self.retry_after = None


def clip_bytes(text, limit=LIST_ITEM_BYTES):
    data = text.encode()
    if len(data) <= limit:
        return text
    return data[: limit - 3].decode(errors="ignore") + "..."


def clip_line(text, limit=LINE_CHARS):
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass
class Frame:
    """What the lens shows: text lines plus a list of selectable action labels."""

    mode: str
    lines: list[str]
    items: list[tuple[str, str]] = field(default_factory=list)  # (action_id, label)
    overflow: list[str] = field(default_factory=list)  # content that had to be cut


def render(display, selected=None):
    """Lays a DisplayState out for the lens. `selected` turns a listed action into `confirm`."""
    mode = display["mode"]
    if mode not in MODES:
        raise ValueError(f"Unknown display mode {mode!r}")
    page = display.get("page") or {}
    actions = display.get("actions") or []
    overflow = []
    lines = []

    def add(text):
        if len(text) > LINE_CHARS:
            overflow.append(text)
        lines.append(clip_line(text))

    def listing():
        proposed = [a for a in actions if a["status"] == "proposed"]
        if len(proposed) > LIST_ITEMS:
            overflow.append(f"{len(proposed)} actions (max {LIST_ITEMS})")
        items = []
        for action in proposed[:LIST_ITEMS]:
            if len(action["label"].encode()) > LIST_ITEM_BYTES:
                overflow.append(action["label"])
            items.append((action["action_id"], clip_bytes(action["label"])))
        return items

    if display.get("notice"):
        add("! " + display["notice"])
    if selected is not None:
        action = next(a for a in actions if a["action_id"] == selected)
        mode = "confirm"
        add("Confirm: " + action["label"])
        if action.get("description"):
            add(action["description"])
        add("Tap = approve, double-tap = cancel")
        return Frame(mode, lines[:SCREEN_LINES], [], overflow + lines[SCREEN_LINES:])
    if mode in ("summary", "actions", "reader"):
        if page:
            add(page["title"])
            for point in page.get("summary", []):
                add("- " + point)
            for fact in page.get("facts", []):
                add(f"{fact['label']}: {fact['value']}")
        else:
            add("Waiting for a page...")
        items = listing() if mode == "actions" else []
    elif mode == "loading":
        running = [a for a in actions if a["status"] in ("approved", "running")]
        add("Working: " + (running[0]["label"] if running else "..."))
        items = listing()
    elif mode in ("result", "error"):
        for action in actions:
            if action["status"] in ("completed", "failed"):
                result = action.get("result") or {}
                add(
                    ("Done: " if action["status"] == "completed" else "Failed: ")
                    + action["label"]
                )
                for text in (result.get("message") or "").splitlines():
                    add(text)
                if result.get("result_url"):
                    add(result["result_url"])
        items = listing()
    else:  # confirm comes only from local selection; the API never returns it.
        items = []
    if len(lines) > SCREEN_LINES:
        overflow.extend(lines[SCREEN_LINES:])
    return Frame(mode, lines[:SCREEN_LINES], items, overflow)


class G2Client:
    def __init__(
        self, base_url, token, session_id, *, retries=4, backoff=0.2, log=None
    ):
        self.base_url = base_url.rstrip("/")
        # Optional list that receives every exchange (for transcripts and tests).
        self.log = log
        self.token, self.session_id = token, session_id
        self.retries, self.backoff = retries, backoff
        self.revision = 0
        self.display = None
        self.notices: list[str] = []
        self.http: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *exc):
        await self.http.close()

    async def call(self, method, path, body=None, params=None):
        """One HTTP call. Returns (status, json or None) or raises G2Error with the envelope."""
        async with self.http.request(
            method,
            self.base_url + path,
            json=body,
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
        ) as response:
            try:
                payload = (
                    None
                    if response.status == 204
                    else await response.json(content_type=None)
                )
            except ValueError:  # e.g. an HTML 502/503 page from a proxy
                payload = None
            if self.log is not None:
                self.log.append(
                    {
                        "method": method,
                        "path": path,
                        "params": params,
                        "body": body,
                        "status": response.status,
                        "response": payload,
                    }
                )
            if response.status == 204:
                return 204, None, response.headers
            if response.status >= 400:
                error = (payload or {}).get("error") or {}
                failure = G2Error(
                    response.status,
                    error.get("code", "BACKEND_UNAVAILABLE"),
                    error.get("message", ""),
                    error.get("request_id"),
                )
                retry_after = response.headers.get("Retry-After", "")
                failure.retry_after = (
                    float(retry_after) if retry_after.isdigit() else None
                )
                raise failure
            return response.status, payload, response.headers

    async def retrying(self, method, path, body=None, params=None):
        """Retries transient failures (network, 429, 503) with the same body and request_id."""
        for attempt in range(self.retries + 1):
            try:
                return await self.call(method, path, body, params)
            except G2Error as exc:
                if exc.code not in RETRYABLE or attempt == self.retries:
                    raise
                if exc.code == "RATE_LIMITED" and exc.retry_after:
                    await asyncio.sleep(min(exc.retry_after, 5))
                    continue
            except (aiohttp.ClientError, TimeoutError):
                if attempt == self.retries:
                    raise G2Error(
                        503, "BACKEND_UNAVAILABLE", "Glasses API unreachable"
                    ) from None
            await asyncio.sleep(self.backoff * 2**attempt)

    @property
    def path(self):
        return f"/v1/sessions/{self.session_id}"

    async def poll(self):
        """GET display?after_revision=N. Returns the new DisplayState, or None on 204."""
        status, display, _ = await self.retrying(
            "GET", self.path + "/display", params={"after_revision": str(self.revision)}
        )
        if status == 204:
            return None
        if display["schema_version"] != "1.0":
            raise ValueError("Unsupported display schema_version")
        if display["revision"] < self.revision:
            raise ValueError("Display revision went backwards")
        self.revision, self.display = display["revision"], display
        return display

    async def refresh(self):
        """Fetches the full current display regardless of the last seen revision."""
        self.revision = 0
        return await self.poll()

    async def wait_for(self, predicate, timeout=20.0, interval=0.1):
        """Polls until `predicate(display)` holds, as the G2 app's poll loop would."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            await self.poll()
            if self.display and predicate(self.display):
                return self.display
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(
                    f"Display never matched; last revision {self.revision}"
                )
            await asyncio.sleep(interval)

    def listed(self, action_id):
        return any(
            a["action_id"] == action_id and a["status"] == "proposed"
            for a in (self.display or {}).get("actions", [])
        )

    async def decide(
        self, action_id, decision="approve", *, request_id=None, confirm=None
    ):
        """Approves or rejects one action shown on the lens, as the user confirmed it.

        Returns the ApprovalResponse, or None when the action is no longer available
        (the notice says why). STALE_REVISION refreshes the display and, if the action is
        still listed, asks `confirm(display)` again before resubmitting with a new request_id.
        """
        if decision not in ("approve", "reject"):
            raise ValueError(decision)
        if not self.listed(action_id):
            raise ValueError(
                "G2 can only decide actions present in the current display"
            )
        request_id = request_id or f"g2-{decision}-{uuid4()}"
        for attempt in range(3):
            body = {
                "schema_version": "1.0",
                "request_id": request_id
                if attempt == 0
                else f"{request_id}-r{attempt}",
                "expected_revision": self.revision,
            }
            try:
                _, response, _ = await self.retrying(
                    "POST", f"{self.path}/actions/{action_id}/{decision}", body
                )
                return response
            except G2Error as exc:
                if exc.code == "STALE_REVISION":
                    await self.refresh()
                    if not self.listed(action_id):
                        self.notices.append("That option changed; pick again.")
                        return None
                    if confirm is not None and not confirm(self.display):
                        self.notices.append("Cancelled after the display changed.")
                        return None
                    continue
                if exc.code in ("ACTION_EXPIRED", "CONFLICT", "ACTION_NOT_FOUND"):
                    await self.refresh()
                    self.notices.append(
                        {
                            "ACTION_EXPIRED": "That option expired.",
                            "CONFLICT": "That option is no longer available.",
                            "ACTION_NOT_FOUND": "That option is gone.",
                        }[exc.code]
                    )
                    return None
                raise
        self.notices.append("Display keeps changing; try again.")
        return None


async def run(args):
    async with G2Client(args.base_url, args.token, args.session) as g2:
        decided = False
        while True:
            display = await g2.poll()
            if display is not None:
                frame = render(display)
                print(f"--- revision {display['revision']} mode {frame.mode}")
                print("\n".join(frame.lines))
                for index, (_, label) in enumerate(frame.items, 1):
                    print(f"  [{index}] {label}")
                for text in frame.overflow:
                    print(f"  (cut for the lens: {text[:60]})")
                choice = args.approve or args.reject
                if choice and not decided and len(frame.items) >= choice:
                    action_id = frame.items[choice - 1][0]
                    decision = "approve" if args.approve else "reject"
                    print(
                        json.dumps(
                            await g2.decide(action_id, decision, confirm=lambda d: True)
                        )
                    )
                    decided = True
                for notice in g2.notices:
                    print("! " + notice)
                g2.notices.clear()
                if args.once or display["mode"] in ("result", "error") and decided:
                    return
            await asyncio.sleep(args.interval)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--token", default="demo-g2")
    parser.add_argument("--session", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument(
        "--once", action="store_true", help="render one display and exit"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--approve", type=int, metavar="N", help="approve listed option N"
    )
    group.add_argument("--reject", type=int, metavar="N", help="reject listed option N")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
