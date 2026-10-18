# "Focal can help": recommendations from a fixed capability catalog

> **Superseded design.** Earlier versions of this note described free-form
> recommendations: Claude wrote three actions with a `title` (up to 48 characters),
> a `description` and a free-text `instruction`, a strip headed **Recommended
> actions**, a separate session-action popup, an `executor: "fetch.ai"` field on
> the selection event and a "Selected; not run" placeholder. None of that exists
> any more. Options are now chosen only from capabilities Focal really has, and
> each one runs when chosen. Source of truth: `shared/recommendations.ts`.

## What the wearer sees

When a page finishes loading, `companion.recommend(page)` asks for options and one
strip opens on the G2 display and the phone, headed **Focal can help** (the
`STRIP_HEADING` constant). It holds up to three options plus **Close
recommendations**. Swipe moves the cursor, tap chooses, double-tap closes the strip
without leaving the page. The reader menu's **Focal can help (n)** item reopens the
cached options without another request. A new page clears them and aborts any
request still running. Errors offer a retry on the phone; nothing is invented.

The Settings menu has **Recommendations: on/off** and a **Recommendation layout**
chooser (Corner popup, Bottom strip, Full-screen list), stored through the Even
bridge under `webreader.v2.recommendationLayout`. Bottom strip is the default.
G2 containers are transparent, so page text under the strip is clipped rather than
overlapped, and closing restores the page. Swipes update only the small text cells.
`tests/recommendation-layouts.html` compares the layouts with the real renderer.

## The capability catalog

`CAPABILITIES` in `shared/recommendations.ts` is the whole list, in priority order.
Every option title is fixed by the catalog and fits one strip cell (`TITLE_MAX`).

| id | Title | How it runs when chosen |
| --- | --- | --- |
| `compare_prices` | Compare prices | Watcher session action (review, confirm, approve) if one is proposed; else chat `compare prices for <product>`, shown as a side-by-side comparison when the reply has offers |
| `add_to_amazon_cart` | Add to Amazon cart | **Only** as the watcher's approval-gated session action on a product page. Never chosen by the model, never run without approval |
| `review_summary` | Review summary | Watcher session action if proposed; else chat `show me reviews for <product>` |
| `latest_news` | Latest news | Chat `latest news on <topic>` (news pages only) |
| `web_search` | Look this up | Chat `look up <topic>` |
| `page_summary` | Summarize page | The reader's own condensed deck (no request) |

Chat commands go to the orchestrator's `POST /v1/chat` when signed in; if no agent
answers (or signed out) the reader opens a web search instead.

## Where options come from

`POST /api/recommendations` (server/recommendations.ts) takes a
`RecommendationContext`: `{ url, title, text, kind?, product? }`.

- **Product pages** (`kind: "shop"` with one `product`): `shoppingActions()` returns
  Compare prices and Review summary without a model call (`source: "catalog"`).
- **Other pages**: Claude (structured JSON output, `CLAUDE_RECOMMENDATIONS_MODEL`
  or the reader's model) may pick only from `MODEL_CAPABILITIES`, each with a
  `query` copied from the page. `pickCapabilities()` drops unknown ids, the cart,
  queries not found on the page, news off news pages, and the page summary unless
  it is a long article with nothing else to offer (`source: "claude"`). An empty
  list is a valid answer.
- **Watcher session actions** (`compare_prices`, `find_reviews`,
  `add_to_amazon_cart` from `services/agents ... integration.py watch`) are merged
  in by `mergeSessionActions()`: one option per capability, an option backed by a
  session action carries its `session_action_id`, and the cart appears only from
  its session action on a product page. The batch keeps its `catalog`/`claude`
  source, or is `session` when only the watcher proposed options.

Invalid input returns 400, oversized requests 413, provider failures 502 with a
generic retry message. Keys stay on the server.

## Session actions on the lens

Choosing an option with a `session_action_id` opens that action's review card
(`src/lensos/action-cards.ts`): the action, its description (for the cart, the
exact listing: title, ASIN, price, condition, seller) and its status, in whole
words at real glyph widths, paged with swipes. A tap on the last page opens
**Confirm action** (Approve / Reject); only a tap there sends the decision.
Double-tap backs out one step; from a card opened on the strip it closes back to
the page.

After approval the card shows **Working: <title>**. The wearer can stay or close
it: if they close it, the result pops up over the page (**Focal finished** or
**Focal couldn't finish**, `src/lensos/action-popup.ts`), and its **View** opens the
result. The result card (**Done: <title>** / **Failed: <title>**) shows the
worker's message with its `Source:` URLs folded into one
`Sources: apple.com, amazon.com` line and the `Retrieved:` timestamp dropped,
paged with swipes. Every string sent to the glasses goes through
`src/lensos/glyphs.ts`, which maps or drops symbols the G2 font lacks.

## Selection event

The phone panel and the glasses both emit `lensos:recommendation-selected`
(`RECOMMENDATION_SELECTED`) on `window` when an option is chosen:

```json
{
  "schema_version": "1.0",
  "request_id": "<UUID for this selection>",
  "source": "catalog",
  "action": {
    "id": "session-<action_id>",
    "capability": "compare_prices",
    "title": "Compare prices",
    "description": "Side-by-side prices for AirPods Pro 3.",
    "query": "AirPods Pro 3",
    "session_action_id": "<watcher action_id, when there is one>"
  },
  "context": { "url": "https://www.apple.com/airpods-pro/", "title": "AirPods Pro 3", "text": "...", "kind": "shop", "product": "AirPods Pro 3" }
}
```

`main.ts` handles it (`runSuggestion`). A selection is never authorization: session
actions still need review, confirm and a tap, and page text is untrusted input.

## Verification

- `npm test`: catalog validation, model-output filtering, session merging, the
  strip layout, review/confirm/approve cards, result formatting, glyph mapping,
  navigation races and the gateway. No external model or execution calls.
- `npm run build`: TypeScript and production bundle.
- `npm run test:preview`: local fixture at `http://127.0.0.1:5187` with a sample
  product page, a sample `compare_prices` session action and glasses test controls
  that show the text containers received at the SDK boundary.
