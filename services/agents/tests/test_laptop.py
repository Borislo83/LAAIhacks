import pytest

from laai_agents import laptop
from laai_agents.agents import build_youtube_agent
from laai_agents.laptop import YouTubeOpenRequest, open_youtube

TOKEN = "laptop-test-token"
WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture
def opened(monkeypatch):
    """Records argv instead of running `open`. No browser is touched."""
    runs = []

    async def fake_run(argv):
        runs.append(argv)
        return 0

    monkeypatch.setattr(laptop, "run_open", fake_run)
    monkeypatch.setattr(laptop.sys, "platform", "darwin")
    monkeypatch.setenv("LAPTOP_EXECUTOR_TOKEN", TOKEN)
    monkeypatch.delenv("LAPTOP_OPEN_DRY_RUN", raising=False)
    return runs


async def test_opens_an_exact_watch_url_with_an_argv_list(opened):
    result = await open_youtube(YouTubeOpenRequest(url=WATCH, executor_token=TOKEN))
    assert result.status == "opened" and result.url == WATCH
    assert opened == [["open", WATCH]]


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1",
        "https://www.youtube.com/watch?v=dQw4w9WgXc",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ; rm -rf ~",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ\n",
        "https://www.youtube.com/watch?v=$(whoami)xx",
        "https://www.youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
        "file:///etc/passwd",
        "-a Terminal",
        "",
    ],
)
async def test_refuses_anything_but_an_exact_watch_url(opened, url):
    result = await open_youtube(YouTubeOpenRequest(url=url, executor_token=TOKEN))
    assert result.status == "refused"
    assert opened == []


@pytest.mark.parametrize("token", ["", "wrong", TOKEN + "x"])
async def test_needs_the_executor_token(opened, token):
    result = await open_youtube(YouTubeOpenRequest(url=WATCH, executor_token=token))
    assert result.status == "refused" and opened == []


async def test_missing_token_is_not_configured(opened, monkeypatch):
    monkeypatch.delenv("LAPTOP_EXECUTOR_TOKEN")
    result = await open_youtube(YouTubeOpenRequest(url=WATCH, executor_token=""))
    assert result.status == "not_configured" and "LAPTOP_EXECUTOR_TOKEN" in result.summary
    assert opened == []


async def test_dry_run_opens_nothing(opened, monkeypatch):
    monkeypatch.setenv("LAPTOP_OPEN_DRY_RUN", "1")
    result = await open_youtube(YouTubeOpenRequest(url=WATCH, executor_token=TOKEN))
    assert result.status == "dry_run" and opened == []


async def test_failed_open_is_reported(opened, monkeypatch):
    async def fail(argv):
        return 1

    monkeypatch.setattr(laptop, "run_open", fail)
    result = await open_youtube(YouTubeOpenRequest(url=WATCH, executor_token=TOKEN))
    assert result.status == "failed"


async def test_off_mac_nothing_runs(opened, monkeypatch):
    monkeypatch.setattr(laptop.sys, "platform", "linux")
    result = await open_youtube(YouTubeOpenRequest(url=WATCH, executor_token=TOKEN))
    assert result.status == "failed" and opened == []


async def test_run_open_uses_exec_not_a_shell(monkeypatch):
    seen = {}

    class Process:
        async def wait(self):
            return 0

    async def fake_exec(*argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return Process()

    monkeypatch.setattr(laptop.asyncio, "create_subprocess_exec", fake_exec)
    assert await laptop.run_open(["open", WATCH]) == 0
    assert seen["argv"] == ("open", WATCH) and "shell" not in seen["kwargs"]


async def test_rest_route(opened):
    route = build_youtube_agent()._rest_handlers[("POST", "/youtube/open")]
    result = await route(None, YouTubeOpenRequest(url=WATCH, executor_token=TOKEN))
    assert result.status == "opened" and opened == [["open", WATCH]]


async def test_rest_route_refuses_a_wrong_token(opened):
    route = build_youtube_agent()._rest_handlers[("POST", "/youtube/open")]
    for token in ("wrong", ""):
        result = await route(None, YouTubeOpenRequest(url=WATCH, executor_token=token))
        assert result.status == "refused"
    assert opened == []
