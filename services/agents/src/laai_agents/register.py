"""Connect every Focal agent to Agentverse as a mailbox agent, in one go.

    uv run laai-register            # register all agents
    uv run laai-register --dry-run  # list names and addresses only

Needs AGENTVERSE_API_KEY in .env (agentverse.ai → Profile → API Keys). This does the
same as clicking "Connect → Mailbox" in each agent's inspector: it proves each agent's
identity to Agentverse and publishes its name, description, README and protocols.
Then run the agents with AGENTVERSE_MAILBOX=1 so they pick up their mail.
Seeds fix the addresses, so set private seeds in .env before registering.
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv

PROFILE_URL = "https://agentverse.ai/agents/details/{address}/profile"


def build_all():
    from .coordinator import build_coordinator
    from .main import BUILDERS

    agents = [build() for build in BUILDERS.values()]
    agents.append(build_coordinator({a.name: a.address for a in agents}))
    return agents


async def register(agent, token: str) -> tuple[bool, str]:
    from uagents.mailbox import AgentverseConnectRequest, register_in_agentverse
    from uagents_core.registration import RegistrationRequest

    details = RegistrationRequest(
        address=agent.address,
        name=agent.name,
        handle=agent._handle,
        profile=agent._build_registration_profile(),
        endpoints=agent._endpoints,
        protocols=list(agent.protocols.keys()),
        metadata=agent.metadata,
    )
    result = await register_in_agentverse(
        request=AgentverseConnectRequest(user_token=token, agent_type="mailbox"),
        identity=agent._identity,
        prefix=agent._prefix,
        agentverse=agent._agentverse,
        agent_details=details,
    )
    return result.success, result.detail or ""


async def run(dry_run: bool) -> int:
    token = os.getenv("AGENTVERSE_API_KEY", "").strip()
    if not token and not dry_run:
        print("Set AGENTVERSE_API_KEY in .env (agentverse.ai → Profile → API Keys).")
        return 1
    weak = [
        name
        for name in ("COORDINATOR", "PRICE", "ALTERNATIVES", "REVIEWS", "VERIFIER", "MEMORY",
                     "BUYER", "MESSENGER", "YOUTUBE", "NEWS", "AMAZON_CART", "BESTBUY_CART")
        if not os.getenv(f"{name}_AGENT_SEED")
    ]
    if weak and not dry_run:
        print("Set private seeds first (these use public dev seeds):", ", ".join(weak))
        return 1
    failures = 0
    for agent in build_all():
        if dry_run:
            print(f"{agent.name:26} {agent.address}")
            continue
        ok, detail = await register(agent, token)
        failures += not ok
        print(f"{'✓' if ok else '✗'} {agent.name:26} {PROFILE_URL.format(address=agent.address)}"
              + ("" if ok else f"  ({detail})"))
    if not dry_run:
        print(f"\n{'All agents registered.' if not failures else f'{failures} failed.'} "
              "Now run: AGENTVERSE_MAILBOX=1 uv run laai-agents")
    return 1 if failures else 0


def main() -> None:
    load_dotenv(".env")
    # Mailbox endpoints are what Agentverse should record for each agent.
    os.environ["AGENTVERSE_MAILBOX"] = "1"
    parser = argparse.ArgumentParser(description="Register all Focal agents on Agentverse")
    parser.add_argument("--dry-run", action="store_true", help="only list names and addresses")
    raise SystemExit(asyncio.run(run(parser.parse_args().dry_run)))


if __name__ == "__main__":
    main()
