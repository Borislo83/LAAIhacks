# FOCAL

![tag:multi-agent](https://img.shields.io/badge/multi--agent-3D8BD3) ![tag:shopping](https://img.shields.io/badge/shopping-3D8BD3) ![tag:news](https://img.shields.io/badge/news-3D8BD3) ![tag:memory](https://img.shields.io/badge/memory-3D8BD3) ![tag:payments](https://img.shields.io/badge/payments-3D8BD3)

**FOCAL works so you can focus on what matters.** Ask it for almost anything. FOCAL works out what you want, plans the work, sends it to the right specialists in parallel, checks their results, and asks you before anything happens for real. Every hop between agents shows up in the chat as it happens.

**Its specialty is shopping.** Ask "should I buy it?" and a whole team takes over: price, reviews and alternatives agents research in parallel, a verifier audits every price, a buyer finds the listing, checkout goes through the Agent Payment Protocol, and FOCAL can keep watching the price for you. It also handles news briefs, videos and messages to friends, and remembers what you like.

## Try it
- **Decide and buy:** "Should I buy the Apple AirPods Pro 2?"
- **Catch up:** "What's the latest on SpaceX Starship?"
- **Find a video:** "Play Blinding Lights on YouTube"
- **Message a friend:** "Text Alex I'm running late"

## The team
| Agent | Role |
|---|---|
| 🧠 Memory | Your Focal Studio profile (live API) and past decisions |
| 💲 Price | Prices across stores, read from product pages, ranked by trust |
| ⭐ Reviews | Expert ratings, pros and cons |
| 🔀 Alternatives | Cheaper or better-rated rivals within budget |
| 🛡️ Verifier | Audits every price, drops weak leads, and sends the price agent back for a deeper search when needed |
| 🛒 Buyer · 🔗 Amazon / Best Buy cart links | Finds the listing and gives you the store's own add-to-cart link (never checks out) |
| 💬 Messenger | Drafts an iMessage you approve before it's sent |
| 📰 News · ▶️ YouTube | Sourced news briefs and video links |

## How a purchase decision runs
Memory → a **form card** for budget and priority → a plan → price, reviews and alternatives **in parallel** → verifier audit (with recovery) → **comparison card** → listing and **cart approval cards** → **iMessage draft card** → a **paid price watch** through the Agent Payment Protocol (Stripe), which keeps checking after the chat ends.

## Protocols
Agent Chat Protocol · ASI:One Interactive Cards (form, custom, review) · Agent Payment Protocol (seller, Stripe) · typed agent-to-agent messages.

## Safety
Nothing is added to a cart, sent or paid for without your approval on a card. Every claim comes with its source, and nothing is made up when a source fails.
