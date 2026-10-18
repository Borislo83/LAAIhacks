# Devpost: tracks and tech feedback (draft)

Tracks we're entering: **Applied Intelligence, Fetch.ai (Agentverse), Even Realities,
Paper, Cognition (Devin).** Lines marked **[check]** are guesses about the team's
experience. Confirm or rewrite them before pasting.

## Track sentences

**Applied Intelligence.** Focal is a team of AI agents behind your Even Realities G2
glasses. Look at a product or ask a question, and it reads the page, checks prices,
alternatives and reviews in parallel, and puts one short sourced answer in your line of
sight. If it can't verify a price, it doesn't show one.

**Fetch.ai (Agentverse).** A FOCAL coordinator uAgent sends price, alternatives and reviews
specialists out at the same time over typed messages, then combines their partial
results. Every agent speaks the Agent Chat Protocol, so ASI:One can reach each one by
itself. ASI:One also writes the review summaries and pulls product names out of chat.
Price-verifier and memory agents support the coordinator, and more agents handle news,
YouTube, Spotify and messaging.

**Even Realities ("Quiet Tech").** Focal turns any web page into screens built for the G2's
576×288 green display: Claude decides what kind of site it is (news, shopping, forum, live
stats) and builds a short deck for it, and photos are converted to 4-bit line art. You
say "hey search" or tap the touchpad or R1 ring. One "Focal can help" strip offers up to
three next steps, then gets out of the way. Actions like adding to an Amazon cart need
your approval and never reach checkout.

**Paper.** We designed the Focal landing page and Studio dashboard in a Paper file,
"Focal — Design", and built the web prototype from those frames. That kept the glasses
view, the Studio and the landing page in one visual language across a team of five.

**Cognition (Devin).** We used Devin to write code. It delivered the visual fixes for
our scroll-driven pitch page as a PR we reviewed and merged (`Borislo83/LAAIhacks#1`:
label collisions, framing, half-drawn objects and the phone layout). We also sent our
heavier, slower tasks to Devin sessions so the rest of the team could keep building the
glasses app in parallel. **[check: list anything else Devin wrote]**

## Tech feedback

**Fetch.ai uAgents / Agentverse / ASI:One.** Building concurrent uAgents was quick: typed
models made it easy to fan out to three specialists and still combine partial results
when one is slow. The Agent Chat Protocol let ASI:One reach every specialist without
extra glue code. The hard part was connecting agents to Agentverse with a mailbox and
getting ASI:One to discover them. You can only check that with a real account test end
to end, so we couldn't test it locally. A local Agentverse/ASI:One emulator, or a "why
isn't my agent discoverable" checklist, would have saved us time. **[check]**

**Even Realities G2 / Even Hub SDK.** The simulator and the `evenhub qr` flow got code onto
real glasses fast, and designing for a monochrome display where black is see-through
was a fun constraint. Two things slowed us down. The Even app WebView enforces CORS, so
we had to build a server just to fetch pages. And the display's 576×288 size and 4-bit
greyscale meant we built our own image pipeline. An official fetch proxy and an
image-conversion helper in the SDK would help every web-based Even Hub app. **[check]**

**Paper.** Designing straight onto artboards and getting real HTML/CSS back made it easy to
move from mockup to working prototype. Our main issue was drift: once the code moved on
(we changed our accent colour from blue to green), the Paper frames fell behind and we
had to update them by hand. A way to sync design tokens both ways between Paper and code
would fix that. **[check]**

**Devin (Cognition).** We gave Devin a specific visual-QA task on our pitch page, and it
came back as a clean, reviewable PR that we merged. It worked best with a tight brief and
a clear way to check the result (screenshots at set widths). Broader briefs needed more
back-and-forth. Seeing roughly how many credits a task will cost before starting it
would help hackathon teams plan. **[check]**
