# G2 ↔ Focal integration guide (contract v1.0)

For the Glasses team. This covers how a G2 client talks to the Focal session API (the
orchestrator), with requests and responses copied from a real run on 2026-10-17, and
what `glasses-web-reader/` still needs before it can join the flow.

The examples were captured from the unmodified orchestrator running against our real
migration SQL (embedded Postgres) with `DEMO_AUTH_MODE` tokens. Session and action IDs
are from that run. Prices come from the demo catalog, so they are not live offers.

## 1. Base URL and auth

| Where | Base URL |
|---|---|
| Your laptop, orchestrator running locally | `http://127.0.0.1:8001` |
| Hosted | HTTPS only (not deployed yet) |

Every `/v1/sessions` call needs `Authorization: Bearer <token>`. The server reads the
caller's role from the token, never from the request. The roles are:

| Role | Can |
|---|---|
| `g2` | read display, approve, reject, register a device |
| `web` | create a session, PUT the page, read display |
| `agent` | PUT proposed actions |
| `executor` | read events, ack, POST results |

**Local development (`DEMO_AUTH_MODE`).** When the orchestrator's `.env` has
`DEMO_AUTH_MODE=true` and `DEMO_USER_ID=<an existing Supabase Auth user id>`, it accepts
these fixed tokens: `demo-g2`, `demo-web`, `demo-agent`, `demo-executor` and `demo-all`.
They are accepted only from the same machine (127.0.0.1 or ::1), and only when the request
has no forwarding headers (`X-Forwarded-For`, `Forwarded`, `X-Real-IP` or
`CF-Connecting-IP`). A phone on Wi-Fi or a Cloudflare tunnel therefore cannot use them
directly; see §6.

**Real tokens.** Use a Supabase access token for a user whose `app_metadata.laai_roles`
contains `"g2"`. An admin sets this field (Boris owns the Supabase project); users cannot
set it themselves. Tokens are cached for 30 seconds, so a revoked token can keep working
for up to 30 seconds.

## 2. Call sequence (contract handshake)

```
web       POST /v1/sessions                                  -> session_id, revision 1
g2        GET  /display?after_revision=0                     -> 200 revision 1 (no page)
g2        GET  /display?after_revision=1                     -> 204 (unchanged)
web       PUT  /page                                         -> revision 2
agent     PUT  /actions                                      -> revision 3 or 4
g2        GET  /display?after_revision=2                     -> 200 mode "actions"
g2        POST /actions/{id}/approve  {expected_revision}    -> approved + event_id
executor  GET  /events?after=                                -> action.approved
executor  POST /actions/{id}/result   running, then completed
g2        GET  /display?after_revision=N                     -> mode "loading", then "result"
```

### Create a session (web)
`POST /v1/sessions` with body `{}` (or `{"device_id": "<uuid from POST /v1/devices>"}`) returns **201**:
```json
{"schema_version":"1.0","session_id":"971abe4d-9839-4a3c-90f0-cb9e6a6e5c13","revision":1}
```

### Poll the display (g2)
`GET /v1/sessions/{id}/display?after_revision=0`:
```json
{"schema_version":"1.0","session_id":"971abe4d-9839-4a3c-90f0-cb9e6a6e5c13","revision":1,
 "page":null,"actions":[],"mode":"summary","notice":null,
 "preferences":{"display_density":"comfortable"}}
```
If you then send `GET .../display?after_revision=1` and nothing has changed, you get **204 with an empty body**. Always send the last
revision you rendered, keep showing the current screen on a 204, and poll about once a second.

### Page arrives (web), actions arrive (agent)
`PUT /v1/sessions/{id}/page`:
```json
{"schema_version":"1.0","request_id":"g2-e2e-page-001","page_id":"page-sony",
 "url":"https://example.com/sony-wh-1000xm5","kind":"product",
 "title":"Sony WH-1000XM5 Wireless Headphones",
 "summary":["Active noise cancellation","Up to 30 hours battery"],
 "facts":[{"label":"Price","value":"$399.99 (demo)"}],
 "source_label":"Example Store","updated_at":"2026-10-17T20:00:00Z"}
```
→ `{"schema_version":"1.0","session_id":"971abe4d-…","revision":2}`

The executor (`laai_agents.integration`, or its `watch` mode on its own) then PUTs `compare_prices` and `find_reviews`.
The next display poll returns:
```json
{"schema_version":"1.0","session_id":"971abe4d-9839-4a3c-90f0-cb9e6a6e5c13","revision":4,
 "page":{"page_id":"page-sony","title":"Sony WH-1000XM5 Wireless Headphones","kind":"product",
   "summary":["Active noise cancellation","Up to 30 hours battery"],
   "facts":[{"label":"Price","value":"$399.99 (demo)"}],"source_label":"Example Store"},
 "actions":[
  {"action_id":"act-1-50860f3d-89ad-491a-8137-5057a386aee8","type":"compare_prices",
   "label":"Compare prices and alternatives",
   "description":"Research Sony WH-1000XM5; alternatives under $300",
   "status":"proposed","requires_approval":true,"expires_at":"2026-10-18T04:25:13.768999Z","result":null},
  {"action_id":"act-2-9a00fe9c-b981-4b8b-8c4e-fb32da9e181d","type":"find_reviews",
   "label":"Check independent reviews",
   "description":"Research Sony WH-1000XM5; alternatives under $300",
   "status":"proposed","requires_approval":true,"expires_at":"2026-10-18T04:25:13.768999Z","result":null}],
 "mode":"actions","notice":null,"preferences":{"display_density":"comfortable"}}
```
Show only actions with `"status":"proposed"` as choices. Never create an action ID
yourself; only IDs from the latest display are valid.

### Approve (g2)
`POST /v1/sessions/{id}/actions/act-1-50860f3d-…/approve`:
```json
{"schema_version":"1.0","request_id":"g2-e2e-tap-001","expected_revision":4}
```
→ **200**
```json
{"action_id":"act-1-50860f3d-89ad-491a-8137-5057a386aee8","status":"approved","event_id":"fce9e367-6384-4d5c-a905-d51816ae8c17"}
```
Sending the identical body again returns the identical response and does not create a
second event. Reject uses the same body at `.../reject` and returns
`{"action_id":"act-2-…","status":"rejected","event_id":null}`. A reject emits no event.

`expected_revision` must be the `revision` of the display the user confirmed on.

### What the executor sees (not your job; shown for reference)
`GET /v1/sessions/{id}/events?after=` returns:
```json
{"events":[{"event_id":"fce9e367-6384-4d5c-a905-d51816ae8c17","cursor":"0000000000000000001",
  "type":"action.approved","session_id":"971abe4d-…","action_id":"act-1-50860f3d-…",
  "page_id":"page-sony","occurred_at":"2026-10-18T03:55:13.799000Z",
  "consumed_at":null,"action_status":"approved"}]}
```
The executor posts `running`, then `completed` or `failed`. Each post increments the revision.

### Result on the lens (g2)
While the action runs, `mode` is `"loading"`. When it finishes, `mode` is `"result"` and the action carries its outcome:
```json
{"action_id":"act-1-50860f3d-89ad-491a-8137-5057a386aee8","type":"compare_prices",
 "label":"Compare prices and alternatives","status":"completed","requires_approval":true,
 "expires_at":"2026-10-18T04:25:13.768999Z",
 "result":{"status":"completed",
   "message":"Best price for Sony WH-1000XM5 is $328.00 at Amazon.\nConsider Anker Soundcore Space Q45 ($99.99): $228.01 cheaper.",
   "action_id":"act-1-50860f3d-…","request_id":"fce9e367-…:outcome","result_url":null}}
```
`result.message` is plain text with `\n` line breaks. `result.result_url` is null or an HTTPS link.

### Modes
The server picks the mode from the actions, in this priority order:

| `mode` | When | Show |
|---|---|---|
| `summary` | no actions on the current page (invalidated actions are left out of `actions`) | page title, summary, facts |
| `actions` | at least one action, none approved/running/finished | page + list of `proposed` actions. If every action was rejected, the mode stays `actions` with nothing to choose, so show the page. |
| `loading` | an action is approved or running | "Working: <label>" (other proposed actions stay selectable) |
| `result` | an action completed (none running/failed) | `result.message` |
| `error` | an action failed | `result.message` (e.g. "Live research unavailable") |
| `confirm` | **never sent by the server**; your local "are you sure?" screen | |
| `reader` | reserved; not sent until a `read_article` handler exists | |

`notice` is always null today. Action order is `(proposed_at, action_id)`; our executor's IDs
(`act-1-…`, `act-2-…`) keep its order stable.

## 3. Errors

Every error has this shape, and `request_id` echoes yours when you sent one:
```json
{"error":{"code":"STALE_REVISION","message":"Display changed; refresh and confirm again.","request_id":"g2-e2e-tap-001"}}
```

| HTTP | `code` | What the G2 app should do |
|---|---|---|
| 409 | `STALE_REVISION` | Refetch the display with `after_revision=0`. If the action is still `proposed`, have the user confirm again, then resend with the new `expected_revision`. The failed attempt was not stored, so either reuse the `request_id` or send a new one. |
| 409 | `ACTION_EXPIRED` | Refresh and show "That option expired." Do not retry. |
| 409 | `CONFLICT` | The action belongs to an old page or proposal, was already decided, or the same `request_id` was sent with a different body. Refresh and show "No longer available." Do not retry. |
| 404 | `ACTION_NOT_FOUND` / `SESSION_NOT_FOUND` | Refresh, or pick up the new session. Action IDs from another session always return 404. |
| 429 | `RATE_LIMITED` | Wait (`Retry-After` if present), then resend the **same body and `request_id`**. The limit is 600 operations per user per minute. |
| 503 | `BACKEND_UNAVAILABLE` | Back off and resend the same body and `request_id`; after a few tries show "Offline". The same applies to network errors and non-JSON 502/503 pages from a proxy. |
| 401 | `UNAUTHORIZED` | Token missing, invalid, or a demo token sent from another machine. |
| 403 | `FORBIDDEN` | Wrong role, for example a `web` token calling approve. |
| 422 | `INVALID_PAYLOAD` | Bug in the client; do not retry. `schema_version` must be `"1.0"`. |

The rule for retries: resend a tap with the same `request_id` as often as needed, and the
server applies it once. A real run showed this: the first try returned 429, the second 503,
and the third was applied but its reply was lost. The fourth try received the original
approval, and only one event existed.

## 4. Lens limits (G2 576×288)

The API allows more text than the lens can show. Clip on your side:

- Action `label` is up to 160 characters in the API, but a list item holds at most **63
  UTF-8 bytes** and a list at most 20 items (firmware limit, also enforced by the Even Hub
  simulator). Our labels are 25 and 31 bytes.
- Page `summary` allows 20 items of up to 2,000 characters each, and `facts` up to 30.
  About 10 lines of about 48 characters fit on one screen, so paginate or truncate.
- `description` (~55 characters) and `result.message` (up to 16,000 characters) are wider
  than one line. In live mode the message also adds up to 3 `Source:` URLs and a
  `Retrieved:` line for each result. Wrap the text, and paginate with swipes.

## 5. Simulated G2 client

`services/agents/src/laai_agents/g2_sim.py` is a reference G2 client. It polls with
`after_revision` and handles 204, renders each mode inside the limits above, and lets the
user approve or reject only listed actions. It applies every retry rule in §3.

Watch a session and approve option 1 against **our** server:
```bash
cd services/agents && uv run laai-g2-sim --base-url http://127.0.0.1:8001 --token demo-g2 --session <session_id> --approve 1
```

To check **your** server, or any implementation of the contract, use `--base-url` and
`--token`. Add `--once` to render a single display and exit, or `--reject N` to reject option N.

Example output from the run above:
```
--- revision 4 mode actions
Sony WH-1000XM5 Wireless Headphones
- Active noise cancellation
- Up to 30 hours battery
Price: $399.99 (demo)
  [1] Compare prices and alternatives
  [2] Check independent reviews
```

The automated version is `services/agents/tests/test_g2_e2e.py` (`uv run pytest
tests/test_g2_e2e.py`, about 8 seconds). It needs `node`, plus `npm ci` in `supabase/tests`. It
runs the unmodified orchestrator with demo tokens on the real migrations
(`supabase/tests/postgrest_stub.mjs`) and the real executor in catalog mode. It covers:

- handshake steps 1–5;
- payload shapes;
- page change invalidating old actions;
- stale approval → refresh → confirm again;
- declining after a refresh;
- duplicate `request_id`;
- reject;
- expiry;
- executor crash and redelivery;
- ack;
- cross-session IDs;
- 429/503/lost-reply retries.

Set `G2_E2E_TRANSCRIPT=file.json` to save every G2 exchange.

## 6. glasses-web-reader → Focal: gap report

Checked on 2026-10-17 at the repository root, `glasses-web-reader/`:

- `npm ci`, `tsc --noEmit` and `vite build` all pass.
- The condense server (:8787, with `ALLOW_PRIVATE_HOSTS` unset) and Vite (on :5180, because
  :5173 is taken by our dashboard) both serve `/api/health`.
- `/api/condense` returned pages for an NPR article, an AP and a BBC front page, Wikipedia, and an Amazon product page.
- The SSRF guard refused `http://127.0.0.1:8001` (403).
- The desktop simulator (`evenhub-simulator --automation-port`) runs, renders, and responds to taps.
- Smart view, tested from the main checkout with an Anthropic key in its `.env`
  (`npm run server`, :8787):

  | Page | Result | Time |
  |---|---|---|
  | apnews.com (news) | page with 57 sections, then a `kind` event and 4 screen batches | 24 s |
  | crutchfield.com, Sony WH-1000XM5 (product) | `kind` = `shop`, 2 screen batches | 18 s |
  | rtings.com, WH-1000XM5 review | `kind` = `shop`, 2 screen batches | 7 s |
  | bestbuy.com product page | clean 502 "Couldn't load bestbuy.com - it blocks automated readers" | — |

  Smart view takes 7–24 s per page, so only PUT the page once the first `page` event
  arrives, not after the whole deck is built.

Today the app calls only its own `/api/condense`, `/api/manga/strip` and
`/api/telemetry`. It never calls `/v1/sessions`, so nothing it reads reaches Focal and no
Focal actions reach the lens. To connect them:

1. **Session.** On launch, call `POST /v1/devices` (g2) once and keep `device_id` with
   `bridge.setLocalStorage`. Then call `POST /v1/sessions` with `{"device_id": …}` (web). Sessions expire after
   24 hours, so create a new one when you get `SESSION_NOT_FOUND`.
2. **Page.** After `condense()` delivers its first `page` event in `load()` (`src/main.ts`),
   PUT a `PageSubmission` built from `CondensedPage`, for example from the Amazon page in our run:
   ```json
   {"schema_version":"1.0","request_id":"<uuid per PUT>","page_id":"url-<sha256(url)[:32]>",
    "url":"https://www.amazon.com/dp/B09XS7JWHH","kind":"product",
    "title":"Sony WH-1000XM5 Wireless Industry Leading Noise Canceling Headphones with Auto Noise …",
    "summary":["Auto NC Optimizer, Up to 30 Hrs of Battery, Carrying Case, Crystal Clear Calls, …",
               "4.2 out of 5 stars (20,197)"],
    "facts":[{"label":"Price","value":"…"}],
    "source_label":"amazon.com","updated_at":"<now, ISO 8601>"}
   ```
   - `page_id` must match `^[A-Za-z0-9_.:-]{1,160}$`, so a raw URL is not valid; hash it.
   - `title` is at most 500 characters, `summary` at most 20 × 2,000, `facts` at most 30.
   - Map Smart view's `kind` (`shop` → `product`, `news` → `article`, and so on). Its `product` screen (`price`,
     `rating`, `stock`) fills `facts` better than raw sections do.
   - Every navigation must PUT a new `page_id`, which invalidates the old page's actions.
3. **Actions.** Poll `GET /display?after_revision=N` while a page is open. When the mode is
   `actions`, add the proposed labels to the existing menu (`openMenu()`, next to "Contents"
   and "Links on page"). A tap opens a local confirm screen, and a second tap approves with the
   displayed `revision`. Show `loading`, `result` and `error` with the existing reader
   containers.
4. **Who proposes actions.** `laai-glasses-executor watch` does it automatically, and the
   glasses app needs no change for it. It lists the user's sessions (`GET /v1/sessions`,
   `agent` or `executor` role, added with migration `202610170006_session_list.sql`) and
   re-reads the display of each session whose revision changed. When the active page is a
   `product` page with no actions yet, it proposes `compare_prices` and `find_reviews` for
   the product named in the title. When the user approves one, it runs it and posts the
   result, like `poll`. It never proposes for a page that has been replaced: the API refuses
   that with 409, and the watcher does not retry. A new page PUT invalidates the old
   page's actions, and the watcher then proposes for the new page. It also skips sessions
   untouched for 15 minutes, and pages that already have actions from any proposer. Its
   journal keeps proposal and result request IDs, so a restart sends the same proposal again
   and the API applies it once. `article` and `generic` pages get no actions yet: a
   news-summary action needs a new registered action type (see `PROPOSALS` in
   `integration.py`).
   ```bash
   cd services/agents && uv run laai-glasses-executor --journal ~/.local/state/laai/watch.json watch
   ```
   Set `GLASSES_API_URL` and `GLASSES_{WEB,AGENT,EXECUTOR}_TOKEN` as in §7. `--once`
   makes one pass and exits, and `--interval` sets the seconds between passes (default 2).
   Each pass costs 1 request, plus about 3 for each session that changed, from the same
   600-per-minute budget the G2 uses.
5. **Network path and auth (blocker for real glasses).** The app runs in the phone's Even
   WebView and reaches the server over Wi-Fi or a tunnel. Demo tokens are refused from any
   address that is not loopback. The orchestrator's CORS allowlist (`CORS_ORIGINS`) holds only
   the dashboard origins (`localhost:3000`, `localhost:5173`), not the WebView's origin. The simplest fix is for the condense server (same machine as the
   orchestrator) to proxy `/api/focal/*` to `http://127.0.0.1:8001/v1/...` and attach the
   token on the server side. That keeps tokens out of the phone and needs no CORS change.
   The proxy must not add `X-Forwarded-For`, and the orchestrator must run with
   `DEMO_AUTH_MODE=true`. In production, use a real `g2`-role Supabase token instead.

Observations about glasses-web-reader itself (reported only; none of its code was changed):

- **Smart view needs `ANTHROPIC_API_KEY`.** Without it, every page ends in the stream error
  "Smart view is not set up: add ANTHROPIC_API_KEY to the server .env", and the glasses show
  "Could not load <site>". `.env.example` says the key is needed only for "AI summary mode"
  and that "Full-text mode works without it". The README calls it required.
- **The browser fallback needs `npx playwright install chromium-headless-shell` once.**
  Without it, every site that blocks plain fetches (AP, B&H, Best Buy) fails with "it blocks
  automated readers". The README does not mention this step.
- **`BOT_WALL` misses some block pages.** It did not catch Sweetwater's "Access to this page
  has been denied", which came through as a 16-word page.
- **Best Buy and REI** still fail after the browser fallback.

## 7. Running it live against the real database

The orchestrator now running on :8001 has no `DEMO_AUTH_MODE` setting, so the live HTTP
run was not done. To run it:

1. Add `DEMO_AUTH_MODE=true` and `DEMO_USER_ID=<existing Auth user id>` to
   `services/orchestrator/.env` (local only), then restart the orchestrator.
2. Check that the live Supabase project has migrations through `202610170004`. The
   automated test also applies `202610170005_workflow_perf.sql`, which changes no response
   shapes.
3. Run the same steps against it, using new sessions and `request_id`s that start with `g2-e2e-`:
   ```bash
   cd services/agents && uv run laai-glasses-executor --journal /tmp/g2-e2e.json propose --page page.json --product "Sony WH-1000XM5"
   ```
   ```bash
   cd services/agents && uv run laai-g2-sim --session <printed id> --approve 1
   ```
   ```bash
   cd services/agents && uv run laai-glasses-executor --journal /tmp/g2-e2e.json poll
   ```
   For the executor commands, set `GLASSES_API_URL=http://127.0.0.1:8001` and
   `GLASSES_{WEB,AGENT,EXECUTOR}_TOKEN=demo-{web,agent,executor}` in their environment.
4. For the automatic flow, apply `202610170006_session_list.sql` too. Then run
   `uv run laai-glasses-executor --journal /tmp/g2-watch.json watch` in place of the
   propose and poll commands, and PUT pages from the web role or glasses-web-reader.
