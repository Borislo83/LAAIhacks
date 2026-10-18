import pytest

from laai_orchestrator.chat import wants_playback


@pytest.mark.parametrize(
    "text",
    [
        "Hey, Foko! Play Blinding Lights on YouTube for me. We realized we didn't bring our.",
        "Hey, Foco. Open up Blinding Lights on YouTube for me.",
        "open blinding lights on youtube",
        "pull up the Espresso music video on YouTube",
        "Hey Focal, play Espresso on YouTube",
        "play Bad Guy",
        "Hey play blinding lights on youtube",
    ],
)
def test_play_and_open_on_youtube_play_it(text):
    assert wants_playback(text)


@pytest.mark.parametrize(
    "text",
    [
        "find me a youtube link for Blinding Lights",
        "Is the Apple Watch worth it? find a youtube review",
        "open up the AirPods page on Apple",
        "how to start a car youtube video",
        "send me the youtube url for espresso",
    ],
)
def test_links_and_other_requests_do_not_play(text):
    assert not wants_playback(text)
