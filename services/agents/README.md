# Agents

Fetch.ai [uAgents](https://uagents.fetch.ai/docs) for the glasses. There are three specialists and a coordinator:

| Agent | Agent protocol | REST | What it does |
|---|---|---|---|
| `laai-price-compare` | `PriceCompareRequest` → `PriceCompareResponse` | `POST /price` | Prices across stores, and how much you'd save vs the current page |
| `laai-alternatives` | `AlternativesRequest` → `AlternativesResponse` | `POST /alternatives` | Cheaper or better-rated competitors in the same category |
| `laai-reviews` | `ReviewsRequest` → `ReviewsResponse` | `POST /reviews` | Rating, pros/cons, a review link, and a one-line summary |

Every agent also speaks the **Agent Chat Protocol**, so ASI:One and Agentverse chat can talk to it in plain language.

Product data defaults to online search. Set `SEARCH_PROVIDER=ddgs` for keyless online
search, `auto` to try ASI:One search with DDGS fallback, or `asi` to require ASI search.
`PRODUCT_DATA_MODE=demo` explicitly selects the illustrative fixture catalog. Live failures
never fall back to fixture prices. Every result identifies its data source, retrieval time,
source links, and limitations. Structured offers are read only from reviewed HTTPS merchant domains, without redirects. Search results can be old or incomplete; prices are leads,
not verified checkout offers. Missing numeric evidence stays missing.

ASI search must report a completed search and URL citations; unsupported responses do
not count as live evidence. DDGS needs no API key, but public search can rate-limit it.
Run `uv run python check_search.py` to diagnose both providers without printing secrets.
See [ASI search documentation](https://docs.asi1.ai/documentation/build-with-asi-one/web-search)
and [DDGS](https://pypi.org/project/ddgs/).

## Focal coordinator

The Bureau also starts `focal-shopping-assistant`, a fourth uAgent with ACP and
`POST /consider`. It sends typed messages to the three specialist uAgents concurrently,
waits for their responses, and combines results, sources and individual failures.

```bash
curl http://127.0.0.1:8000/consider -H 'Content-Type: application/json' \
  -d '{"product":"Consider Sony XM5 and recommend an alternative under $300"}'
```

Or provide structured input: `{"product":"Sony XM5","max_price":300}`. The dashboard's
chat routes `consider/evaluate/assess` and explicit price+review+alternative requests
to the coordinator. "Consider this product" without an actual product name asks for it;
the browser integration supplies the selected page's product identity.

Known catalog names/categories seed candidate identity only. Live mode looks up prices
again and never uses fixture prices or ratings. Unknown categories return research links
rather than invented alternatives. Candidates need a matching in-stock USD structured merchant offer
strictly below the requested budget. Ratings remain absent unless backed by a source.

The local API is a development surface. It executes research immediately on an explicit
request; it does not represent a glasses approval. For G2, use the approval-gated contract
client below. The local Bureau is not production-authenticated and should not be exposed.

## Shared glasses contract v1.0

[`integration.py`](src/laai_agents/integration.py) is a client of the Glasses Team's API,
not another API or database. Configure `GLASSES_API_URL` and separate `web`, `agent`,
and `executor` tokens in `.env`. Hosted API URLs require HTTPS.

```bash
uv run python -m laai_agents.integration --journal /tmp/focal-sony.json propose \
  --page ../../demo/sony-page.json --product 'Sony XM5' --budget 300
# After the user approves an exact action in G2:
uv run python -m laai_agents.integration --journal /tmp/focal-sony.json poll
```

Use `--session SESSION_ID` when the browser has already created the session. Page and
action request IDs are retained across retries. The client proposes two allowed actions:
`compare_prices` (explicitly labeled prices AND alternatives) and `find_reviews`.
No `custom` action or unregistered handler is needed. Each approval authorizes only its
own research operation; the client has no approve endpoint or G2 token.

`poll` processes one batch of ordered approval events. It checks session/page/action identity,
active display and approved state, and expiry, then posts running and completed/failed results.
It stores the outcome before posting it and advances its cursor only after acceptance.
An API outage therefore retries the same result/request ID after restart. Run polling through
your executor scheduler. CLI journal locking allows only one worker per journal.

Use a new journal for each product/page/budget context. Journals contain page data but no
tokens; delete them when the corresponding session is retired. The Glasses Team owns stale
revision checks, approval idempotency, page invalidation and event ordering/outbox persistence.
The DB team owns durable production storage; this local journal is an adapter fixture, not
a competing schema. Read-only research may repeat if the process crashes before journaling
its outcome; do not extend it to purchases/navigation without a durable execution ledger.

The contract does not fix the events response envelope: this adapter accepts a list or
`{"events":[...]}`. It does not invent an acknowledgment endpoint. Confirm the envelope,
result message size limit and outbox acknowledgment semantics with the Glasses/DB teams.
Live API integration needs their URL and tokens; tests use a contract fixture.

## Setup

```bash
uv sync
```

```bash
cp .env.example .env
```

`ASI_ONE_API_KEY` is optional. You can get one at [asi1.ai](https://asi1.ai) (Developer → Create New). With a key, the agents use ASI:One to write review summaries and to pick the product name out of chat messages. Without one, they use the catalog text as is.

## Run

The coordinator and three specialists in one Bureau on port 8000:

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

For the complete workflow, set a private `COORDINATOR_AGENT_SEED` and
`COORDINATOR_AGENTVERSE_MAILBOX=1`, then start the normal Bureau (all four agents).
Connect **focal-shopping-assistant** to your Agentverse account using its inspector link,
publish its shopping capabilities, and test the full request from ASI:One. Specialists can
remain in the local Bureau; the coordinator delegates typed messages to them.
Mailbox/account connection and ASI:One discovery require a real end-to-end account test;
local execution and successful manifest publication alone do not prove discoverability.

To expose an individual specialist instead:

1. Run the agent on its own with a mailbox: `AGENTVERSE_MAILBOX=1 uv run laai-agents --only reviews --port 8003`.
2. Open the inspector link it prints and connect it to your Agentverse account.
3. Give it a good name and description on Agentverse so ASI:One can find it.

Before deploying anywhere public, set your own seeds in `.env`. The seeds fix each agent's address, and the defaults are dev values that are in this repo.

## Test

```bash
uv run pytest
```
