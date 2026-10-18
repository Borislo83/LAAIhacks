"""Run the agents.

    uv run laai-agents                 # every agent in one Bureau on :8000
    uv run laai-agents --demo          # same, plus a client that queries each agent once
    uv run laai-agents --only reviews --port 8003   # one agent on its own (e.g. with AGENTVERSE_MAILBOX=1)
"""

import argparse

from dotenv import load_dotenv
from uagents import Bureau

load_dotenv()

from . import llm
from .agents import (
    build_alternatives_agent,
    build_messenger_agent,
    build_news_agent,
    build_price_agent,
    build_reviews_agent,
    build_spotify_agent,
    build_youtube_agent,
)
from .buyer import build_buyer_agent
from .carts import build_amazon_cart_agent, build_bestbuy_cart_agent
from .memory import build_memory_agent
from .verifier import build_verifier_agent

BUILDERS = {
    "price": build_price_agent,
    "alternatives": build_alternatives_agent,
    "reviews": build_reviews_agent,
    "youtube": build_youtube_agent,
    "spotify": build_spotify_agent,
    "messenger": build_messenger_agent,
    "news": build_news_agent,
    "buyer": build_buyer_agent,
    "amazon-cart": build_amazon_cart_agent,
    "bestbuy-cart": build_bestbuy_cart_agent,
    "verifier": build_verifier_agent,
    "memory": build_memory_agent,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="LAAI Fetch.ai agents")
    parser.add_argument("--only", choices=BUILDERS, help="run a single agent")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true", help="add a client that queries every agent")
    args = parser.parse_args()

    print(f"ASI:One LLM: {'on' if llm.has_key() else 'off (set ASI_ONE_API_KEY in .env)'}")

    if args.only:
        agent = BUILDERS[args.only](port=args.port)
        if agent is None:
            raise SystemExit(f"{args.only}: refused to start (see the warning above).")
        print(f"{agent.name}: {agent.address}")
        agent.run()
        return

    # A builder returns None when it refuses to start (e.g. Spotify on a public seed).
    agents = [agent for build in BUILDERS.values() if (agent := build()) is not None]
    from .coordinator import build_coordinator

    agents.append(build_coordinator({a.name: a.address for a in agents}))
    bureau = Bureau(port=args.port, endpoint=[f"http://127.0.0.1:{args.port}/submit"])
    for agent in agents:
        print(f"{agent.name}: {agent.address}")
        bureau.add(agent)
    if args.demo:
        from .demo import build_demo_client

        bureau.add(build_demo_client({a.name: a.address for a in agents}))
    bureau.run()


if __name__ == "__main__":
    main()
