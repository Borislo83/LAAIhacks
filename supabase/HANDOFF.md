# DB team handoff (shared integration contract v1.0)

The DB team owns the Supabase Postgres schema and migrations for the shared session workflow.
This page explains what each team gets and how to plug in. Contract terms follow
[`apps/glasses/INTEGRATION.md`](../apps/glasses/INTEGRATION.md) v1.0.

## Status

| Migration | What it adds | Live project `hjdogsjwgnbdlxcspkaf` |
|---|---|---|
| `202610170001_orchestrator.sql` | Settings, contexts, tasks (dashboard) | Applied |
| `202610170002_shared_workflow.sql` | Contract tables, `laai_workflow` RPC | Applied (do not rerun) |
| `202610170003_user_preferences.sql` | `user_preferences` + private notes | Applied (do not rerun) |
| `202610170004_workflow_v1_1.sql` | Event ack, invalidation outbox, devices, rate-limit fix | Applied 2026-10-17 |
| `202610170005_workflow_perf.sql` | Lock/index/retention fixes (no API change) | Applied 2026-10-17 (pg_cron not installed: daily purge not scheduled) |
| `202610170006_session_list.sql` | `sessions` read operation for `GET /v1/sessions` (page watcher) | Applied 2026-10-18 (SQL editor) |
| `202610170007_cart_action.sql` | Registers the `add_to_amazon_cart` action type; display's page carries `url` | Applied 2026-10-18 (SQL editor) |

004, 005 and 006 only use `create or replace` / `if [not] exists`, so applying any of them
twice is harmless. 006 replaces only the `laai_workflow` entry point (rate limit unchanged)
and adds `laai_workflow_sessions`; deploy it before an orchestrator that serves
`GET /v1/sessions`, or that route returns `SESSION_NOT_FOUND`. Apply in the Supabase SQL editor, then run `python check_supabase.py` in
`services/orchestrator` (read-only; it reports whether 004 is live).
007 is rerunnable too: it swaps the `laai_actions.type` check and replaces
`laai_workflow_apply` (005's body plus the new type and display's `page.url`). Until it is
live, the watcher proposes without the cart action.

005 keeps `laai_workflow`'s signature and every response shape. It changes:
- Reads (`status`, `display`, `events`, `ack`) no longer lock the session row; only writes do.
  `display` is one statement, so its revision, page and actions always come from one snapshot.
- The rate-limit counter is read without a lock before the operation and bumped after it, so a
  user's calls no longer queue on their bucket row. Still shared, still 600/user/min, failed and
  refused requests still count; concurrent in-flight calls can overshoot by their number.
- Drops `laai_session_owner_revision` (revision in an index made every write non-HOT) and the
  unused `laai_events_unconsumed`; adds `laai_sessions_user_expiry (user_id, expires_at)` for
  preference saves and Auth-user cascades.
- Adds `laai_purge_expired()` and schedules it daily (03:17 UTC) if pg_cron is installed.

## Contract tables → Supabase

| Contract table | Supabase | Notes |
|---|---|---|
| `users` | `auth.users` | Supabase Auth is the user registry; no second users table. |
| `devices` | `laai_devices` | Register with the `device` operation (004). |
| `sessions` | `laai_sessions` | 24 h expiry; `revision` bumps on every state change. |
| `pages` | `laai_pages` | Keyed `(session_id, id)`; `summary_json`, `facts_json`. |
| `actions` | `laai_actions` | Status `proposed→approved/rejected→running→completed/failed`, or `invalidated`. |
| `approval_events` | `laai_approval_events` | Transactional outbox; `cursor` is an identity column. |
| `idempotency_keys` | `laai_idempotency_keys` | Unique `(session_id, request_id, operation)`; stores request + response. |
| `user_preferences` | `laai_user_preferences` | Plus `notes` (≤10,000 chars, never sent to models). |

The contract's indexes exist (actions session+page+status, events cursor, idempotency
composite). Its session+revision index is served by the primary key (sessions are always looked
up by id, and revision is read from that row); 005 drops the extra `(user_id, id, revision)` index
because it made every revision bump a non-HOT update. Every query is scoped to the verified user. Direct client access is
revoked; only the service role can call the functions. Deleting an Auth user cascades all their data.

Retention (from 005): `laai_purge_expired()` removes sessions expired over 30 days (and their
pages, actions, events and idempotency keys) plus dashboard `laai_contexts`/`laai_tasks` created
over 30 days ago, and returns `{sessions, contexts, tasks}` counts. If the pg_cron extension is
installed when 005 is applied, it is scheduled as job `laai-purge-expired`; otherwise nothing is
scheduled. To schedule it later, enable pg_cron (Database → Extensions) and run
`select cron.schedule('laai-purge-expired', '17 3 * * *', 'select public.laai_purge_expired()');`,
or run `select public.laai_purge_expired();` by hand in the SQL editor.

## Glasses team: repository adapter

The persistence entry point is one Postgres function, called with the service-role key through
PostgREST (`POST /rest/v1/rpc/laai_workflow`) or any Postgres driver:

```
laai_workflow(p_user_id uuid, p_operation text, p_session_id uuid, p_payload jsonb) → jsonb
```

Pass the **verified** caller's user ID. Never take it from a request body. Each call is one
transaction: state, revision, idempotency record and outbox event commit together or not at all.
On failure it returns `{"error": {"code", "message"}, "status": <HTTP status>}` and writes
nothing except the rate-limit counter.

| `p_operation` | Session? | Payload | Returns |
|---|---|---|---|
| `device` | no | `{}` or `{"device_id"}` | `{device_id, last_seen_at}` |
| `create` | no | `{}` or `{"device_id"}` | `{schema_version, session_id, revision}` |
| `status` | yes | `{}` | `{session_id, revision, active_page_id, expires_at}` |
| `page` | yes | contract `PageSubmission` | `{schema_version, session_id, revision}` |
| `actions` | yes | contract actions submission | `{schema_version, session_id, revision}` |
| `display` | yes | `{"after_revision"}` | contract `DisplayState`, or `{"unchanged": true}` → 204 |
| `approve` / `reject` | yes | `{request_id, expected_revision, action_id}` | `{action_id, status, event_id}` |
| `events` | yes | `{"after": cursor}` | `{"events": [...]}` (max 100, cursor order) |
| `ack` | yes | `{"event_id"}` | `{event_id, cursor, consumed_at}` |
| `result` | yes | `{request_id, action_id, status, message, result_url?}` | `{action_id, status}` |

`page`, `actions`, `approve`, `reject` and `result` need a `request_id`. A retry with the same
input returns the stored response; the same `request_id` with different input returns `CONFLICT`.

Rules enforced in the database, so your adapter does not have to:
- Approvals are rejected for unknown (`ACTION_NOT_FOUND`), stale (`STALE_REVISION`), expired
  (`ACTION_EXPIRED`), already-decided or old-page (`CONFLICT`) and other-session actions.
- A new page snapshot, even with the same `page_id`, invalidates all earlier actions, and from
  004 also closes their unconsumed events.
- Action IDs can't be reused. Proposals that are already expired are rejected.
- Results are accepted only for approved/running actions. `completed`/`failed` mark the event consumed.
- 600 operations per user per minute across all API instances (`RATE_LIMITED`, 429).

Error codes beyond the contract list: `DEVICE_NOT_FOUND` and `EVENT_NOT_FOUND` (404).

`services/orchestrator/src/laai_orchestrator/workflow.py` is a working reference adapter
(FastAPI). It exposes the contract endpoints plus `POST /v1/devices` (g2) and
`POST /v1/sessions/{id}/events/{event_id}/ack` (executor). Use it as-is, or port the
`operate()` call into your own API.

## Web team

- Preferences: `GET`/`PUT /v1/preferences` with a signed-in Supabase token. Fields:
  `display_density` (`comfortable|compact`), `shopping_priority` (`balanced|price|quality`),
  `proactive_enabled`, `notes`. Saving a change bumps the revision of the user's active sessions,
  so G2 sees it on the next display poll. `DisplayState.preferences` includes only `display_density`.
- Pages: `POST /v1/sessions`, then `PUT .../page` with a fresh `request_id` per snapshot.

## Agent / executor team

- Propose with `PUT .../actions`. This release registers `compare_prices`,
  `find_reviews` and (from 007) the approval-gated `add_to_amazon_cart`. `open_url`, `read_article` and `custom` are rejected with `INVALID_PAYLOAD`
  until a reviewed handler (and, for `open_url`, a destination allowlist) is registered.
- Poll `GET .../events?after=<cursor>`. From 004 each event carries `action_status`. Skip events
  whose status is `invalidated`: they will never accept a result, and the DB has already closed
  them. `services/agents/src/laai_agents/integration.py` skips them (it re-checks `display`
  before running) and advances its cursor.
- Delivery is at-least-once. Post results with stable request IDs (`<event_id>:outcome`) and
  optionally `ack` the event once you are done with it.

## Running locally (all teams)

1. `cd services/orchestrator && cp .env.example .env`, then fill in the Supabase URL and keys.
2. To skip Google sign-in, set `DEMO_AUTH_MODE=true` and `DEMO_USER_ID=<an existing Auth user id>`
   (create one under Supabase → Authentication → Users). Requests from this machine can then use:

   | Bearer token | Roles |
   |---|---|
   | `demo-web` | web |
   | `demo-g2` | g2 |
   | `demo-agent` | agent |
   | `demo-executor` | executor |
   | `demo-all` | all four |

   The tokens are public fixtures. They're refused from any non-loopback address, through a
   proxy (`X-Forwarded-For`/`Forwarded`), or when demo mode is off. Never enable it on a host.
3. `uv sync && uv run laai-orchestrator` (port 8001)

```bash
curl -s -X POST localhost:8001/v1/sessions -H 'Authorization: Bearer demo-web'
```

## Tests

- `cd supabase/tests && npm test`: runs the real migration SQL in embedded Postgres (PGlite).
  Every behavior suite runs twice, against the live schema (001-004) and with 005 on top;
  `workflow_perf.test.mjs` covers what 005 changes and `session_list.test.mjs` what 006 adds. Shared setup is in `tests/helpers.mjs`.
- `cd services/orchestrator && uv run pytest -q`: API, roles, error envelope, demo auth.
- `cd services/agents && uv run pytest -q`: executor journal, retries, invalidated approvals.
