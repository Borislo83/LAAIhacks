"""Read-only provider diagnostics; prints no keys or request headers."""

import asyncio

from dotenv import load_dotenv
from laai_agents.search import asi_search, free_search


async def main():
    for name, search in (("asi", asi_search), ("ddgs", free_search)):
        try:
            sources = await search(
                "Sony WH-1000XM5 current price USD independent review"
            )
            print(name, "OK", len(sources), "sources")
            for source in sources[:3]:
                print(source.model_dump())
        except Exception as exc:  # noqa: BLE001 -- diagnostic reports both providers independently
            print(name, "FAILED", type(exc).__name__, str(exc)[:180])


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(main())
