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
    monkeypatch.setenv("IMESSAGE_DRY_RUN", "0")  # dry-run tests switch it back on
    monkeypatch.delenv("IMESSAGE_DELIVERY", raising=False)  # link tests set it themselves
    monkeypatch.setattr(imessage, "dry_run_log", imessage.deque(maxlen=20))
    monkeypatch.delenv("ASI_ONE_API_KEY", raising=False)
    monkeypatch.setattr(imessage.sys, "platform", "darwin")
    monkeypatch.setattr(imessage, "_drafts", imessage.OrderedDict())
    monkeypatch.setattr(imessage, "_sent_at", imessage.deque())
    sent = []

    async def fake_osascript(argv):
        sent.append(argv)
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
    assert (result.status, result.recipient_name, result.text) == ("ready", "Sam", "See you soon.")


async def test_llm_extraction_drops_a_leading_linking_word(monkeypatch):
    async def complete(system, user):
        return json.dumps({"recipient": "sam", "message": "that the glasses demo works"})

    monkeypatch.setattr(imessage.llm, "complete", complete)
    result = await draft("text Sam that the glasses demo works")
    assert result.text == "The glasses demo works."


async def test_prepare_returns_a_masked_draft_and_never_sends(setup):
    result = await draft()
    assert result.status == "ready" and result.draft_id
    assert (result.recipient_name, result.recipient_masked) == ("Alex", "+1•••••4567")
    assert result.text == "Running late, be there at 7." and result.expires_at
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
    hostile = 'Hi"; do shell script "rm -rf ~" --'
    result = await draft(f"text Alex {hostile}")
    assert result.text == hostile  # already capitalized and ending in punctuation
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
    assert await _real_run_osascript(imessage.osascript_argv("+15551234567", "$(reboot)")) == (0, "")
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
    async def denied(argv):
        return 1, "execution error: Not authorized to send Apple events to Messages. (-1743)"

    monkeypatch.setattr(imessage, "run_osascript", denied)
    response = await send(await draft())
    assert response.status == "needs_permission" and "Automation" in response.summary


async def test_timeout_reports_failure(monkeypatch):
    async def hangs(argv):
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
        MessageSendRequest(draft_id=result.draft_id, text=result.text, executor_token=TOKEN),
    )
    assert response.status == "sent" and setup[0][3] == "sam@icloud.com"


def test_dry_run_is_on_unless_explicitly_disabled(monkeypatch):
    monkeypatch.delenv("IMESSAGE_DRY_RUN")
    assert imessage.dry_run()
    for value in ("1", "yes", "", "true"):
        monkeypatch.setenv("IMESSAGE_DRY_RUN", value)
        assert imessage.dry_run()
    for value in ("0", "false", "off", "No"):
        monkeypatch.setenv("IMESSAGE_DRY_RUN", value)
        assert not imessage.dry_run()


async def test_dry_run_records_the_exact_call_and_never_executes(setup, monkeypatch):
    monkeypatch.delenv("IMESSAGE_DRY_RUN")
    monkeypatch.setattr(imessage.sys, "platform", "linux")  # works anywhere
    hostile = 'Hi"; do shell script "rm -rf ~"'
    result = await draft(f"text Alex {hostile}")
    response = await send(result)
    assert (response.status, response.dry_run) == ("sent", True)
    assert response.summary.startswith("Dry run: would have sent to Alex")
    assert setup == []  # neither the fake runner nor create_subprocess_exec ran
    [call] = imessage.dry_run_log
    assert call["argv"] == imessage.osascript_argv("+15551234567", hostile)
    # A real send of the same draft would execute exactly this argv.
    monkeypatch.setenv("IMESSAGE_DRY_RUN", "0")
    monkeypatch.setattr(imessage.sys, "platform", "darwin")
    real = await draft(f"text Alex {hostile}")
    await send(real)
    assert setup == [call["argv"]]


async def test_dry_run_enforces_every_check(setup, monkeypatch):
    monkeypatch.setenv("IMESSAGE_DRY_RUN", "1")
    result = await draft()
    assert (await send(result, token="wrong")).status == "failed"
    assert (await send(result, text="changed")).status == "failed"
    assert (await send(result)).dry_run
    reused = await send(result)
    assert reused.status == "failed" and "already used" in reused.summary
    monkeypatch.setattr(imessage, "RATE_LIMIT", 1)
    assert "Too many" in (await send(await draft())).summary
    stale = await draft()
    clock = imessage.time.monotonic() + imessage.DRAFT_SECONDS + 1
    monkeypatch.setattr(imessage.time, "monotonic", lambda: clock)
    assert "expired" in (await send(stale)).summary
    assert len(imessage.dry_run_log) == 1 and setup == []


async def test_dry_run_log_route_masks_handles_and_needs_the_token(setup, monkeypatch):
    monkeypatch.setenv("IMESSAGE_DRY_RUN", "1")
    await send(await draft("text Alex see you at 7"))
    route = build_messenger_agent()._rest_handlers[("POST", "/imessage/dry-run-log")]
    denied = await route(None, imessage.DryRunLogRequest(executor_token="wrong"))
    assert denied.error and not denied.calls
    log = await route(None, imessage.DryRunLogRequest(executor_token=TOKEN))
    [call] = log.calls
    assert call["argv"][3] == "+1•••••4567" and call["text"] == "See you at 7."
    assert "5551234567" not in log.json()
    monkeypatch.setenv("IMESSAGE_DRY_RUN", "0")
    off = await route(None, imessage.DryRunLogRequest(executor_token=TOKEN))
    assert off.error and not off.calls


async def test_contact_without_text_asks_what_to_say(monkeypatch):
    from laai_agents import imessage

    monkeypatch.setenv("IMESSAGE_CONTACTS", "Jonathan=+12137930070")

    async def no_llm(*args, **kwargs):
        return None

    monkeypatch.setattr(imessage.llm, "complete", no_llm)
    reply = await imessage.prepare(imessage.MessageDraftRequest(request="Hey send a message to Jonathan"))
    assert reply.status == "needs_message" and reply.draft_id is None
    assert reply.summary == "What would you like to say to Jonathan?"
    draft = await imessage.prepare(
        imessage.MessageDraftRequest(request="text Jonathan running 10 minutes late")
    )
    assert draft.status == "ready" and draft.text == "Running 10 minutes late."


async def test_link_delivery_returns_messages_link_once(monkeypatch):
    from laai_agents import imessage

    monkeypatch.setenv("IMESSAGE_CONTACTS", "Jonathan=+12137930070")
    monkeypatch.setenv("IMESSAGE_EXECUTOR_TOKEN", "t" * 32)
    monkeypatch.setenv("IMESSAGE_DELIVERY", "link")

    async def no_llm(*args, **kwargs):
        return None

    monkeypatch.setattr(imessage.llm, "complete", no_llm)
    draft = await imessage.prepare(imessage.MessageDraftRequest(request="text Jonathan see you at 7"))
    send = imessage.MessageSendRequest(draft_id=draft.draft_id, text=draft.text, executor_token="t" * 32)
    first = await imessage.send(send)
    assert first.status == "open_link"
    assert first.open_url == "sms:+12137930070?&body=See%20you%20at%207."
    assert (await imessage.send(send)).status == "failed"  # single use
    wrong = imessage.MessageSendRequest(draft_id=draft.draft_id, text=draft.text, executor_token="x" * 32)
    assert (await imessage.send(wrong)).status == "failed"


def test_send_name_a_message_phrasing_is_parsed():
    from laai_agents.imessage import extract_by_pattern, tidy

    book = {"Jonathan": "+12135550100", "Alex": "+12135550101"}
    recipient, body = extract_by_pattern('Send jonathan a message "lol"', book)
    assert recipient.lower() == "jonathan" and tidy(body) == "Lol"
    assert extract_by_pattern("shoot Alex a quick text saying I'm running late", book) == (
        "Alex", "I'm running late",
    )


async def test_two_word_contact_resolves_from_first_name_or_full_name():
    book = imessage.contacts() | {"Legend Yang": "+15554445672"}
    assert imessage.extract_by_pattern("text Legend I'm testing my glasses", book) == (
        "Legend",
        "I'm testing my glasses",
    )
    assert imessage.extract_by_pattern("text Legend Yang I'm testing my glasses", book) == (
        "Legend Yang",
        "I'm testing my glasses",
    )
    assert imessage.resolve("Legend", book) == ["Legend Yang"]
    assert imessage.resolve("legend yang", book) == ["Legend Yang"]


async def test_first_name_draft_targets_the_full_contact(setup, monkeypatch):
    monkeypatch.setenv("IMESSAGE_CONTACTS", "Legend Yang=+1 555 444 5672,Alex=+15551234567")
    result = await draft("text Legend I'm testing my glasses")
    assert result.status == "ready" and result.recipient_name == "Legend Yang"
    assert result.recipient_masked == "+1•••••5672"
    assert result.text == "I'm testing my glasses."
    assert (await send(result)).status == "sent"
    assert setup[-1][3:] == ["+15554445672", "I'm testing my glasses."]


async def test_dry_run_records_the_target_handle(setup, monkeypatch):
    monkeypatch.setenv("IMESSAGE_DRY_RUN", "1")
    monkeypatch.setenv("IMESSAGE_CONTACTS", "Legend Yang=+15554445672")
    response = await send(await draft("text Legend I'm testing my glasses"))
    assert response.status == "sent" and response.dry_run and "Legend Yang" in response.summary
    assert setup == []  # nothing executed
    assert imessage.dry_run_log[-1]["argv"][3] == "+15554445672"


@pytest.mark.parametrize(
    "stderr, hint",
    [
        (
            "0:120: execution error: Messages got an error: "
            "Can’t get participant id \"+15551234567\". (-1728)",
            "could not find that contact",
        ),
        (
            "12:40: execution error: Messages got an error: AppleEvent handler failed. (-10000)",
            "iMessage is signed in",
        ),
        ("execution error: Messages isn’t running. (-600)", "not running"),
    ],
)
async def test_applescript_errors_are_surfaced_without_the_handle(monkeypatch, stderr, hint):
    async def fails(argv):
        return 1, stderr

    monkeypatch.setattr(imessage, "run_osascript", fails)
    response = await send(await draft())
    assert response.status == "failed"
    assert hint in response.summary and "Messages said:" in response.summary
    assert "5551234567" not in response.summary
    if "+15551234567" in stderr:
        assert "+1•••••4567" in response.summary


async def test_applescript_error_drops_message_text():
    code, reason = imessage.applescript_error(
        "1:2: execution error: Can’t send “secret plan” to +15551234567. (-1708)",
        "+15551234567",
        "secret plan",
    )
    assert code == "-1708"
    assert "secret plan" not in reason and "+15551234567" not in reason and "+1•••••4567" in reason


async def test_consent_required_maps_to_needs_permission(monkeypatch):
    async def needs_consent(argv):
        return 1, "execution error: Not authorized. (-1744)"

    monkeypatch.setattr(imessage, "run_osascript", needs_consent)
    assert (await send(await draft())).status == "needs_permission"
