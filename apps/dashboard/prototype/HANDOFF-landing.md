# Landing page handoff — new Focal positioning

For whoever updates `focal.html`. The brand, pitch and video script now use the messaging
below (full context: [demo/PITCH_AND_VIDEO.md](../../../demo/PITCH_AND_VIDEO.md)). The landing
should say the same thing, so judges hear one story across the page, the pitch and the video.
Design rules in [README.md](README.md) are unchanged: paper ground, signal green, one
Instrument Serif italic word per heading, agent orbs, aperture logo.

## Decisions already made

- **Name stays Focal.** We considered Glance, Focus and Foci; Focal is distinct, easy to say
  and search, and matches the aperture logo. "Many foci at once" can live in body copy.
- **Tagline:** **Focal — your team, one glance away.**
- **Subline:** *Chase your goals. Keep your peace.*
- **Positioning:** general lifestyle help, not a financial app (Even Realities bans
  medical/financial/emergency apps). Shopping is the first use case, not the whole product.
- **Story:** quiet on the surface, powerful underneath. Simple to wear, not simple inside.

## Copy, section by section

| Section (current) | Current copy | New copy |
|---|---|---|
| `<title>` | Focal | Focal — your team, one glance away |
| Kicker (line 92) | Smart glasses with agents that act on what you see | Lifestyle agents behind your glasses |
| H1 (line 93) | Look once. See it *clearly.* | Your team, one *glance* away. |
| Lede (line 96) | …one agent answers on the lens in under a second. | Glance at something and a team of AI agents goes to work: the best trusted price, a better alternative, what reviewers really say. One short answer on your G2, then it steps aside. *Chase your goals. Keep your peace.* |
| Glance timeline caption | From your gaze to an answer on the lens | Same; see the timing note below |
| Agents section H2 | Agents, *thinking* | Keep. Sub: "A whole team on Fetch.ai's Agentverse, each with one job, working in parallel" |

Add three short sections after the agents strip (one italic word each):

1. **Quiet on the *surface*.** Focal stays out of sight until you glance at something,
   shows one short answer on your Even Realities G2, then gets out of the way.
2. **Powerful *underneath*.** An orchestrator reads what you're looking at and sends
   specialist Fetch.ai agents out at the same time. Every answer comes with its sources; if a
   number can't be backed up, Focal doesn't show it.
3. **A team that *grows* with you.** Set what matters to you in the Studio (price, quality or
   balanced) and every agent works to it. Focal remembers your choices across the glasses and
   the Studio. Your private notes never reach the AI. CTA: **Open Studio**.

Optional trust line near the CTA: "Focal only acts with your OK. It can add to your cart; it
never checks out."

## Fix before judges see it — current claims that aren't true

| Where | Problem | Change to |
|---|---|---|
| Lede + timeline (`STEPS`, 0.8 s counter) | "Under a second" / 0.8 s isn't what live search takes | Drop the number from the lede. Either relabel the timeline as an illustration or use a time measured in rehearsal |
| Reader agent orb + "Loading chapter 4 of Pride and Prejudice" | No reader agent exists yet (only price, alternatives, reviews, coordinator) | Label it "Coming soon" or remove it |
| Lens hints "nod to open", "tilt to open", "blink twice" | G2 has no camera, so blinking can't be detected; its inputs are mics, temple touch and the R1 ring | "tap to open", "swipe for more", "ring to open" |
| "Price agent checks 3 sellers", "Reading 1,204 reviews", "$299 at Best Buy", XM6 | Made-up numbers presented as live | Keep as clearly illustrative examples, or match the demo (Sony WH-1000XM5) |
| "Sign in" / "Open Studio" | Fine | Point Open Studio at `dashboard.html` |

## Learning — wording rule

A separate session ([learn-shopping-priority](#local_92452ec5-f13b-44a0-be66-82aad4cc2d55))
is building "learn your priority from what you approve and skip." Until it ships and is
merged, the landing says Focal **remembers** your choices and **grows with you**, never that
it **already learns** from your behaviour. Once merged, section 3 can add: "It notices what you
approve and skip, and adjusts — and always shows you why."

## Done looks like

- Tagline, subline and the three sections are on the page in the existing design language.
- Every row in "Fix before judges see it" is resolved.
- Reduced-motion still turns off the animations; page works at phone width.
- Thumbnail for Devpost (5:3) can be cut from the new hero.
