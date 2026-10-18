# Agents

Fetch.ai [uAgents](https://uagents.fetch.ai/docs) for the glasses. There are five specialists and a coordinator:

| Agent | Agent protocol | REST | What it does |
|---|---|---|---|
| `laai-price-compare` | `PriceCompareRequest` → `PriceCompareResponse` | `POST /price` | Prices across stores, and how much you'd save vs the current page |
| `laai-alternatives` | `AlternativesRequest` → `AlternativesResponse` | `POST /alternatives` | Cheaper or better-rated competitors in the same category |
| `laai-reviews` | `ReviewsRequest` → `ReviewsResponse` | `POST /reviews` | Rating, pros/cons, a review link, and a one-line summary |
| `laai-youtube` | `YouTubeRequest` → `YouTubeResponse` | `POST /youtube` | A YouTube link for a song or video the user asks for |
| `laai-messenger` | `MessageDraftRequest` → `MessageDraftResponse` | `POST /imessage/prepare`, `POST /imessage/send` | Drafts an iMessage to an allowlisted friend; sends only after approval |

Every agent also speaks the **Agent Chat Protocol**, so ASI:One and Agentverse chat can talk to it in plain language.

Product data defaults to online search. Set `SEARCH_PROVIDER=ddgs` for keyless online
search, `auto` to try ASI:One search with DDGS fallback, or `asi` to require ASI search.
`PRODUCT_DATA_MODE=demo` explicitly selects the illustrative fixture catalog. Live failures
never fall back to fixture prices. Every result identifies its data source, retrieval time,
source links, and limitations. Prices are gathered broadly, then ranked by trust. Any public HTTPS search result
that names the product is a candidate: its JSON-LD offers (Offer or AggregateOffer), price
meta tags, or a single clear dollar price in the search snippet. Each price is labeled
`high` (major retailer/brand store + read from the page), `medium` (one of the two) or
`low` (neither), and the best price is the cheapest in the most trusted tier available.
Only clear mismatches are dropped: other currencies, explicit out-of-stock or used
listings, installments, ranges, other model variants and price outliers. Pages are
fetched read-only, redirects are re-checked, and private network hosts are refused.
Search results can be old or incomplete; prices are leads,
not verified checkout offers. Missing numeric evidence stays missing.

ASI search must report a completed search and URL citations; unsupported responses do
not count as live evidence. DDGS needs no API key, but public search can rate-limit it.
Run `uv run python check_search.py` to diagnose both providers without printing secrets.
See [ASI search documentation](https://docs.asi1.ai/documentation/build-with-asi-one/web-search)
and [DDGS](https://pypi.org/project/ddgs/).

## YouTube agent

`laai-youtube` turns a request like `find the song "Bohemian Rhapsody"` or `youtube link
for how to tie a tie` into one video link. It is separate from the shopping research.

```bash
curl http://127.0.0.1:8000/youtube -H 'Content-Type: application/json' \
  -d '{"query":"find the song Blinding Lights","kind":"auto"}'
```

`kind` is `song`, `video` or `auto` (the default). `auto` means `song` when the request
mentions a song, track, music or audio, and `video` otherwise. The agent strips request
phrasing ("find the song", "youtube link for", "play") or uses a quoted title as is. For
songs it adds "official audio" to the search. The response has `title`, `url`, `channel`,
`duration` (when known), up to three `alternatives` and a plain-text `summary` (title,
channel, link), which is also its chat reply.

It searches the YouTube Data API first only when `YOUTUBE_API_KEY` is set. Otherwise it
uses keyless DDGS video search, then web search for `site:youtube.com`. Only single-video
links are returned, always as `https://www.youtube.com/watch?v=<11-character id>`
(`youtu.be` links are rewritten). Shorts, playlists, channels and other sites are dropped,
and no URL from the query or the results is fetched. No match gives `url: null` with a
summary that says so. If every provider fails, `error` is set and no link is guessed.

## iMessage agent

`laai-messenger` sends a message to a friend through the Messages app on the Mac running
the agents. It works in two steps, like the Amazon cart agent:

1. `POST /imessage/prepare` with `{"request":"text Alex running late, be there at 7"}`
   finds the recipient and the message (ASI:One when configured, otherwise a pattern for
   "text/message/tell <name> <message>"). It returns `draft_id`, `recipient_name`,
   `recipient_masked` (for example `+1•••••4567`), `text`, `expires_at` (5 minutes) and a
   summary. It never sends. Chat and the typed protocol only prepare drafts.
2. After the user approves the draft, the backend calls `POST /imessage/send` with
   `{"draft_id":"…","text":"<the exact draft text>","executor_token":"…"}`.

Recipients come only from `IMESSAGE_CONTACTS`, for example
`Alex=+15551234567,Sam=sam@icloud.com`. Names match case-insensitively. A name that
matches more than one contact returns `ambiguous` with the candidates, and an unknown
name returns `unknown_contact`; neither creates a draft.

Sending requires `IMESSAGE_EXECUTOR_TOKEN` (compared in constant time; set it in the
agents and orchestrator `.env` files and never send it to the browser), an unexpired
draft, and text identical to the draft. Each draft is used once, even if Messages fails,
so an uncertain send is never repeated automatically. Messages are 1–1000 characters and
at most 10 are sent per minute. Sending only works on macOS.

The agent runs `osascript` directly (no shell) with a 15-second timeout. The script is
fixed; the handle and the message are passed as arguments, never written into the script.
The first send asks macOS for permission to control Messages. If it was denied, the result
is `needs_permission`: allow it under System Settings > Privacy & Security > Automation.
Results are `sent`, `failed` or `needs_permission`. Message text and phone numbers are not
written to info-level logs.

## Live dashboard progress

Research requests may include an opaque `progress_id`. The coordinator forwards it to
the specialist uAgents; search, offer validation and summary steps emit actual work events.
`POST /progress` with `{"progress_id":"<32-character random ID>","after":0}` returns ordered
events for that request. Events are ephemeral, capped and retained for up to ten minutes.
The dashboard backend relays them over authenticated server-sent events. They describe
operations and results, not a model's private reasoning. No simulated percentage is shown.

## Amazon cart agent

The normal Bureau also starts `focal-amazon-buyer-demo`. `POST /buy/prepare` accepts either
`{"product":"Sony XM5","quantity":1}` or
`{"url":"https://www.amazon.com/dp/B0B5VHRX7F","quantity":1}`. Product names search online
for Amazon listings; the user chooses a specific listing before approving a cart addition.
Links must be HTTPS Amazon US product URLs with an ASIN; tracking parameters are discarded.

The review returns a short-lived `confirmation_token`. After explicit approval, the backend
calls `POST /buy/confirm` with that token and its private `executor_token`. Set the same
`AMAZON_CART_EXECUTOR_TOKEN` in agents and orchestrator `.env` files; never send it to the
browser. The local environments are configured; the example files contain no credentials.

The executor launches a dedicated visible Chrome window through Playwright. It reads the
real Amazon cart, checks the approved listing's ASIN and quantity, clicks **Add to Cart**,
and verifies the ASIN is visible in the active cart. `cart_added` is returned only with that
evidence. Existing items are not duplicated; their quantity must be reviewed on Amazon.
An uncertain attempt is never automatically repeated. Unknown/expired approvals require a
new review. There is no checkout, payment, Buy Now, or order-submission operation.

Chrome must be installed on the machine running the agents. The profile lives in the ignored
`.runtime/amazon-profile` directory (override `AMAZON_BROWSER_PROFILE` if needed). It does not
read your normal browser profile. Sign in or complete Amazon verification manually in this
window if asked, then prepare and approve again. Signed-out carts remain in that dedicated
browser session; a dashboard link opened in another browser profile would not share it.
The real cart remains visible in the agent's Chrome window. Prices and totals must be
reviewed on Amazon. The cloud-hosted browser option is not configured.

On the dashboard, type `Buy Sony XM5 on Amazon`, or paste an Amazon product link. Choose a
listing, then use **Add to real Amazon cart** or type `confirm add to cart`. Quantity is also
available in Direct mode. No buyer execution is implied by a research recommendation.

## Coordinator workflow

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
rather than invented alternatives. Candidates need a trust-ranked USD price
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

The coordinator, the four specialists and the Amazon cart agent in one Bureau on port 8000:

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
