# Agents

Fetch.ai [uAgents](https://uagents.fetch.ai/docs) for the glasses. There are three agents:

| Agent | Agent protocol | REST | What it does |
|---|---|---|---|
| `laai-price-compare` | `PriceCompareRequest` → `PriceCompareResponse` | `POST /price` | Prices across stores, and how much you'd save vs the current page |
| `laai-alternatives` | `AlternativesRequest` → `AlternativesResponse` | `POST /alternatives` | Cheaper or better-rated competitors in the same category |
| `laai-reviews` | `ReviewsRequest` → `ReviewsResponse` | `POST /reviews` | Rating, pros/cons, a review link, and a one-line summary |

Every agent also speaks the **Agent Chat Protocol**, so ASI:One and Agentverse chat can talk to it in plain language.

Product data comes from a mock catalog in `src/laai_agents/catalog.py`. To use real data, replace the functions in that file.

## Setup

```bash
uv sync
```

```bash
cp .env.example .env
```

`ASI_ONE_API_KEY` is optional. You can get one at [asi1.ai](https://asi1.ai) (Developer → Create New). With a key, the agents use ASI:One to write review summaries and to pick the product name out of chat messages. Without one, they use the catalog text as is.

## Run

All three agents in one Bureau on port 8000:

```bash
uv run laai-agents
```

The same, plus a client that sends one request to each agent and logs the replies:

```bash
uv run laai-agents --demo
```

How the orchestrator calls an agent over HTTP:

```bash
curl -X POST http://127.0.0.1:8000/price -H 'content-type: application/json' -d '{"product": "Sony XM5", "current_store": "Best Buy"}'
```

## Making an agent reachable from ASI:One

1. Run the agent on its own with a mailbox: `AGENTVERSE_MAILBOX=1 uv run laai-agents --only reviews --port 8003`.
2. Open the inspector link it prints and connect it to your Agentverse account.
3. Give it a good name and description on Agentverse so ASI:One can find it.

Before deploying anywhere public, set your own seeds in `.env`. The seeds fix each agent's address, and the defaults are dev values that are in this repo.

## Test

```bash
uv run pytest
```
