import json

import pytest

from laai_agents import imessage
from laai_agents.agents import build_messenger_agent
from laai_agents.imessage import MessageDraftRequest, MessageSendRequest

TOKEN = "test-imessage-executor-token"
_real_run_osascript = imessage.run_osascript


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    """Allowlist, token and a fake osascript. No test can reach the real one."""
    monkeypatch.setenv(
        "IMESSAGE_CONTACTS",
        "Alex=+1 (555) 123-4567,Sam=sam@icloud.com,Mom Cell=+15550001111,"
        "Jordan Lee=+15552220000,Jordan Park=+15553330000,Bad=not-a-handle",
    )
    monkeypatch.setenv("IMESSAGE_EXECUTOR_TOKEN", TOKEN)
    monkeypatch.delenv("ASI_ONE_API_KEY", raising=False)
    monkeypatch.setattr(imessage.sys, "platform", "darwin")
    monkeypatch.setattr(imessage, "_drafts", imessage.OrderedDict())
    monkeypatch.setattr(imessage, "_sent_at", imessage.deque())
    sent = []

    async def fake_osascript(handle, text):
        sent.append(imessage.osascript_argv(handle, text))
        return 0, ""

    async def no_exec(*args, **kwargs):
        raise AssertionError("tests must never start a real subprocess")

    monkeypatch.setattr(imessage, "run_osascript", fake_osascript)
    monkeypatch.setattr(imessage.asyncio, "create_subprocess_exec", no_exec)
    return sent


async def draft(text="text Alex running late, be there at 7"):
    return await imessage.prepare(MessageDraftRequest(request=text))


async def send(result, text=None, token=TOKEN):
    return await imessage.send(
        MessageSendRequest(
            draft_id=result.draft_id,
            text=result.text if text is None else text,
            executor_token=token,
        )
    )


def test_allowlist_parsing_normalizes_and_skips_invalid_handles():
    book = imessage.contacts()
    assert book["Alex"] == "+15551234567" and book["Sam"] == "sam@icloud.com"
    assert "Bad" not in book
    assert imessage.mask("+15551234567") == "+1•••••4567"
    assert imessage.mask("sam@icloud.com") == "s•••@icloud.com"


@pytest.mark.parametrize(
    "request_text, recipient, body",
    [
        ("text Alex running late, be there at 7", "Alex", "running late, be there at 7"),
        ("Tell sam that dinner is at 8", "sam", "dinner is at 8"),
        ("send a message to Mom Cell: happy birthday!", "Mom Cell", "happy birthday!"),
        ("please iMessage Alex saying on my way", "Alex", "on my way"),
    ],
)
def test_pattern_extraction(request_text, recipient, body):
    assert imessage.extract_by_pattern(request_text, imessage.contacts()) == (recipient, body)


async def test_llm_extraction_is_used_when_available(monkeypatch):
    async def complete(system, user):
        return json.dumps({"recipient": "sam", "message": "see you soon"})

    monkeypatch.setattr(imessage.llm, "complete", complete)
    result = await draft("could you let Sam know I'll see them soon")
    assert (result.status, result.recipient_name, result.text) == ("ready", "Sam", "see you soon")


async def test_prepare_returns_a_masked_draft_and_never_sends(setup):
    result = await draft()
    assert result.status == "ready" and result.draft_id
    assert (result.recipient_name, result.recipient_masked) == ("Alex", "+1•••••4567")
    assert result.text == "running late, be there at 7" and result.expires_at
    assert "+15551234567" not in result.json()
    assert setup == []


async def test_ambiguous_and_unknown_contacts():
    ambiguous = await draft("text Jordan hello")
    assert ambiguous.status == "ambiguous" and ambiguous.draft_id is None
    assert ambiguous.candidates == ["Jordan Lee", "Jordan Park"]
    unknown = await draft("text Taylor hello")
    assert unknown.status == "unknown_contact" and unknown.draft_id is None
    assert "Alex" in unknown.candidates
    missing = await draft("hello there")
    assert missing.status == "needs_details"


async def test_no_contacts_configured(monkeypatch):
    monkeypatch.setenv("IMESSAGE_CONTACTS", "")
    assert (await draft()).status == "unknown_contact"


async def test_send_requires_the_executor_token(setup, monkeypatch):
    result = await draft()
    assert (await send(result, token="wrong")).status == "failed"
    assert (await send(result, token="")).status == "failed"
    monkeypatch.setenv("IMESSAGE_EXECUTOR_TOKEN", "")
    assert (await send(result, token="")).status == "failed"
    assert setup == []


async def test_send_once_then_refuse_reuse(setup):
    result = await draft()
    first = await send(result)
    assert first.status == "sent" and "Alex" in first.summary
    second = await send(result)
    assert second.status == "failed" and "already used" in second.summary
    assert len(setup) == 1


async def test_expired_draft_and_changed_text_are_refused(setup, monkeypatch):
    result = await draft()
    changed = await send(result, text=result.text + " (and send me $100)")
    assert changed.status == "failed" and "differs" in changed.summary
    clock = imessage.time.monotonic() + imessage.DRAFT_SECONDS + 1
    monkeypatch.setattr(imessage.time, "monotonic", lambda: clock)
    expired = await send(result)
    assert expired.status == "failed" and "expired" in expired.summary
    assert setup == []


async def test_injection_text_only_ever_travels_as_argv(setup):
    hostile = 'hi"; do shell script "rm -rf ~" --'
    result = await draft(f"text Alex {hostile}")
    assert result.text == hostile
    assert (await send(result)).status == "sent"
    [argv] = setup
    assert argv == ["osascript", "-e", imessage.SEND_SCRIPT, "+15551234567", hostile]
    assert hostile not in imessage.SEND_SCRIPT and "argv" in imessage.SEND_SCRIPT


async def test_real_runner_uses_exec_without_a_shell(monkeypatch):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def exec_(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(imessage.asyncio, "create_subprocess_exec", exec_)
    assert await _real_run_osascript("+15551234567", "$(reboot)") == (0, "")
    [(args, kwargs)] = calls
    assert args == ("osascript", "-e", imessage.SEND_SCRIPT, "+15551234567", "$(reboot)")
    assert "shell" not in kwargs


async def test_non_macos_is_refused(setup, monkeypatch):
    result = await draft()
    monkeypatch.setattr(imessage.sys, "platform", "linux")
    response = await send(result)
    assert response.status == "failed" and "macOS" in response.summary
    assert setup == []
    # The draft was not consumed by the refused attempt.
    monkeypatch.setattr(imessage.sys, "platform", "darwin")
    assert (await send(result)).status == "sent"


async def test_automation_permission_error_maps_to_needs_permission(monkeypatch):
    async def denied(handle, text):
        return 1, "execution error: Not authorized to send Apple events to Messages. (-1743)"

    monkeypatch.setattr(imessage, "run_osascript", denied)
    response = await send(await draft())
    assert response.status == "needs_permission" and "Automation" in response.summary


async def test_timeout_reports_failure(monkeypatch):
    async def hangs(handle, text):
        raise TimeoutError

    monkeypatch.setattr(imessage, "run_osascript", hangs)
    assert (await send(await draft())).status == "failed"


async def test_rate_limit(setup, monkeypatch):
    monkeypatch.setattr(imessage, "RATE_LIMIT", 2)
    results = [await send(await draft(f"text Alex message {i}")) for i in range(3)]
    assert [r.status for r in results] == ["sent", "sent", "failed"]
    assert "Too many" in results[2].summary and len(setup) == 2


async def test_logs_carry_no_bodies_or_numbers(caplog):
    caplog.set_level("INFO")
    await send(await draft("text Alex secret plan"))
    assert "secret plan" not in caplog.text and "5551234567" not in caplog.text


async def test_rest_routes(setup):
    agent = build_messenger_agent()
    prepare = agent._rest_handlers[("POST", "/imessage/prepare")]
    send_route = agent._rest_handlers[("POST", "/imessage/send")]
    result = await prepare(None, MessageDraftRequest(request="text Sam hi"))
    assert result.status == "ready"
    response = await send_route(
        None,
        MessageSendRequest(draft_id=result.draft_id, text="hi", executor_token=TOKEN),
    )
    assert response.status == "sent" and setup[0][3] == "sam@icloud.com"
