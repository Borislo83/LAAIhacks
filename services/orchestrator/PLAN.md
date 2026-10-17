# Backend implementation plan

## Scope and architecture

`services/orchestrator/` owns backend intelligence, task prediction, permissions, and persistence.
`services/agents/` owns Fetch.ai execution and product data. `apps/dashboard/` owns settings and
permission controls. `apps/glasses/` sends page context, chooses suggestions, and displays results.
The API contract lives in `packages/shared/openapi.json`, generated from the backend models.

```text
Dashboard / glasses ── Supabase Auth sign-in → user access token
        │
        └─ authenticated REST → FastAPI orchestrator (:8001)
                                  ├─ ASI:One understanding and task selection
                                  ├─ Supabase settings, contexts, tasks, results
                                  └─ permission-checked dispatch → Fetch.ai agents (:8000)
```

## Implemented

- Supabase migration for settings, contexts, and tasks, with ownership constraints and indexes.
- RLS for each user's reads; server-only writes and database RPCs.
- Supabase Auth verification, user scoping, input validation, and configured CORS.
- Optional ASI:One page understanding with validated task names and deterministic fallback.
- Saved suggestions for price comparisons, alternatives, and reviews using existing agent payloads.
- Atomic context/prediction persistence and task claiming with current permission checks.
- Saved results, failure states, and paginated history that survives backend restarts.
- Settings API for agent permissions and glasses projection preferences.
- API tests and embedded PostgreSQL tests, plus a shared OpenAPI export.

## Live project setup

Target Supabase project: `hjdogsjwgnbdlxcspkaf`. Its URL, publishable key, and backend server key
are configured in the ignored `services/orchestrator/.env` (owner-only file permissions).
Credentials are not committed. The existing server key was retrieved through the signed-in
Supabase browser dashboard; the backend supports its legacy service-role format.

The migration schema is present in the live project. Verified all expected columns in
`laai_settings`, `laai_contexts`, and `laai_tasks`, server access to the claim RPC, and rejection
of the same RPC for anonymous clients. `check_supabase.py` passes against live Auth and database
tables. The configured backend passes its local health and unauthenticated-access checks.
Verification did not create users or modify user rows.

Remaining client/deployment integration:

- Supply the backend server key through the hosting provider's secret manager when deploying.
- Enable a Supabase Auth provider and sign in from a client.
- Smoke test settings, context submission, and confirmed dispatch, then restart and fetch saved history.

## Client integration and later work

1. Dashboard: use Supabase Auth, read/replace settings through the API, display task history.
2. Glasses: send page context; present suggestions; dispatch the selected task and poll saved results.
   Apply display settings on the device. Polling is the initial transport; WebSockets can follow.
3. Deployment: configure frontend origins and agent URL, and supply backend secrets through the host.
4. Durability beyond this demo: introduce a worker queue, execution leases, and recovery for interrupted
   `running` tasks before adding automatic dispatch or retries.
5. Operational growth: add retention controls, rate limits, and cursor pagination as traffic requires.

## Acceptance checks

1. Change settings, restart the backend, and see saved values.
2. Product pages yield suggestions only for enabled agents; reading pages yield none without a product.
3. Revoking permission after prediction blocks dispatch of that task.
4. Dispatching a task twice produces at most one agent call; the second gets 409.
5. Agent failures are saved; Supabase failures do not cause an untracked agent dispatch.
6. Another user's token cannot read, dispatch, or change the first user's data.
7. Reconnecting clients retrieve saved outcomes from task history.
