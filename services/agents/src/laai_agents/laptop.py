"""Open a YouTube video in the laptop's default browser, for "play X" asked from the glasses.

POST /youtube/open needs LAPTOP_EXECUTOR_TOKEN, a private token shared with the
orchestrator (never with the browser or glasses). Only an exact
https://www.youtube.com/watch?v=<11-char id> URL is accepted, and it is opened with
`open <url>` as an argv list, never through a shell. LAPTOP_OPEN_DRY_RUN=1 records the
request without opening anything.
"""

import asyncio
import os
import re
import secrets
import sys
from typing import Literal

from pydantic.v1 import Field, SecretStr
from uagents import Model

WATCH_URL = re.compile(r"https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}")
OPEN_TIMEOUT = 10


class YouTubeOpenRequest(Model):
    url: str = Field(max_length=60)
    executor_token: SecretStr = SecretStr("")


class YouTubeOpenResponse(Model):
    status: Literal["opened", "dry_run", "not_configured", "refused", "failed"]
    summary: str
    url: str | None = None


def dry_run() -> bool:
    return os.getenv("LAPTOP_OPEN_DRY_RUN", "0").strip().lower() in {"1", "true", "yes", "on"}


def authorized(token: SecretStr) -> bool:
    expected = os.getenv("LAPTOP_EXECUTOR_TOKEN", "")
    return bool(expected) and secrets.compare_digest(
        expected.encode(), token.get_secret_value().encode()
    )


def open_argv(url: str) -> list[str]:
    return ["open", url]


async def run_open(argv: list[str]) -> int:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), OPEN_TIMEOUT)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise


async def open_youtube(req: YouTubeOpenRequest) -> YouTubeOpenResponse:
    if not os.getenv("LAPTOP_EXECUTOR_TOKEN", ""):
        return YouTubeOpenResponse(
            status="not_configured",
            summary="Opening videos on the laptop isn't set up (LAPTOP_EXECUTOR_TOKEN is empty).",
        )
    if not authorized(req.executor_token):
        return YouTubeOpenResponse(status="refused", summary="Not authorized to open videos on the laptop.")
    if not WATCH_URL.fullmatch(req.url):
        return YouTubeOpenResponse(status="refused", summary="Only YouTube watch links can be opened.")
    if dry_run():
        return YouTubeOpenResponse(
            status="dry_run", url=req.url, summary="Dry run: the video was not opened on the laptop."
        )
    if sys.platform != "darwin":
        return YouTubeOpenResponse(status="failed", url=req.url, summary="Opening videos needs the Mac laptop.")
    try:
        code = await run_open(open_argv(req.url))
    except (OSError, TimeoutError):
        code = None
    if code != 0:
        return YouTubeOpenResponse(status="failed", url=req.url, summary="Couldn't open the browser on the laptop.")
    return YouTubeOpenResponse(status="opened", url=req.url, summary="Opened the video on the laptop.")
