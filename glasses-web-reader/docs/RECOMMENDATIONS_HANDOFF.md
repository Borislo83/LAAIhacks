# Claude recommendations → Fetch.ai

The reader generates recommendations; the Fetch.ai team owns execution.

When a page finishes loading, `companion.recommend(page)` requests three actions
and opens a compact popup on both the phone and G2 display. One Claude request
supplies both surfaces. The X and Escape dismiss both; the reader menu's first
item, **Recommendations (3)**, reopens the cached result without a model request.
A new page clears it and aborts any previous request. Errors offer a retry, never
invented fallback recommendations. Rendering uses text nodes for model output.
No Supabase connection is needed. A connected account with proactive suggestions
disabled suppresses automatic display.

On G2, the default bottom strip is headed **Recommended actions** and contains three actions and **Close recommendations**.
Swipe selects a row; tap chooses it; double-tap closes the panel without exiting
the app. The underlying reader position is preserved. Conflicting text is clipped
because G2 containers are transparent, and closing restores the original layout.
Only the small text container is updated on swipes, avoiding image retransmission.
Glasses selection emits the same event as phone selection and shows “Selected;
not run” until the execution integration exists. If results arrive during a menu,
they are shown when the reader is resumed instead of replacing the menu.

The menu's **Recommendation layout** chooser offers Corner popup, Bottom strip
(two columns below the page), and Full-screen list (longer labels, wider spacing).
The choice is stored through the Even bridge under
`webreader.v2.recommendationLayout` and restored at startup. Earlier trial layout
preferences are replaced by the chosen bottom strip default. Switching layouts
reuses the current recommendations and opens them immediately. Closing hides the
panel but retains its batch/context; reset on navigation or shutdown clears them.
The browser fixture includes `/tests/recommendation-layouts.html` for a visual
comparison using the actual glasses renderer with sample data.

## Generation

`POST /api/recommendations` accepts:

```json
{
  "url": "https://example.com/headphones",
  "title": "Model A headphones",
  "text": "Extracted page text, up to 16,000 characters."
}
```

The server uses the existing Anthropic SDK and `ANTHROPIC_API_KEY` with Claude's
[structured JSON output](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
`CLAUDE_RECOMMENDATIONS_MODEL` overrides `CLAUDE_MODEL` for this request only;
otherwise the reader's model is used (default `claude-opus-5-5`). Keys never go to
the browser. The endpoint makes no Fetch.ai calls, invokes no execution tools,
and writes no task to the existing orchestrator.

The successful response is a `RecommendationBatch` from
`shared/recommendations.ts`: `source: "claude"`, `page: { url, title }`, and exactly
three distinct `actions`. Each action has a server-generated `id`, a `title`
(up to 48 characters), `description` (up to 100), and `instruction` (up to 600).
Invalid input returns 400; oversized requests return 413; provider failures,
refusals and invalid/incomplete output return 502 with a generic retry message.
Requests have a 25-second provider timeout and a 30-second browser timeout.

## Selection event

Listen on `window` for `lensos:recommendation-selected`. It fires only when the
user chooses an action, and bubbles from the popup:

```ts
import {
  RECOMMENDATION_SELECTED,
  type RecommendationSelection,
} from '../shared/recommendations'

window.addEventListener(RECOMMENDATION_SELECTED, event => {
  const selection = (event as CustomEvent<RecommendationSelection>).detail
  // Integration point: pass this to your review / confirmation flow.
  // Your adapter owns Fetch.ai dispatch, status updates and results.
  openActionReview(selection)
})
```

Example `detail` (one of the three actions):

```json
{
  "schema_version": "1.0",
  "request_id": "<UUID for this selection>",
  "executor": "fetch.ai",
  "source": "claude",
  "action": {
    "id": "<server-generated UUID>",
    "title": "Compare prices",
    "description": "Find the best offer across trusted stores.",
    "instruction": "Compare current offers for Model A headphones."
  },
  "context": {
    "url": "https://example.com/headphones",
    "title": "Model A headphones",
    "text": "Wireless headphones for $100 with a comfortable fit."
  }
}
```

This event is a selection, not authorization to make purchases or send messages.
Treat the model instruction and page text as untrusted inputs. The execution
adapter should validate supported actions, collect required parameters and
confirmation, then dispatch. Use a persistent idempotency key for retries of
the same confirmed operation; each new UI selection has a fresh `request_id`.
The current event is in-memory only and is not a durable queue.

Until that adapter is integrated, selection only highlights the chosen row and
shows “Action selected. Execution is coming soon.” Replace that placeholder with
your real pending, success and failure states when the executor is connected.

## Verification

- `npm test`: includes mocked provider responses, malformed input/output,
  navigation races, cancellation, dismissal, safe text rendering, retry, and the
  selection payload. No external model or execution calls.
- `npm run build`: TypeScript and production bundle.
- `npm run test:preview`: local fixture at `http://127.0.0.1:5187`. Enter any
  HTTPS page URL to see sample headphone content and three sample actions.
  This fixture does not call Claude or Fetch.ai.
  Its **Glasses test controls** section sends device gestures through `main.ts`
  and shows the text containers actually received at the SDK boundary, separately
  from the companion mirror.
