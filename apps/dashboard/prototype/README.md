# Focal design prototype

`focal.html` is the chosen landing direction (E2 Green, "Anatomy"). Open it in a browser; it has no build step.

The Paper file "Focal — Design" holds the static E2 frame (still in the older blue accent).

## Design rules

- **Name:** Focal. Logo is an aperture: ink ring with a dot in the centre.
- **Ground:** paper `#F7F7F5`, ink `#0B0B0C`, muted `#6E6E73`, hairline `#E4E4E6`.
- **Signal green `#139A4E`:** the one accent on light surfaces. Used for live signals from the glasses: gaze dot, lens highlight, active pins, timeline steps, the italic headline word, the `s` unit.
- **Lens surface `#0E0E10`:** only for the glasses display and agents. Phosphor green `#3CFF7A` appears only inside the lens.
- **Type:** Inter 500 with tight tracking for headings, one Instrument Serif italic word per heading, JetBrains Mono for numbers and times.
- **Agents are orbs:** each agent has its own animated "thinking" orb, used as its avatar everywhere.
  - Reviews: light sweeps down a striped sphere
  - Alternatives: dots ride a wave across vertical lines
  - Price: dashes slide along scanlines
  - Reader: bars flicker
- **Motion:** orbs loop. The glance sequence (gaze → intent → agent → answer, counting to 0.8 s) plays once on load and on "Replay the glance". Reduced-motion settings turn all of it off.

## Agent dashboard

`dashboard.html` is a simple Studio page for talking to the Fetch.ai agents, in the same style.

- **Chat:** plain-language questions go to the orchestrator's `POST /v1/chat` (:8001), which picks the agents.
- **Direct:** fill in the fields and call the agents' REST routes on the Bureau (:8000) yourself.
- Usage (calls, average time, errors) is counted in this browser only.

Run `uv run laai-agents` in `services/agents` and `uv run laai-orchestrator` in `services/orchestrator`
(with `CHAT_AUTH_REQUIRED=false` and `http://localhost:5173` in `CORS_ORIGINS` in its `.env`). Then serve this
folder over HTTP, for example `python3 -m http.server 5173`, and open `/dashboard.html`. Opening it as a file can't reach the agents.
