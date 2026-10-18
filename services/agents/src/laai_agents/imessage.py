"""iMessage to an allowlisted friend: prepare a draft, then send it only after approval.

Prepare never sends. Recipients come only from IMESSAGE_CONTACTS, never from the request.
Send needs the private IMESSAGE_EXECUTOR_TOKEN and an unexpired, unused draft whose text
matches exactly; each draft is used once. Sending runs `osascript` without a shell and
passes the handle and text as argv, so message text is never part of the script source.
Message bodies and handles are not logged.

IMESSAGE_DRY_RUN is on unless explicitly set to 0: every send step runs (token, draft,
single use, text match, rate limit, the exact osascript argv) but the argv is recorded
in memory instead of executed.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import sys
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

from pydantic.v1 import Field, SecretStr
from uagents import Model

from . import llm
from .progress import report, tracked

log = logging.getLogger(__name__)

DRAFT_SECONDS = 300
MAX_DRAFTS = 64
RATE_LIMIT = 10  # sends per RATE_WINDOW seconds
RATE_WINDOW = 60
SEND_TIMEOUT = 15

_drafts: OrderedDict[str, dict] = OrderedDict()
_sent_at: deque[float] = deque()
# Would-be osascript calls in dry-run mode, newest last.
dry_run_log: deque[dict] = deque(maxlen=20)

# The handle and text arrive as argv; nothing user-supplied is ever part of this source.
SEND_SCRIPT = """on run argv
    set targetHandle to item 1 of argv
    set messageText to item 2 of argv
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant targetHandle of targetService
        send messageText to targetBuddy
    end tell
end run"""

PHONE = re.compile(r"\+?[0-9]{7,15}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class MessageDraftRequest(Model):
    request: str = Field(min_length=1, max_length=1200)
    progress_id: str | None = Field(default=None, min_length=32, max_length=64)


class MessageDraftResponse(Model):
    # needs_message: the contact is known but no text was given, so ask for it.
    status: Literal["ready", "ambiguous", "unknown_contact", "needs_details", "needs_message"]
    draft_id: str | None = None
    recipient_name: str | None = None
    recipient_masked: str | None = None
    text: str | None = None
    expires_at: str | None = None
    candidates: list[str] = Field(default_factory=list)
    summary: str
    error: str | None = None


class MessageSendRequest(Model):
    draft_id: str = Field(min_length=32, max_length=64)
    text: str = Field(min_length=1, max_length=1000)
    executor_token: SecretStr = SecretStr("")


class MessageSendResponse(Model):
    status: Literal["sent", "failed", "needs_permission", "open_link"]
    summary: str
    dry_run: bool = False
    # Link delivery: an sms: URL that opens Messages with the approved draft.
    open_url: str | None = None


class MessageApproval(Model):
    """Agent-to-agent send approval; accepted only from the coordinator."""

    draft_id: str = Field(min_length=32, max_length=64)
    text: str = Field(min_length=1, max_length=1000)


class DryRunLogRequest(Model):
    executor_token: SecretStr = SecretStr("")


class DryRunLogResponse(Model):
    dry_run: bool
    calls: list[dict] = Field(default_factory=list)
    error: str | None = None


def dry_run() -> bool:
    """Real sending needs an explicit IMESSAGE_DRY_RUN=0 (or false/no/off)."""
    value = os.getenv("IMESSAGE_DRY_RUN", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def authorized(token: SecretStr) -> bool:
    expected = os.getenv("IMESSAGE_EXECUTOR_TOKEN", "")
    return bool(expected) and secrets.compare_digest(
        expected.encode(), token.get_secret_value().encode()
    )


def contacts() -> dict[str, str]:
    """IMESSAGE_CONTACTS="Alex=+15551234567,Sam=sam@icloud.com" as {name: handle}."""
    result = {}
    for entry in os.getenv("IMESSAGE_CONTACTS", "").split(","):
        name, _, handle = entry.partition("=")
        name, handle = " ".join(name.split()), handle.strip()
        phone = re.sub(r"[\s().-]", "", handle)
        if PHONE.fullmatch(phone):
            handle = phone
        elif not EMAIL.fullmatch(handle):
            continue
        if name:
            result[name] = handle
    return result


def mask(handle: str) -> str:
    if "@" in handle:
        user, _, domain = handle.partition("@")
        return f"{user[:1]}•••@{domain}"
    return f"{handle[:2]}{'•' * 5}{handle[-4:]}"


def resolve(name: str, book: dict[str, str]) -> list[str]:
    """Allowlisted names matching `name`: an exact match, else whole-word matches."""
    wanted = " ".join(name.lower().split())
    exact = [n for n in book if n.lower() == wanted]
    if exact or not wanted:
        return exact
    words = set(wanted.split())
    return [n for n in book if words <= set(n.lower().split())]


VERB = re.compile(
    r"^(?:(?:hey|hi|ok|okay|yo)[\s,!]+)?(?:please\s+|can you\s+|could you\s+)?"
    r"(?:send\s+(?:an?\s+)?(?:imessage|text|message)\s+to|imessage|text|message|tell)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
LINK = re.compile(r"^(?:that|saying|says|to say)\b\s*|^[:,\-–]\s*", re.IGNORECASE)


# "Send Jonathan a message "lol"", "shoot Alex a quick text saying I'm late".
SEND_NAME = re.compile(
    r"^(?:(?:hey|hi|ok|okay|yo)[\s,!]+)?(?:please\s+|can you\s+|could you\s+)?"
    r"(?:send|shoot|drop|write)\s+(.+?)\s+(?:an?\s+)?(?:quick\s+)?(?:imessage|message|text|msg|note)\b"
    r"\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def extract_by_pattern(request: str, book: dict[str, str]) -> tuple[str, str] | None:
    """'text Alex running late' -> ('Alex', 'running late')."""
    match = VERB.match(request.strip())
    named = None if match else SEND_NAME.match(request.strip())
    if named and named[1].lower() not in {"me", "us", "him", "her", "them", "a", "an", "the"}:
        body = LINK.sub("", named[2].strip(), count=1).strip()
        return named[1].strip(" .,!?"), body
    if not match:
        return None
    rest = match[1].strip()
    # Prefer the longest allowlisted name the request starts with ("Mom Cell" over "Mom").
    for name in sorted(book, key=len, reverse=True):
        if re.match(rf"{re.escape(name)}\b", rest, re.IGNORECASE):
            recipient, body = rest[: len(name)], rest[len(name) :]
            break
    else:
        recipient, _, body = rest.partition(" ")
    body = LINK.sub("", body.strip(), count=1).strip()
    # A known contact with no text yet ("send a message to Jonathan") still parses;
    # prepare() then asks what to say.
    return (recipient.strip(" .,!?"), body) if recipient else None


def tidy(text: str) -> str:
    """Light formatting only: no wrapping quotes, a capital first letter, end punctuation."""
    text = " ".join(text.split())
    # Only a message wrapped in one matching pair of quotes loses them; inner quotes stay.
    for left, right in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        if len(text) > 1 and text[0] == left and text[-1] == right and right not in text[1:-1]:
            text = text[1:-1].strip()
            break
    words = text.split()
    if not words:
        return text
    first, last = words[0], words[-1]
    link = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)
    # Capitalize a plain lowercase first word only: never a link or "iPhone"/"eBay".
    if first.isalpha() and first.islower() and not link.match(first):
        text = text[0].upper() + text[1:]
    # End punctuation for sentences, not for a link or a casual one- or two-word text.
    if text[-1].isalnum() and len(words) >= 3 and not link.match(last):
        text += "."
    return text


async def extract(request: str, book: dict[str, str]) -> tuple[str, str] | None:
    answer = await llm.complete(
        "Extract who the user wants to message and the exact message to send. Reply "
        'with JSON only: {"recipient": "<name>", "message": "<text>"}. Do not add, '
        "translate or rephrase the message.",
        request,
    )
    try:
        data = json.loads(answer or "")
        recipient, body = str(data["recipient"]).strip(), str(data["message"]).strip()
        # The model sometimes keeps the linking word ("that the demo works").
        body = LINK.sub("", body, count=1).strip()
        if recipient and body:
            return recipient, body
    except (ValueError, KeyError, TypeError):
        pass
    return extract_by_pattern(request, book)


def _prune() -> None:
    now = time.monotonic()
    for draft_id in list(_drafts):
        if _drafts[draft_id]["expires"] < now:
            del _drafts[draft_id]


@tracked("messenger", subject="message draft")
async def prepare(req: MessageDraftRequest) -> MessageDraftResponse:
    _prune()
    book = contacts()
    if not book:
        return MessageDraftResponse(
            status="unknown_contact",
            error="No iMessage contacts are configured",
            summary="Add friends to IMESSAGE_CONTACTS before sending messages.",
        )
    report("parsing", "Finding the recipient and message")
    parsed = await extract(req.request, book)
    if not parsed:
        return MessageDraftResponse(
            status="needs_details",
            error="Recipient or message missing",
            summary='Say who to message and what to say, e.g. "text Alex running late".',
        )
    recipient, text = parsed
    text = tidy(text)
    if len(text) > 1000:
        return MessageDraftResponse(
            status="needs_details",
            error="Message is too long",
            summary="Messages can be at most 1000 characters.",
        )
    matches = resolve(recipient, book)
    if not matches:
        return MessageDraftResponse(
            status="unknown_contact",
            candidates=sorted(book),
            error="Recipient is not in the allowed contacts",
            summary=f"{recipient[:60]} is not in your iMessage contacts. Known: {', '.join(sorted(book))}.",
        )
    if len(matches) > 1:
        return MessageDraftResponse(
            status="ambiguous",
            candidates=matches,
            text=text or None,
            error="More than one contact matches",
            summary=f"Which one did you mean: {', '.join(matches)}?",
        )
    name = matches[0]
    if not text:
        return MessageDraftResponse(
            status="needs_message",
            recipient_name=name,
            recipient_masked=mask(book[name]),
            summary=f"What would you like to say to {name}?",
        )
    draft_id = uuid4().hex
    if len(_drafts) >= MAX_DRAFTS:
        _drafts.popitem(last=False)
    _drafts[draft_id] = {
        "name": name,
        "handle": book[name],
        "text": text,
        "expires": time.monotonic() + DRAFT_SECONDS,
        "used": False,
    }
    report("draft_ready", "Draft ready; awaiting your approval")
    return MessageDraftResponse(
        status="ready",
        draft_id=draft_id,
        recipient_name=name,
        recipient_masked=mask(book[name]),
        text=text,
        expires_at=(datetime.now(UTC) + timedelta(seconds=DRAFT_SECONDS)).isoformat(),
        summary=f'Draft to {name}: "{text}". Approve to send it with iMessage; nothing is sent until then.',
    )


def _rate_limited() -> bool:
    now = time.monotonic()
    while _sent_at and now - _sent_at[0] >= RATE_WINDOW:
        _sent_at.popleft()
    return len(_sent_at) >= RATE_LIMIT


def osascript_argv(handle: str, text: str) -> list[str]:
    return ["osascript", "-e", SEND_SCRIPT, handle, text]


async def run_osascript(argv: list[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), SEND_TIMEOUT)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode, stderr.decode(errors="replace")


def _refused(summary: str) -> MessageSendResponse:
    return MessageSendResponse(status="failed", summary=summary)


def _claim(req: MessageSendRequest) -> dict | MessageSendResponse:
    """The approved draft, now used and rate-counted, or the refusal to return instead."""
    _prune()
    draft = _drafts.get(req.draft_id)
    if draft is None:
        return _refused("This draft expired or does not exist. Prepare the message again.")
    if draft["used"]:
        return _refused("This draft was already used. Prepare a new message to send again.")
    if req.text != draft["text"]:
        return _refused("The message differs from the approved draft. Nothing was sent.")
    if _rate_limited():
        return _refused("Too many messages in the last minute. Try again shortly.")
    # Single use from here on: an uncertain attempt is never repeated automatically.
    draft["used"] = True
    _sent_at.append(time.monotonic())
    return draft


def delivery() -> str:
    """'imessage' sends through Messages on macOS; 'link' hands the user an sms: link.

    Link delivery is opt-in with IMESSAGE_DELIVERY=link (for example on Windows, where
    Messages can't be automated); otherwise sending goes through iMessage as before.
    """
    value = os.getenv("IMESSAGE_DELIVERY", "").strip().lower()
    return "link" if value == "link" else "imessage"


def sms_link(handle: str, text: str) -> str:
    """Opens the Messages app with the recipient and text filled in; the user taps Send."""
    return f"sms:{handle}?&body={quote(text, safe='')}"


async def send(req: MessageSendRequest) -> MessageSendResponse:
    if not authorized(req.executor_token):
        return _refused("The iMessage sender is not configured or authorized. Nothing was sent.")
    if delivery() == "link":
        return _send_link(req)
    dry = dry_run()
    if not dry and sys.platform != "darwin":
        return _refused("iMessage sending needs macOS with the Messages app. Nothing was sent.")
    draft = _claim(req)
    if isinstance(draft, MessageSendResponse):
        return draft
    argv = osascript_argv(draft["handle"], draft["text"])
    if dry:
        masked = mask(draft["handle"])
        dry_run_log.append(
            {
                "argv": argv,
                "recipient": draft["name"],
                "recipient_masked": masked,
                "text": draft["text"],
                "at": datetime.now(UTC).isoformat(),
            }
        )
        log.info("iMessage dry run: would exec osascript for draft %s to %s", req.draft_id, masked)
        return MessageSendResponse(
            status="sent",
            dry_run=True,
            summary=f"Dry run: would have sent to {draft['name']}. No message was sent.",
        )
    try:
        code, stderr = await run_osascript(argv)
    except (TimeoutError, OSError):
        log.info("iMessage draft %s: osascript did not finish", req.draft_id)
        return _refused(
            "Messages did not respond. Check the Messages app before sending again."
        )
    if code == 0:
        log.info("iMessage draft %s sent", req.draft_id)
        return MessageSendResponse(
            status="sent", summary=f"Sent your iMessage to {draft['name']}."
        )
    error_code, reason = applescript_error(stderr, draft["handle"], draft["text"])
    if (
        error_code in PERMISSION_ERRORS
        or "-1743" in stderr
        or "not authorized to send apple events" in stderr.lower()
    ):
        log.info("iMessage draft %s: Automation permission missing (%s)", req.draft_id, error_code)
        return MessageSendResponse(
            status="needs_permission",
            summary=(
                "macOS blocked control of Messages. Open System Settings > Privacy & "
                "Security > Automation, allow the app running the agents to control "
                "Messages, then prepare the message again."
            ),
        )
    log.info("iMessage draft %s: osascript exited with %s (%s)", req.draft_id, code, error_code)
    hint = ERROR_HINTS.get(
        error_code,
        "Check that you are signed in to iMessage and the contact can receive iMessages.",
    )
    detail = f" Messages said: {reason}" if reason else ""
    return _refused(f"Messages could not send this to {draft['name']}. {hint}{detail}")


# -1743: not allowed to send Apple events; -1744: the event would need the user's consent.
PERMISSION_ERRORS = {"-1743", "-1744"}
ERROR_HINTS = {
    "-1728": "Messages could not find that contact on iMessage. Check the number or email.",
    "-1719": "Messages could not find that contact on iMessage. Check the number or email.",
    "-600": "Messages is not running. Open the Messages app and try again.",
    "-10000": "Messages rejected the request. Check that iMessage is signed in and enabled.",
}
APPLESCRIPT_ERROR = re.compile(r"(?:^\s*\d+:\d+:\s*)?(?:execution error:\s*)?(.*?)\s*\((-?\d+)\)\s*$")


def applescript_error(stderr: str, handle: str, text: str) -> tuple[str | None, str]:
    """('-1728', 'Messages got an error: …') from osascript's stderr, handle and text removed.

    The reason is shown to the owner, so the handle is masked and the message body dropped.
    """
    line = next((ln for ln in reversed(stderr.strip().splitlines()) if ln.strip()), "")
    match = APPLESCRIPT_ERROR.match(line)
    code, reason = (match[2], match[1]) if match else (None, line.strip())
    if handle:
        reason = reason.replace(handle, mask(handle))
    if text:
        reason = reason.replace(text, "…")
    return code, reason[:200]


def _send_link(req: MessageSendRequest) -> MessageSendResponse:
    """The approved draft as an Open-in-Messages link, with the same draft checks."""
    draft = _claim(req)
    if isinstance(draft, MessageSendResponse):
        return draft
    log.info("iMessage draft %s: approved as a Messages link to %s", req.draft_id, mask(draft["handle"]))
    return MessageSendResponse(
        status="open_link",
        open_url=sms_link(draft["handle"], draft["text"]),
        summary=f"Approved. Open it in Messages and tap Send to text {draft['name']}.",
    )


def read_dry_run_log(req: DryRunLogRequest) -> DryRunLogResponse:
    """Recent would-be sends, with handles masked. Dry-run mode and the token only."""
    if not dry_run():
        return DryRunLogResponse(dry_run=False, error="Only available in dry-run mode")
    if not authorized(req.executor_token):
        return DryRunLogResponse(dry_run=True, error="Not authorized")
    calls = []
    for call in list(dry_run_log)[-5:]:
        argv = list(call["argv"])
        argv[3] = call["recipient_masked"]
        calls.append({k: v for k, v in call.items() if k != "argv"} | {"argv": argv})
    return DryRunLogResponse(dry_run=True, calls=calls)
