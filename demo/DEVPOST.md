# Focal — Devpost submission (optimised)

Even Hub App Name: Focal

Tracks: Applied Intelligence · Agentverse (Fetch.ai) · Even Realities · Paper · Cognition (Devin) · Visa (recommended, see note at bottom)

---

## Inspiration

You're standing in a store holding a pair of headphones. Is this a good price? What do reviewers actually say? Is there something better for less? Answering that means pulling out your phone, opening five tabs and losing ten minutes, while the store, and the people you came with, fade into the background.

Smart glasses should fix this, but today they mostly show notifications. We wanted the opposite of another screen: you ask once, without looking down, and a team of AI agents goes and does the busywork. The answer shows up in your line of sight for as long as you need it, then disappears.

That's Focal: **your team, one glance away.**

## What it does

Focal is a team of AI agents behind your Even Realities G2 glasses. You ask in plain words, by voice or from the chat box, and Focal decides which agents to send.

- **Shop smarter.** Ask about a product and three agents work in parallel: one finds trusted prices, one reads expert reviews and writes a verdict with pros and cons, and one finds a better alternative in your budget. A verifier agent checks every price before you see it. You get three picks (best price, alternative, recommended) with product photos and sources.
- **Search a store.** "Any smart watches under $50 at Amazon?" lists real Amazon products in budget, ranked by rating and number of reviews.
- **Act, with your OK.** One tap adds a pick to your real Amazon cart, then Focal checks the cart page to confirm it's there. It always stops before checkout.
- **Read any web page on your glasses.** Say "Hey Search" and a question, and the best page opens, rebuilt into screens that fit the G2's 576×288 lens: headline cards, key points, product lists with prices and ratings. Then a single "Focal can help" strip offers up to three next steps.
- **Everything else, same chat box.** Find a song or video on YouTube, get a newsletter-style brief of today's news, or draft a text to a friend that's only sent once you approve it.
- **Works inside ASI:One too.** The whole shopping workflow runs in an ASI:One conversation with Interactive Cards: a brief form, a live comparison, a cart review and an optional paid price watch.

## How we built it

Focal has three layers: the glasses reader, an orchestrator that decides who handles each request, and a team of Fetch.ai agents that do the work.

**Agents (Fetch.ai uAgents, Agentverse, ASI:One).** Each capability is its own uAgent: price, reviews, alternatives, a price verifier, memory, Amazon and Best Buy carts, YouTube, news, Spotify and a messenger. The FOCAL coordinator turns your intent into a plan: it loads your preferences from the memory agent, sends typed messages to the price, reviews and alternatives agents **in parallel**, streams each hop into the chat as it happens, has the verifier audit the prices (retrying and recovering when a specialist fails or times out), and combines the answers into picks. Every agent speaks the **Agent Chat Protocol**, is registered on **Agentverse** as a mailbox agent (one command registers them all), and can be found through **ASI:One**.

Inside ASI:One, the coordinator uses **Interactive Cards** for each step: a brief form, a comparison card, a listing picker, a cart review with approve/reject, and a message-draft review. It also implements the **Agent Payment Protocol** with Stripe Checkout in test mode for a paid price watch, our monetization model. The seller verifies the payment with Stripe before it sends `CompletePayment`, and live keys are refused by default. Real side effects (adding to a cart, sending a message) only run for the owner's address and only after a card approval. Everyone else gets the same flow, with those steps clearly marked as simulated.

**Orchestrator (Python, FastAPI, Supabase).** It reads each request and routes it: a product question to the coordinator, a song to YouTube, a text to the messenger. It streams live progress over SSE, so you watch real agent events rather than a fake progress bar. Supabase handles sign-in, saved preferences (price, quality or balanced) and task history.

**Glasses reader (TypeScript, Even Hub SDK, Claude).** A small server fetches a page (with SSRF guards), extracts the article with Readability, and Claude decides what kind of site it is (news, shopping, forum, live stats, wiki) and builds a short deck of screens for the 576×288 display. Images are converted to 4-bit greyscale at their exact size so they read on a see-through lens. "Hey Search" voice queries are transcribed, and Claude picks the best page to open. You navigate with the temple touchpad or the R1 ring.

**Real data, no paid data APIs.** Prices and listings come from Amazon's public search pages and store pages. Reviews come from schema.org markup and review sites like PCMag and SoundGuys. News comes from full articles found through Google News. YouTube results come from oEmbed and channel feeds.

**Cart.** A Playwright-driven Chrome window with its own signed-in Amazon profile adds the item and checks the cart page. There is no checkout or payment code in the cart path.

**Design (Paper).** We designed Focal's landing page and Studio dashboard in Paper ("Focal — Design"), working out the visual system there first: a calm paper background, one green accent borrowed from the G2's display, and an aperture logo. Then we built the web pages from those frames. The glasses screens follow the same rules: one idea per screen, generous spacing, nothing that competes for attention.

**Built with Devin (Cognition).** We used Devin as an extra engineer to write code. We gave it scoped tasks with a clear way to check the result, such as fixing the layout of our scroll-driven pitch page (label collisions, framing, half-drawn drawings and the phone layout across breakpoints). It came back as a pull request we reviewed and merged, and we sent heavier, slower jobs to Devin sessions so the rest of the team could keep building the glasses app in parallel.

## Challenges we ran into

- **Rendering websites on the glasses.** A web page is built for a big colour screen, and the G2 shows 576×288 pixels of green on a see-through lens. Pulling the real content out of cluttered pages, then laying it out as readable screens, took many rounds.
- **Images on a lens with no colour.** Black is see-through, so a normal photo becomes a glaring block or vanishes. We convert every image to 4-bit greyscale at its exact size: bright photos become line art, white product backgrounds are knocked out, and dark images are dithered so they still read.
- **Getting a dozen agents to agree.** We tuned constantly: what each agent searches for, how strictly it matches a product, when to retry, how long to wait, and how the coordinator combines answers. Early on, a search for Galaxy Buds2 alternatives suggested "Battle of the Buds", a headline Amazon matched to an unrelated listing, and another offered an AirPods case as an alternative to earbuds. We added rules to tell products from headlines and accessories, and to keep model numbers exact, so "Galaxy Buds 3 Pro" never passes for "Galaxy Buds Pro".
- **Following the request.** "Smart watches under $50 at Amazon" is a category, a budget and a store, not a product, and a text message request was first mistaken for shopping. We rebuilt routing so the orchestrator follows what you actually asked.
- **Making actions safe.** Agents that can touch your real cart or send a real message need hard limits: approvals tied to the exact listing, owner-only side effects, and an executor that physically cannot check out.

## Accomplishments that we're proud of

- **Real answers, not links.** Ask "Is the Garmin Forerunner worth it?" and you get a verdict, a rating averaged across review sites, and pros and cons, each with a source.
- **From intent to action.** One request becomes a plan, parallel research, a verified price, an approved cart add and a confirmed cart, with the whole flow usable inside ASI:One.
- **Honest when unsure.** If a price can't be confirmed, Focal says so. Nothing is sent or bought without your approval.
- **No paid data APIs.** Live prices, listings, reviews, news and videos all come from public pages.
- **The web on a lens.** Any page, rebuilt to read comfortably on the G2 and opened by voice, then gone when you're done.

## What we learned

- **Small agents beat one big prompt.** Agents with one job each are easier to test, retry and run in parallel.
- **Accuracy is the product.** One wrong match, like a phone case passed off as earbuds, costs more trust than a slow answer. Most of our time went into telling the right product apart from look-alikes.
- **Live data needs a plan B.** Rate limits and bot checks are normal, so every agent needs retries, caching and an honest "I couldn't confirm this".
- **Quiet is a design decision.** On a lens, every extra word costs attention. We cut screens down until each one said one thing.
- **Keep people in control.** Agents can prepare a cart or a message, but the final send or buy stays a human tap.

## What's next for Focal

- **Learning your priorities.** Focal already remembers your preferences and decisions. Next, it learns from them automatically.
- **Videos on the glasses.** Play the videos Focal finds, or show a video's gist as captions on the lens.
- **A better voice model.** Speech recognition that holds up in loud stores and on the street.
- **More stores and price alerts.** Grow the paid price watch into alerts that reach you on the glasses when a price drops.

## Built for each track

- **Applied Intelligence.** It solves an everyday problem, overpaying and buying blind, with AI that researches, verifies and acts for you, with safety limits built in.
- **Agentverse (Fetch.ai).** A multi-agent system registered on Agentverse, built on the Agent Chat Protocol and usable end to end in ASI:One. It uses Interactive Cards for every step, the Payment Protocol for a paid price watch, and verifier-backed retries for reliability.
- **Even Realities.** Built for one moment: you're holding a product and want to know if it's worth buying, without pulling out your phone. The answer appears in your line of sight when you ask and gets out of the way when you're done. It's lifestyle help, never checkout or payments on the glasses.
- **Paper.** The landing page and Studio were designed in Paper first, with one calm visual system shared across the web pages and the lens.
- **Cognition (Devin).** Devin wrote code for us: scoped tasks shipped as reviewed, merged pull requests while we built the glasses app in parallel.
- **Visa.** Covers discovery, decision-making and personalization, plus the start of checkout: trusted, sourced prices and an approval-gated cart. Payments happen through the Payment Protocol, with no payment code in the cart path.

---

### Before submitting (checklist from the track pages)

- **Fetch.ai (required):** agent registered on agentverse.ai, the demo shows the interaction from asi1.ai, agent profiles and chats submitted through the **Submission Agent** as well as Devpost, public GitHub repo with run instructions.
- **Even Realities (required):** app uploaded to Even Hub as a **Beta build**, runs on a physical G2 at judging, Even Hub app name in the submission ("Focal" ✓, 20 characters or fewer, no "Even"), G2 shown in the video, README install steps including any keys a tester needs.
- **Even Realities rule:** no financial apps. Keep payments out of the glasses framing. They live in the ASI:One flow only.
- **Visa:** only if you add the track. The text above already covers its criteria.
