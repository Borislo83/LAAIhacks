# Merged reader setup

The merged source is in **glasses-web-reader**. The original standalone
LensOS_Glasses_Team_Playbook and sibling LAAIhacks services/database were not
modified. No new database or migration is introduced by this merge.

## What comes from each project

| Source | Responsibility |
| --- | --- |
| glasses-web-reader | Claude web reinterpretation, streamed decks, images, MangaDex, gestures, navigation, recent pages and reading position |
| LensOS | Paginated action review, explicit approve/reject, revision checks and identical-request retry after uncertain acknowledgments |
| LAAIhacks orchestrator | Supabase-owned sessions/pages/actions, preferences, task history, AI predictions and task dispatch |

`src/lensos/action-cards.ts` is adapted from LensOS's
`apps/even-g2/src/action-cards.ts`. Its types/client and session coordination were
adapted to the actual orchestrator contract rather than the standalone fixture API.
In particular, session creation sends `{}`, not LensOS's create request envelope.

## Run

1. Start the existing agent service and orchestrator using their own runbooks:
   `../services/agents/README.md` and `../services/orchestrator/README.md`.
   Default ports are agents **8000**, orchestrator **8001**. Keep their existing
   Supabase configuration. Do not rerun existing database migrations for this merge.
2. In this folder, run `npm ci`. Copy `.env.example` to `.env` if needed, set
   `ANTHROPIC_API_KEY`, and point `ORCHESTRATOR_URL` at the orchestrator.
3. Run `npm run dev`: reader server **8787**, Vite **5173**. The phone talks to
   `/api/backend` through the same reader origin; only the reader server contacts
   the orchestrator. Continue using `npm run simulate` or the existing QR workflow.
4. In **Account & recommendations**, enter a signed Supabase user access token.
   Ordinary account access requires the backend's `web` role. To approve external
   session actions, the same token also needs trusted `g2` access, or enter a
   separate G2 access token belonging to the same owner. Use the existing trusted
   token issuance/auth flow; this app never grants roles.
5. Enable **Suggest useful actions for pages I open** if desired and save. New
   accounts default to proactive suggestions being off. Open a page, or use
   **Sync current page** to save and predict again after changing preferences.

Tokens stay in memory and are cleared from the form after submission. Only the
session ID and its page URL are saved through the bridge. Reconnect after token
expiry or restarting. Account transport requires HTTPS except on localhost.
Public `demo-*` fixture tokens are deliberately refused by the gateway: a proxy
must not turn an external request into a trusted loopback fixture request.

For a phone/hosted build, set the exact reader HTTPS origin in
`ORCHESTRATOR_ALLOWED_ORIGINS`. Keep `ORCHESTRATOR_URL` server-side. For packaged
builds, set `VITE_API_BASE` and the network whitelist in `app.json` to the reader
server origin as in the original packaging instructions. No Supabase service key
belongs in frontend variables or the phone form.

## Recommendations and actions

- **Recommendations** come from `/v1/contexts`, using the orchestrator's existing
  ASI:One prediction configuration and structured-product fallback. The reader
  keeps Claude for web layouts. Confirming a recommendation calls `/v1/tasks/{id}/dispatch`.
  Dismiss only hides it for this page; the backend has no task-rejection endpoint.
- **Session actions** come from external agents through the shared session API.
  Approve/reject uses the current revision. Approval records an event; an existing
  executor must consume it and report results. Approval alone is not completion.
- **Saved task history** is fetched from Supabase through the orchestrator. History
  entries are read-only so an old page's prediction cannot accidentally run.
- After an uncertain task dispatch, retry checks its status; it does not dispatch
  again. Uncertain session decisions retry the exact original request ID.
- Reconnecting to the same saved page preserves its session actions. Opening a new
  page or explicitly syncing submits a new page and invalidates old actions under
  the existing database contract.
- Preference updates preserve private notes. Notes are never included in page
  prediction input. Reader operation remains available when disconnected.

## Verification and known limits

`npm test` runs the gateway/client/session/review/panel regressions.
`npm run build` checks TypeScript and produces the production bundle.
`npm run test:preview` starts an isolated UI fixture at `http://127.0.0.1:5187`;
use the test token `browser-fixture` there. It uses a simulated SDK and mock API,
never the real database or agents. These fixture files are excluded from the
production build.

All 16 tests and the production build passed after the final review fixes.
Review identified and fixed reconnect invalidation, stale-page restoration,
offline Back navigation and a stale busy state. Isolated browser checks confirmed
reader rendering, recommendations, cancellation, one confirmed dispatch and
reconnection with unchanged session revision. The checks required elevated
execution because the sandbox interfered with the Windows toolchain. No live
Supabase mutation, real agent execution or physical G2 verification was performed.
