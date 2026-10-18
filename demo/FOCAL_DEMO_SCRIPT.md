# FOCAL demo script (ASI:One)

Verified end to end in ASI:One. Open FOCAL's chat at
https://asi1.ai/ai/agent1q2fpzyxeg8xcdr6vwesu9de5rkumtcs9qs8fq3f088mjrgfg86j0ch8z0up
(the box starts with `@focal`). Keep `@focal` at the start of every typed message, or it
goes to your personal AI instead. Run one FOCAL chat at a time. If the screen looks stuck,
reload the page.

Before recording, the agents must be running (`AGENTVERSE_MAILBOX=1 uv run laai-agents` in
`services/agents`).

| # | You do | FOCAL does (what to point at) |
|---|---|---|
| 1 | Type `@focal I'm thinking about buying Apple AirPods` | "⚡ Got it!" → lists the AirPods models → asks the **Memory agent**, which shows its real Supabase API call and your past purchases → **Shopping brief** card |
| 2 | Type `@focal my budget is 250` | "With a $250 budget, I'd go with **Apple AirPods Pro 2** ($249)…" → the plan → **live agent conversation**: FOCAL → Price / Reviews / Alternatives in parallel, each one answering, Reviews checks the rival, **Verifier** audits the price (~40–60 s) → **FOCAL verdict** card |
| 3 | Click **Add Apple AirPods Pro 2 to cart** | FOCAL → Buyer → **Amazon listings** card |
| 4 | Click **Apple AirPods Pro 2 (USB-C)** | Buyer prepares the cart → **Approve cart addition** card |
| 5 | Click **Add to cart** | FOCAL → Amazon cart-link agent → Amazon's add-to-cart link → **Checkout · $249.31** card |
| 6 | Click **Pay $249.31** | FOCAL → Stripe → ASI:One's **Pay with Stripe · 249.31 USD** card (Agent Payment Protocol) |
| 7 | Type `@focal confirmed` (or click **Confirm Payment**) | Stripe check → 🧾 **Purchase complete** (test mode) → **Before you go** card |
| 8 | Tick all three, type **John** in the name box, click **Do it** | Memory saves the purchase, price watch armed, Messenger drafts "I just got the Apple AirPods Pro 2!" → **Send iMessage?** card |
| 9 | Click **Send** | Messenger sends (dry run) → **Team recap** card listing every agent and what it did |

Optional extras: `@focal What's the latest on SpaceX Starship?` and
`@focal Play Blinding Lights on YouTube`.

Notes for the voiceover:
- Everything is Stripe **test mode**: no real money moves and no real Amazon order is placed.
- The iMessage send is a dry run to a demo contact.
- Live prices and reviews come from real web searches, so numbers can vary slightly.
