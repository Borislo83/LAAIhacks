# Web Reader for Even Realities G2

This is the merged reader: glasses-web-reader supplies the reading/Claude layouts,
LensOS supplies the review and confirmation flow, and the existing LAAIhacks
orchestrator supplies Supabase sessions, preferences and AI task recommendations.
See [merged setup and verification](docs/MERGE.md) for the backend connection.

Condense any web page into text that fits the G2's 576x288 display, then read it
with the temple touchpad or R1 ring.

```
phone (Even app WebView)            condense server                      the web
┌──────────────────────┐  /api/condense?url=…   ┌──────────────────┐   fetch   ┌─────┐
│ URL box + mirror     │ ─────────────────────▶ │ SSRF-guarded fetch│ ────────▶ │site │
│ glasses renderer     │ ◀───── JSON sections ─ │ Readability → text│           └─────┘
└──────────┬───────────┘                        │ optional Claude   │
           │ BLE (Even Hub SDK)                 └──────────────────┘
      G2 glasses
```

The server exists because the Even app WebView enforces CORS, so the app
can't fetch arbitrary sites itself.

## Smart view

Needs `ANTHROPIC_API_KEY`. Claude decides what kind of site the page is and builds a short deck of
screens for it:

  | Site kind | Screens |
  |---|---|
  | News | headline card with lead photo, key points with a pulled quote, timeline |
  | Shopping | boxed results list with thumbnails, price, ★ rating and stock (tap opens the product); product card |
  | Software / creative tools | capability tiles, keyboard-shortcut cards, one-step-per-screen how-tos, plan comparison |
  | Forums | ranked feed; thread digest (verdict, agree vs pushback) |
  | Live info | big-number stat (rendered as an image), alert banner, forecast tiles |
  | Wikis / explainers | gist, fact sheet, timeline, comparison, tagged points |

  Images are converted on the server to 4-bit greyscale at the exact box size. On G2, lit pixels
  glow and black is see-through, so bright photos become line art, white product backgrounds are
  knocked out, and dark images stay dithered photos.

## Run it

```bash
npm install
cp .env.example .env          # add ANTHROPIC_API_KEY (required)
npm run dev                   # condense server :8787 + Vite :5173 (proxied /api)
npm run simulate              # desktop simulator
npx evenhub qr --url http://<your-lan-ip>:5173   # real glasses
npm run qr                    # real glasses on isolated Wi-Fi (tunnel), see QR_TESTING.md
```

Deep link: `http://localhost:5173/?open=<url-encoded page>`
(it's `open`, not `url`, because Vite reserves `?url`).

## Focal can help (suggested next steps)

After a page loads, one strip on the G2 (and the matching phone panel, both headed
**Focal can help**) offers up to three next steps. Every option is a capability Focal
really has, from the fixed catalog in `shared/recommendations.ts`:

| Option | What runs |
|---|---|
| **Compare prices** | chat `compare prices for <product>` (side-by-side card), or the watcher's `compare_prices` session action when proposed |
| **Add to Amazon cart** | only the approval-gated `add_to_amazon_cart` session action: review, confirm, approve (dry run by default). Never added directly |
| **Review summary** | chat `show me reviews for <product>`, or the `find_reviews` session action |
| **Latest news** | news pages only: chat `latest news on <topic>` (news agent) |
| **Look this up** | chat `look up <topic>` (search agent) |
| **Summarize page** | rare: the reader's own condensed deck, from its first screen |

On a product page (shop layout with one product) the options are Compare prices, Add to
Amazon cart (only when that session action exists for the page) and Review summary, with
no model call. Other pages ask Claude (`server/recommendations.ts`), which may only pick
catalog ids and copy a product or topic from the page; the server drops unknown ids, the
cart, queries not found in the page text, news off news pages, and keeps Summarize page
only when nothing else fits a long-form page. Nothing relevant means no strip.

Session actions the watcher proposes join the same strip (no second popup). Choosing one
opens its review → confirm → approve cards; the result still pops up as **Focal
finished** / **Focal couldn't finish** and the action card shows Done or Failed. Signed
out, chat options fall back to a web search.

On the glasses, swipe to choose, tap to select, and select **Close recommendations** or
double-tap to resume reading. The phone's X or Escape also closes it. Reopen it from the
reader menu (**Focal can help (n)**) without another request. Navigating clears it, and
closing while it loads cancels the request. **Recommendation layout** in Settings switches
between Corner popup, Bottom strip (default) and Full-screen list. The phone toggle and
the account's proactive-recommendations preference are respected. `npm run test:preview`
uses sample responses without model, account or agent calls.

## Voice search and scrolling

Say **"Hey Search"**, then a question, or choose **Voice search** in the menu or on
the phone. The server transcribes the clip (`STEPFUN_API_KEY` or `OPENAI_API_KEY`),
Claude picks one page from a web search, and that page opens. Hands-free listening is
on by default and can be switched off in the menu or on the phone. Pages opened by
voice don't auto-open suggestions (session actions still appear); open them from the menu with **Focal can help**.

To back out, swipe or double-tap. While it's listening this drops the question; while
it's searching or a page is loading, this cancels and returns to the page you were
on. The loading menu also has **Cancel**, and so does the phone's Back button.

When your account is connected (LensOS, below), the command first goes to the
orchestrator's `POST /v1/chat` through `/api/backend`. If chat called an agent, its
answer shows on the lens instead of a page: a news headline, summary and sources;
"Playing <title>" or the YouTube link; the Spotify line; or a short shopping summary.
Swipe pages through it, tap closes; hands-free answers fade back to the page. A
**message** ask shows the drafted iMessage: read to its last page, then **tap sends**
it through `POST /v1/messages/send`, and **swipe cancels**. Nothing is sent without that
tap, and a draft never fades. Dry runs, missing permission, failures and a disabled
executor (503) are reported as not sent. Signed out, no agent called, or any
gateway/chat error: the web search above runs as before.

Runs of plain-text screens join into one stream. A swipe scrolls it a few lines at
a time, and the counter in the top-right corner shows the screen as 1/4.

## On the glasses

| Gesture | Reader | Menus |
|---|---|---|
| Swipe down / up | next / previous screen (moves the selection on list screens) | move cursor |
| Tap | open the selected list item, otherwise the menu | select |
| Double-tap | back; cancels while listening, searching or loading; on a page with nothing to back out of, opens the menu (Exit is a menu item) | back to the page |

Menu: Resume · Focal can help · Voice search · ◀ back to the previous page · Contents ·
Links on page · Bookmark · Bookmarks · Recent pages · Agent ▸ · Settings ▸ · Exit (manga
swaps Contents and Links for chapter and page items). Settings holds the recommendation
and hands-free toggles; Agent holds saved suggestions, session actions and task history.

The last page you read and your recent pages are saved with `bridge.setLocalStorage`
and restored on the next launch.

With an account connected, pages also sync to the orchestrator. The phone panel and
glasses menu provide Recommendations, Session actions and Saved task history.
Read the action details before confirming. Double-tap goes back during action
review; outside that view it retains the reader's exit behavior. The phone's
Back / cancel button follows the same review navigation.

## Layout

- `server/safe-fetch.ts`: fetches with a timeout, a 4 MB cap and charset decoding. Every
  redirect hop is checked against private, loopback and metadata IPs.
  `ALLOW_PRIVATE_HOSTS=1` turns this off for local testing.
- `server/extract.ts`: HTML → `{title, sections[{heading, blocks}], links}`. Also maps
  glyphs the G2 font may lack (smart quotes, dashes, arrows, emoji) to plain equivalents.
- `server/deck.ts`: Smart view. Claude picks the site kind and fills layout fields with hard character limits; validated and resolved (images, links) on the server.
- `server/images.ts`: G2 image prep (dither / line art / background knockout) and big-number rendering.
- `src/screens.ts`: one renderer per layout, within the device limits (8 text, 4 image containers).
- `paginate` (`src/lensos/agents.ts`): pixel-accurate pagination with `@evenrealities/pretext`. Each section
  starts a new page, and paragraphs flow across page breaks.
- `src/main.ts`: the glasses UI (reader = body + footer text containers; menus = list
  container), with all bridge calls serialized and given timeouts.
- `src/companion.ts`: the phone UI.

## Shipping a .ehpk

1. Deploy the server anywhere that runs Node (`npx tsx server/index.ts`). It's a plain
   Hono app, so Fly, Render, Railway or Vercel all work. Set `ANTHROPIC_API_KEY` there.
2. Build the app against it: `VITE_API_BASE=https://your-server.example.com npm run pack`
3. Put that same origin in `app.json` → `permissions[0].whitelist` (it is a placeholder now).
