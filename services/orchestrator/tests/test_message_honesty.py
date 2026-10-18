import pytest

from laai_orchestrator.chat import NO_DRAFT, _is_message_request, laptop_honest
from laai_orchestrator.models import AgentCall


@pytest.mark.parametrize(
    "text",
    [
        "Text legend. I'm testing the voice flow.",
        "text legend. running late",
        "Tell legend; I'm outside",
        "Text Legend! see you soon",
    ],
)
def test_a_name_ending_in_punctuation_is_still_a_message(text):
    assert _is_message_request(text)


@pytest.mark.parametrize(
    "reply",
    [
        "Drafted a message to Legend for you to review.",
        "I've sent your text to Legend.",
        "Your message is ready to send.",
    ],
)
def test_a_claimed_draft_without_the_messenger_is_replaced(reply):
    assert laptop_honest(reply, []) == NO_DRAFT


def test_a_real_draft_keeps_the_reply_and_other_replies_are_untouched():
    draft = AgentCall(kind="message", payload={}, result={"text": "hi"}, error=None, ms=1)
    assert laptop_honest("Drafted a message to Legend.", [draft]) == "Drafted a message to Legend."
    assert laptop_honest("The AirPods Pro 3 are $199 at Amazon.", []) == "The AirPods Pro 3 are $199 at Amazon."
    assert laptop_honest("Sent from my iPhone reviews say it is great.", []) == "Sent from my iPhone reviews say it is great."


def test_message_words_quoted_from_a_news_result_are_kept():
    from laai_orchestrator.chat import laptop_honest
    from laai_orchestrator.models import AgentCall, AgentKind

    summary = "SpaceX sent the Starship to orbit today."
    news = AgentCall(kind=AgentKind.NEWS, payload={}, result={"summary": summary}, ms=0)
    assert laptop_honest(summary, [news]) == summary
    # A model claim with no source behind it is still replaced.
    assert laptop_honest("I sent the message to Legend.", [news]) != "I sent the message to Legend."
