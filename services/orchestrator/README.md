# Orchestrator

Backend intelligence, task prediction, agent dispatch, and **Supabase persistence** for LAAIhacks.
The dashboard and glasses call this API; the API calls the existing Fetch.ai agents on port 8000.

## Setup

1. In Supabase SQL Editor, run [`202610170001_orchestrator.sql`](../../supabase/migrations/202610170001_orchestrator.sql).
2. From this folder, run `uv sync` and copy `.env.example` to `.env` if `.env` does not exist.
3. Set `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `SUPABASE_SECRET_KEY` in `.env`.
   Keep the secret key in the backend only. New secret keys and legacy service-role JWTs work.
4. Enable a sign-in provider in Supabase Auth. Clients obtain a Supabase session and send its
   **access token**, not an API key, to this backend.
5. Start the agents from `services/agents` with `uv run laai-agents`, then start this service:

```bash
uv run laai-orchestrator
```

After applying the migration and setting keys, run `uv run python check_supabase.py` for a
read-only check of Auth and all three database tables.

The backend listens on port **8001**, with API docs at `http://localhost:8001/docs`.

Dashboard progress uses authenticated `POST /v1/chat/stream` and
`POST /v1/agents/{kind}/stream` server-sent events. `progress` events identify the working
agent and its current operation; `result` carries the final response. The stream sends
heartbeats, cancels its relay on disconnect, and never automatically retries execution.
Ordinary JSON `/v1/chat` remains available. Progress is ephemeral and separate from the
Glasses Team's durable approval events.

The Amazon buying demo uses `POST /v1/buyer/prepare/stream` or chat. It accepts a product
name or Amazon product link and shows a review for a real cart addition.
Chat confirmation requires both an explicit affirmative message and the preparation's
`purchase_confirmation` token. The backend adds its private `AMAZON_CART_EXECUTOR_TOKEN`
and delegates to the local visible Chrome executor. The selected ASIN must be verified in
the active cart before success is reported. The executor stops before checkout; no Amazon
order or payment occurs. Browser profiles are local to the agents process.

iMessage drafts come from chat ("text Alex I'm late"; the LLM's `message_friend` tool or the
rules fallback), which only calls the agent's `/imessage/prepare` and returns the draft in
`calls[].result`. Nothing is sent from chat. After the user reviews it, the client calls
`POST /v1/messages/send {draft_id, text}`. That route always requires a verified sign-in
with the `web` role (even with `CHAT_AUTH_REQUIRED=false`), allows 5 sends per user per minute,
and adds the private `IMESSAGE_EXECUTOR_TOKEN`; it returns 503 while that is unset. Responses
contain only `status` (`sent|failed|needs_permission`) and `summary`.
Set `AGENTS_URL` for a remote agent Bureau and `CORS_ORIGINS` for frontend origins.
Optional `ASI_ONE_API_KEY` enables ASI:One page understanding. Without it, an explicit `product`
field produces deterministic shopping suggestions; unstructured pages produce no tasks.
Invalid or failed LLM responses use the same fallback.

## API

All `/v1` endpoints require `Authorization: Bearer <Supabase access token>`.
The server verifies tokens with Supabase Auth and derives the user ID from that response.
Request bodies cannot supply a user ID. Expired tokens return 401; clients refresh the session.

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Public process liveness; does not check Supabase readiness |
| GET | `/v1/settings` | Read saved settings, initializing defaults once |
| PUT | `/v1/settings` | Replace settings; omitted fields take defaults |
| POST | `/v1/contexts` | Save a page and predict allowed tasks, atomically |
| GET | `/v1/tasks?limit=20&offset=0` | Newest tasks and results for the current user |
| GET | `/v1/tasks/{id}` | Poll a task and retrieve its result |
| POST | `/v1/tasks/{id}/dispatch` | Claim a predicted task and await its agent result |
| POST | `/v1/chat` | Plain-language chat; ASI:One picks which agents to call |

`/v1/chat` takes `{"messages": [{"role": "user", "content": "..."}]}` (up to 20 turns) and returns
`reply`, `source` (`asi_one`, `rules`, or `coordinator`), and `calls`: each agent's `kind`, `payload`, `result`,
`error`, and `ms`. ASI:One chooses tools, and the backend validates every argument before calling
an agent. A store or price the user never typed is dropped. Without a key, keywords choose agents.
Chat saves nothing. For local prototyping only, `CHAT_AUTH_REQUIRED=false` skips the sign-in.

"Consider/evaluate/assess this product" requests with a product name, or explicit requests
for prices, reviews and alternatives, route to the Focal coordinator's `/consider` endpoint.
It delegates typed messages to all three specialist uAgents and forwards the alternative
budget. Responses include sources, timestamps and per-specialist failures. This research
testing path is separate from G2 approval; the [contract client](../agents/README.md#shared-glasses-contract-v10)
submits proposals to the Glasses Team API and runs approved research from its event stream.

Example context body:

```json
{
  "url": "https://www.bestbuy.com/",
  "title": "Sony WH-1000XM5",
  "product": "Sony XM5",
  "current_store": "Best Buy",
  "current_price": 399.99
}
```

The response contains `context_id`, `prediction_source` (`asi_one`, `rules`, or `none`), and saved
`tasks`. Each task has an `id`, `kind`, `status`, and agent `payload`. Dispatch a chosen task with
`{"confirmed": true}`. Use `result.summary` for the glasses overlay; render result links as
user-selectable actions. The backend does not automatically navigate to page or agent URLs.

Example settings body (also the defaults):

```json
{
  "enabled_agents": ["price", "alternatives", "reviews"],
  "require_confirmation": true,
  "projection_enabled": true,
  "text_scale": 1
}
```

Glasses clients apply `projection_enabled` and `text_scale`. Disabled agents are removed from new
predictions and cannot dispatch existing tasks. Setting `require_confirmation` to false permits
explicit dispatch without confirmation; it does not automatically execute predictions.

## Persistence and execution

Supabase stores settings, page snapshots, predictions, timestamps, errors, and results.
Backend queries are scoped to the verified user. RLS permits signed-in clients to read only
their own rows; clients cannot write tables or call orchestration RPCs directly. Database RPCs
are restricted to the backend's service role.

Tasks follow `predicted → running → succeeded | failed`. A database row lock makes claiming atomic
across backend processes and rechecks current permissions and confirmation before dispatch.
Repeat dispatch returns 409; missing or another user's task returns 404; permission denial returns
403. Agent failures are saved as `failed`. Persistence failures return 503 without temporary-storage
fallback. Page and prediction inserts commit together.

Dispatch waits for the agent inside the HTTP request; there is no durable worker queue yet.
If the process stops after claiming or final persistence fails, the task can remain `running`.
Inspect its state and upstream outcome before an operator marks it failed; never blindly reset
it to `predicted`. Disconnected clients should poll before trying another dispatch.
New page submissions create new contexts; task claims, rather than page ingestion, are deduplicated.

Page snapshots include submitted URL and text. Clients should send only content needed for
understanding. Deleting a Supabase Auth user cascades through all their stored data.

## Tests

```bash
uv run pytest -q
uv run ruff check src tests
```

HTTP tests use mock Supabase and agent transports. Database tests run the actual migration in
embedded PostgreSQL with stub Supabase Auth objects:

```bash
cd ../../supabase/tests
npm ci
npm test
```

Database tests validate SQL, constraints, transactions, grants, RLS, and state transitions.
They do not simulate multiple database connections or verify a live Supabase project.

See [`PLAN.md`](PLAN.md) for remaining integration work and
[`packages/shared`](../../packages/shared/README.md) for the API contract.
Supabase references: [API keys](https://supabase.com/docs/guides/getting-started/api-keys),
[API security](https://supabase.com/docs/guides/api/securing-your-api).
