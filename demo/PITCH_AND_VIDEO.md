# Focal — pitch and demo video

Built from the LA Hacks pitching deck, the opening-ceremony deck (tracks and sponsor
rubrics) and the team idea doc. Everything claimed here is something the repo does today;
lines marked **[G2]** depend on the Glasses team's footage or build.

## What we're entering

| Where | Why Focal fits | Say it out loud |
|---|---|---|
| **Applied Intelligence** (main track) | AI solving a real-world problem: overpaying and buying blind | "AI that acts on what you're already looking at" |
| **Agentverse / Fetch.ai** | Four uAgents (coordinator + price, alternatives, reviews) talking typed messages, Agent Chat Protocol, ASI:One search | "One coordinator delegates to three specialists in parallel" |
| **Even Realities** ("Quiet Tech") | Answer appears on the G2 lens only when you look at a product, then gets out of the way | "Help that shows up when you need it, and steps aside when you don't" |
| **Paper** | Landing and Studio designed in Paper ("Focal — Design") | Mention once, show the landing |

Even Realities rule check: no medical, financial or emergency apps. Focal is general
lifestyle help (everyday shopping decisions), not a financial app. Frame it that way; the
cart step never checks out or touches payment.

## Brand

**Focal — your team, one glance away.**
*Chase your goals. Keep your peace.*

One-liner: **Focal is a team of AI agents behind your glasses. Glance at a product and they
tell you whether it's the right price, the right product, and worth buying.**

Three pillars:

1. **Quiet on the surface (G2).** Focal stays out of sight until you glance at something,
   shows one short answer, then gets out of the way.
2. **Powerful underneath (Fetch.ai Agentverse).** Simple to wear, not simple inside. An
   orchestrator reads what you're looking at and sends a team of specialist agents out in
   parallel (price, alternatives, reviews), each answer backed by sources.
3. **Calm control (Studio, designed in Paper).** A clear, focused dashboard to set your
   priorities, approve what agents may do, and see what they've done.

**A team that grows with you.** Tell Focal what you care about once (price, quality or
balanced) and every agent works to that priority. Focal remembers your choices (what you
approve, what you skip, your settings) across the glasses and the Studio, so your team
grows with you. Private notes never reach the AI.

Keep the claims honest: preferences and history are stored and preferences already steer
the agents. Automatic learning from your history is **next**, not built. Say "remembers"
and "grows with you"; don't say the agents "already learn from your behaviour".

## Pitch structure (Demo Path style, from the pitching deck)

Problem → tension → resolution, shown chronologically, features highlighted as they appear.
Split the speaking roughly evenly across the team.

### Round 1 — science fair, 2 minutes + 3 min Q&A

| Time | Speaker | Say | Show |
|---|---|---|---|
| 0:00–0:15 | A | "You're in a store holding $400 headphones. Is that a good price? Is there something better? Are the reviews real? Right now you pull out your phone, open four tabs, and lose ten minutes." | Hold up the product / phone with tabs |
| 0:15–0:30 | A | "Focal puts the answer in your line of sight. You look, it reads the page, and a team of agents answers on your G2 lens." | Hand the judge the G2 **[G2]** |
| 0:30–1:15 | B | "Here's what just happened." Walk the four steps: page captured → orchestrator reads intent → Fetch coordinator sends price, alternatives and reviews agents out **at the same time** → one short answer on the lens. "Every price has a source and a trust level. If we can't verify a number, we don't show one." | Studio (dashboard) with live progress streaming beside the glasses |
| 1:15–1:40 | C | "And it can act — but only with permission. Ask it to add the item to your Amazon cart: it shows you the exact listing, you approve, it adds it and verifies it's in the cart. It physically cannot check out." | Amazon cart review → approve → verified cart |
| 1:40–2:00 | D | "Simple to wear, not simple inside: four Fetch.ai uAgents on Agentverse, an orchestrator, and a Studio designed in Paper where you set your priorities. Focal remembers your choices, so your team grows with you. Next: it learns your priorities on its own, plus reading mode and custom workflows. Focal — your team, one glance away." | Architecture slide → Studio settings |

**Hook alternatives** (pick one, rehearse it): the "four tabs" line above; or a stat — only
use one you've actually sourced, don't make one up on the spot.

### Round 2 — top 5 stage, 3 minutes + 4 min Q&A

Same spine, with 60 more seconds spent on:

1. **The technical "how" (30 s):** concurrent uAgent delegation; trust-ranked pricing
   (`high` = major retailer + read from the page, cheapest in the most trusted tier wins);
   live progress events streamed over authenticated SSE — real work events, no fake
   progress bar.
2. **Trust and safety (20 s):** approval-gated actions, page changes invalidate stale
   approvals, private notes never sent to models, the cart executor has no checkout code.
3. **What we learned (10 s):** honest one-liner, e.g. "live web prices are messy — most of
   our time went into deciding which prices to *not* trust."

### Q&A prep — who answers what

| Likely question | Owner | Short answer |
|---|---|---|
| How do you know the price is right? | Agents | Prices are gathered broadly then ranked by trust; each result carries its source link, retrieval time and limitations. We call them leads, not checkout offers. |
| Why Fetch.ai and not one LLM call? | Agents | Specialists run in parallel and fail independently; the coordinator combines partial results. Each agent is also reachable from ASI:One on its own. |
| Does it learn from me? | DB/orch | Today it remembers: your priority (price/quality/balanced) reorders which agents lead, and your approvals and history are stored per account. Learning priorities automatically from that history is our next step. |
| How fast is it? | Agents | Quote the time you measured in rehearsal. Don't quote the landing page's 0.8 s unless the live demo hits it. |
| What stops it buying things? | DB/orch | Every action needs an explicit approval tied to the exact page and listing; the executor can add to cart and verify, nothing else. |
| Privacy? Is there a camera? | G2 | G2 has no camera. We read the page you're on, not your surroundings. Notes stay in your account and never reach a model. |
| What's next / business? | Anyone | Reading mode, custom workflows in the Studio, more stores. Revenue: affiliate links on the cheaper option — but we'd always show the cheapest trusted price first. |

Don't know? Say so and say how you'd find out. Never argue.

## Demo video script — 2:30 target (2–3 min required)

Proposed script and content only. **[G2]** marks scenes that show the lens.

| Time | Visual | Voiceover | On-screen text |
|---|---|---|---|
| 0:00–0:08 | Cold open: someone in a store picks up Sony WH-1000XM5, glances at them **[G2]** | — (ambient) | — |
| 0:08–0:20 | Fast cuts: phone with four tabs, price sites, review pages, scrolling | "Every time you buy something, you do the same research. Is this the best price? Is there something better? Are the reviews any good?" | "4 tabs. 10 minutes." |
| 0:20–0:30 | Focal logo (aperture) on paper background | "Focal does it in a glance." | **Focal** · your team, one glance away |
| 0:30–0:50 | Through-the-lens: the answer card appears on the G2 HUD **[G2]** | "Look at a product page, and Focal answers right on your Even Realities G2: the best trusted price, a better alternative under your budget, and what reviewers actually say." | — |
| 0:50–1:25 | Studio: type "Consider Sony XM5 and recommend an alternative under $300". Live progress: delegation → searching → sources found → offer checks → results arrive one by one | "Behind that card is a team of Fetch.ai agents. A coordinator sends three specialists out at the same time — price, alternatives, reviews — and you can watch them work. Every price has a source and a trust level. If a number can't be backed up, Focal doesn't show it." | Agent orbs labeled Price · Alternatives · Reviews |
| 1:25–1:35 | Expand a result: source links, retrieval time, limitations | "No invented ratings. No guessed prices." | "Sources on every answer" |
| 1:35–2:00 | "Buy Sony XM5 on Amazon" → choose listing → review card → **Add to real Amazon cart** → Chrome window shows item verified in cart | "And when you're ready, Focal can act — but only with your OK. You approve the exact listing, it adds it to your real Amazon cart and checks it's there. It stops before checkout. Always." | "Approval required" · "Never checks out" |
| 2:00–2:12 | Architecture diagram: G2 → orchestrator (Supabase: approvals, history, preferences) → Fetch coordinator → 3 uAgents → ASI:One / web | "Simple to wear, not simple inside. A whole team of Fetch.ai agents on Agentverse, coordinated by an orchestrator that tracks every approval." | Fetch.ai · Even Realities G2 · Supabase |
| 2:12–2:22 | Studio settings: switch priority to "Price", proactive on; history list | "In the Studio, designed in Paper, you set what matters to you. Focal remembers your choices, so your team grows with you." | "A team that works your way" |
| 2:22–2:30 | Back to the store: person puts the headphones down, picks up the alternative. Logo end card | "Chase your goals. Keep your peace. Focal — your team, one glance away." | **Focal** · your team, one glance away · team names · GitHub URL |

Cut to fit: if long, trim 1:25–1:35 first, then shorten the architecture beat. Don't drop
the approval step — it's the Workday-style "responsible AI" point and the strongest
differentiator.

### Before upload

- [ ] Total length 2:00–3:00, captions burned in or uploaded
- [ ] Nothing shows tokens, `.env`, emails or Supabase keys on screen
- [ ] Amazon account/cart shown is signed-out or a throwaway, no address/payment visible
- [ ] Uploaded to YouTube (unlisted is fine) **well before 12 PM Sunday** so it finishes processing
- [ ] Link added to Devpost with the public GitHub repo, all teammates, 5:3 thumbnail

## Devpost description skeleton

- **Tagline:** Focal — your team, one glance away. Chase your goals. Keep your peace.
- **Inspiration:** the four-tab shopping ritual; glasses that help without demanding attention.
- **What it does:** look at a product → price, alternative and review answer on G2; approved actions like adding to an Amazon cart.
- **How we built it:** Fetch.ai uAgents (coordinator + 3 specialists, Agent Chat Protocol, ASI:One search), Python orchestrator with Supabase (auth, approvals, history, preferences), Even Hub G2 app, Studio dashboard designed in Paper.
- **Challenges:** trusting live web prices; making actions safe (approval tokens, page invalidation, no-checkout executor).
- **What we learned / what's next:** learning your priorities automatically from your history, reading mode, custom agent workflows, more stores.
- **Tracks:** Applied Intelligence; Agentverse; Even Realities; Paper — one sentence each on how it meets that challenge.
