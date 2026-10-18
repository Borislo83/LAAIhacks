# Focal design prototype

`focal.html` is the chosen landing direction (E2 Green, "Anatomy"). Open it in a browser; it has no build step.

The Paper file "Focal — Design" holds the static E2 frame (still in the older blue accent).

## Design rules

- **Name:** Focal. Logo is an aperture: ink ring with a lens-green dot in the centre.
- **Sheet:** every page follows `index.html` (the blueprint), graphite drafting film `#23272b` with a 24px/120px grid and a thin inset frame line. Shared tokens live in `blueprint-tokens.css` (public-info keeps its own copy).
- **Ink `#e8ebe4`** for line work and text, muted `#8d949a`, 1px hairlines, corners of 3px or less, no shadows or gradients.
- **Lens green `#8ee6a2`:** the G2's display colour, used only for live things: what lands on the lens, active states, data in motion. Lens previews are dark glass with green type.
- **Type:** Instrument Serif (upright, no italics) for headings, Instrument Sans for body, JetBrains Mono for small technical labels, numbers and times. Buttons are solid ink with sheet-coloured text, or a hairline outline.
- **Agents are orbs:** each agent has its own animated "thinking" orb, off-white marks on a dark disc with a hairline ring, used as its avatar everywhere.
  - Reviews: light sweeps down a striped sphere
  - Alternatives: dots ride a wave across vertical lines
  - Price: dashes slide along scanlines
  - Reader: bars flicker
- **Motion:** orbs loop. The glance sequence (gaze → intent → agent → answer, counting to 0.8 s) plays once on load and on "Replay the glance". Reduced-motion settings turn all of it off.

## Agent dashboard

`dashboard.html` is a simple Studio page for talking to the Fetch.ai agents, in the same style.

Chat and Direct mode show real progress while requests run: delegation, online search,
sources found, offer checks and completion/failure. Agent results appear as they arrive;
the final answer retains an expandable activity log. An interrupted stream is shown as
interrupted and is not automatically resubmitted.

The **Amazon cart agent** accepts a product name or Amazon listing URL. Selecting a listing
opens a cart-addition review. **Add to real Amazon cart** or `confirm add to cart` runs the
local browser executor. It opens a separate Chrome window, adds the approved ASIN to a real
Amazon cart and verifies it there. It stops before checkout and cannot submit an order.
Sign in or complete Amazon verification in that Chrome window if asked. Its cart belongs
to that browser profile, not automatically to your normal browser. Product-name searches
need online search; direct listing URLs prepare the review without a search.

- **Chat:** pick any of the ten agents in the roster to chat with it. The **Focal coordinator**
  (`focal-shopping-assistant`) is the full chat: questions go to the orchestrator's
  `POST /v1/chat/stream` (:8001), which picks the agents. Every other agent's chat is scoped:
  it goes to `POST /v1/agents/<agent-name>/chat/stream` and calls only that agent's route, never
  another agent. Each agent keeps its own conversation. The **Price verifier** audits offers you
  paste (store and price each) and never asks the price agent; pasted prices count as medium trust
  at best. **Memory** saves a message starting with "Remember" and recalls on anything else, keyed
  by your account. The cart and iMessage chats keep their approval steps: a cart addition needs the
  review you approved, and a draft sends only from its Approve button.
- **Direct:** fill in the fields and call the agents' REST routes on the Bureau (:8000) yourself.
- Usage (calls, average time, errors) is counted in this browser only.
- Ask "Consider Sony XM5 and recommend an alternative under $300" to delegate to all
  three specialists through the Focal coordinator. Results show online sources, timestamps
  and limitations. Prices/reviews in explicit demo mode are labeled illustrative.
- This Studio is for research testing. The G2 approval workflow uses the contract client
  in `services/agents/src/laai_agents/integration.py` and the Glasses Team's API.

Run `uv run laai-agents` in `services/agents` and `uv run laai-orchestrator` in `services/orchestrator`
(with `CHAT_AUTH_REQUIRED=true` and `http://localhost:5173` in `CORS_ORIGINS` in its `.env`). Then serve this
folder over HTTP, for example `python3 -m http.server 5173`, and open `/dashboard.html`. Opening it as a file can't reach the agents.

## Dashboard authentication

The Studio uses Supabase email/password sign-in, account creation with email confirmation,
persistent sessions, automatic token refresh, and sign-out. Enable the Email provider in
Supabase Auth and allow the exact dashboard URL (for example
`http://localhost:5173/dashboard.html`) under Authentication → URL Configuration → Redirect URLs.
Serve the page over HTTP locally or HTTPS when hosted; do not open it as a file.

The dashboard loads only the Supabase URL and publishable key from `GET /v1/auth/config`.
The service-role/secret key stays on the backend. Chat, Direct, and buyer demo streams send
the signed-in user's bearer token to the orchestrator, which verifies it with Supabase Auth.
Dashboard streams always require authentication, including when the legacy JSON-chat local
prototype bypass is enabled. The dashboard never calls the Bureau directly. Keep the Bureau
private; use the orchestrator as the browser's entrypoint.

Sign-out hides the workspace, cancels active requests, and clears the conversation and pending
purchase confirmation. Changing the backend requires reconnecting before tokens are sent.
The pinned Supabase browser SDK loads from jsDelivr; allow it in hosting CSP settings.
