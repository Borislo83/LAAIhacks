# Shared glasses workflow v1.0

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
No anonymous demo mode is enabled for this API.

The role mapping matches the shared contract. Owners can also read
`GET /v1/sessions/{id}` for session status. `/healthz` is public process liveness.
Use the configured CORS allowlist and HTTPS when hosting; keep the upstream Bureau private.
A database-backed bucket limits shared workflow operations to 600 per user per minute across
API instances. A deployment gateway can enforce a stricter IP limit before authentication.

## Durable state

`laai_workflow` locks the owned session and commits state, revision increments, idempotency
records, and approval outbox entries together. A `(session_id, request_id, operation)` retry
returns the same response; reusing a key with different input returns `CONFLICT`.
Page updates and replacement proposals invalidate earlier actions. IDs cannot be resurrected.
Approvals require the current revision and a proposed, unexpired action on the active page.
Only registered `compare_prices` and `find_reviews` handlers can be proposed for this release.
Other contract type names are recognized but rejected until a reviewed handler is registered.

`GET .../events?after=cursor` returns `{"events": [...]}` in cursor order, at most 100 at a time.
Cursors are 19-digit strings. Reads never consume events. Terminal result acceptance marks the
corresponding event consumed in the same transaction as the saved result and display revision.
Events remain replayable with the original cursor until session retention deletes them;
clients advance their durable cursor **only after** the result is accepted. There is no
separate acknowledgment endpoint. A result retry must reuse its original request ID.

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
