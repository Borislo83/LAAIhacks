# Shared glasses workflow v1.0 (+ DB v1.1)

Apply `supabase/migrations/202610170002_shared_workflow.sql` after the original orchestrator
migration. This adds the shared session API without replacing the existing task/history API.
The Glasses app should consume the session API; the orchestrator owns its persistent adapter.
Supabase Auth supplies the canonical users table. Devices, sessions, pages, actions, approval
events, idempotency responses and request buckets live in the `laai_*` tables.

## Authentication and roles

All `/v1/sessions` endpoints verify a Supabase access token with Auth. The server scopes
**every** database operation to that verified user. Ordinary users have the `web` role.
A trusted administrator can set `app_metadata.laai_roles` to a list containing `web`,
`g2`, `agent`, and/or `executor` on a Supabase Auth identity. Do not put roles in user-editable
`user_metadata`, accept roles from request headers, or grant roles from the dashboard.
Integration callers need roles and ownership for the same user's sessions; unrelated service
accounts cannot access them. Token issuance/delegation belongs to the trusted Auth backend.
For local development only, `DEMO_AUTH_MODE=true` with `DEMO_USER_ID` set to an existing
Auth user accepts fixture tokens (`demo-web`, `demo-g2`, `demo-agent`, `demo-executor`,
`demo-all`) from loopback clients without forwarding headers. Never enable it when hosted.

The role mapping matches the shared contract. Owners can also read
`GET /v1/sessions/{id}` for session status. `/healthz` is public process liveness.
Use the configured CORS allowlist and HTTPS when hosting; keep the upstream Bureau private.
A database-backed bucket limits shared workflow operations to 600 per user per minute across
API instances; from migration 004, failed requests count too. A deployment gateway can enforce a stricter IP limit before authentication.

## Request and response details

- Session IDs are UUIDs. A path with a non-UUID session ID returns 404 `SESSION_NOT_FOUND`.
- `schema_version` is optional on every request body and defaults to `"1.0"`; any other
  value is `INVALID_PAYLOAD`. This includes action results.
- Approve and reject take the same body:
  `{"schema_version":"1.0","request_id":"...","expected_revision":7}`. A rejection returns
  `event_id: null` and emits no event.
- `confirm` mode is G2-local (the glasses show it while the user confirms); the API never
  returns it. `reader` is reserved until a `read_article` handler ships. `notice` is
  currently always `null`.
- Only `compare_prices` and `find_reviews` are registered. `open_url`, `read_article` and
  `custom` pass schema validation but are rejected when proposed (422 `INVALID_PAYLOAD`).
- Every route documents its allowed role(s), typed response and `ErrorEnvelope` errors in
  `packages/shared/openapi.json` (regenerate with `uv run python export_schema.py`).
  Responses keep database extension fields such as `consumed_at`, `action_status`,
  `source_label`, `type`, `expires_at` and `result`.
- Unknown sub-routes return `NOT_FOUND` and wrong methods `METHOD_NOT_ALLOWED`.
- If preferences cannot be read, the display is still returned without `preferences`.
- Verified access tokens are cached in-process for 30 seconds (by SHA-256 hash, at most
  1,024 entries) so G2 polling does not call Supabase Auth on every request. Failed
  verifications are never cached; a revoked token can stay usable for up to 30 seconds.

## Durable state

`laai_workflow` locks the owned session and commits state, revision increments, idempotency
records, and approval outbox entries together. A `(session_id, request_id, operation)` retry
returns the same response; reusing a key with different input returns `CONFLICT`.
Page updates and replacement proposals invalidate earlier actions. IDs cannot be resurrected.
Approvals require the current revision and a proposed, unexpired action on the active page.
Only registered `compare_prices` and `find_reviews` handlers can be proposed for this release.
Other contract type names are recognized but rejected until a reviewed handler is registered.

`GET /v1/sessions` (`agent` or `executor` role, migration 006) lists the caller's unexpired
sessions as `{"sessions":[{session_id, revision, active_page_id, updated_at, expires_at}]}`,
newest activity first, at most 100. It takes no lock and is rate limited like every other
operation. Agent-side watchers compare revisions to notice page PUTs without a per-page event.

`GET .../events?after=cursor` returns `{"events": [...]}` in cursor order, at most 100 at a time.
Cursors are 19-digit strings. Reads never consume events. Terminal result acceptance marks the
corresponding event consumed in the same transaction as the saved result and display revision.
Events remain replayable with the original cursor until session retention deletes them;
clients advance their durable cursor **only after** the result is accepted. A result retry
must reuse its original request ID. With migration 004, executors can also acknowledge an
event with `POST .../events/{event_id}/ack` (idempotent, no revision change), events include
the action's current `action_status`, and invalidating an approved action marks its event
consumed so it never needs delivery. See [the DB team handoff](../../supabase/HANDOFF.md).

This gives at-least-once delivery; it does not promise exactly-once external execution.
The existing executor journal protects result retries; a crash during research can repeat
read-only work. Do not extend it to purchases or navigation without a durable execution ledger.

Sessions expire after 24 hours. The explicit, service-role-only
`laai_purge_expired_sessions()` maintenance function deletes sessions that expired over 30 days
ago and cascades their pages, actions, events and idempotency records. Schedule it with the
hosting platform or Supabase Cron. Deleting an Auth user also cascades their application data.

Run `npm test` in `supabase/tests` and `pytest -q` in `services/orchestrator`. Database tests
execute real migration SQL in embedded PostgreSQL; API tests mock the Supabase HTTP boundary.
A live G2 handoff still needs the migration deployed and real role-scoped user tokens.

## Preferences and notes

Apply `202610170003_user_preferences.sql` after the session migration. Authenticated users
read and replace their own record with `GET`/`PUT /v1/preferences`. The payload contains
`display_density` (`comfortable|compact`), `shopping_priority` (`balanced|price|quality`),
`proactive_enabled`, and `notes` (up to 10,000 characters). Responses also include `updated_at`.
Reads initialize defaults once and never overwrite saved values. Supabase owns persistence;
there is no in-memory fallback. Deleting the Auth user deletes their preferences and notes.

Page-context predictions respect `proactive_enabled` (default false). Explicit chat and Direct
requests still run. Shopping priority orders suggested specialists and coordinator cards;
ASI:One also receives the structured priority as recommendation context. Explicit user requests
and budgets take precedence. Notes are private storage and are never sent to an agent/model.
The signed-in dashboard loads preferences before enabling Save, keeps text after save failures,
and clears it on sign-out. Display density applies to the Studio; session display responses
include only `preferences.display_density` for G2. Changed preferences increment active owned
session revisions atomically so a polling display sees updates; unchanged saves do not.

Deployment check: `python check_supabase.py` performs read-only Auth/table checks without
printing credentials. Run it after applying both migrations and reloading the PostgREST schema.
Never deploy the new API before applying the migrations: unavailable persistence returns 503,
and the dashboard keeps Save disabled rather than pretending data was saved.
