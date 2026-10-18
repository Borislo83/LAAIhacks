// Hardcoded voice routes: "open the AirPods page on Apple" goes straight to a known page
// instead of a web search or the agents. Add a route by adding a row to ROUTES.

export interface Route {
  url: string
  /** Every pattern must match the command (after the open verb is found). */
  needs: RegExp[]
  /** At least one must match, when given (e.g. the site, or the product line). */
  anyOf?: RegExp[]
}

export const ROUTES: Route[] = [
  {
    url: 'https://www.apple.com/airpods-pro/',
    needs: [/\bair\s*pods?\b/i],
    anyOf: [/\bapple(?:'s|\.com)?\b/i, /\bpro\b/i, /\bpage\b/i],
  },
]

/** Asking to go somewhere: "open (up)", "go to", "take me to", "show me", "pull up"... */
const OPEN = /^(?:(?:hey|ok|okay|please|can you|could you|would you|focal)[\s,]+)*(?:open(?:\s+up)?|go\s+to|take\s+me\s+to|bring\s+up|pull\s+up|show(?:\s+me)?|navigate\s+to|load|visit|launch|head\s+to|get\s+me\s+to)\b/i
/** A shopping or research request about the product is for the agents, not a route. */
const NOT_A_ROUTE = /\b(?:price|prices|pricing|compare|comparison|cheap(?:er|est)?|deals?|reviews?|rating|buy|cart|cost|how\s+much|vs\.?|versus|alternatives?)\b/i

/** The page a spoken command asks to open, or null. */
export function routeFor(command: string): string | null {
  // A capture can hold more than one request ("Play Starboy. Open the AirPods page."):
  // the last sentence that asks to open a known page wins.
  const sentences = command.split(/(?<=[.!?])\s+/).map(t => t.trim().replace(/[.!?]+$/, '')).filter(Boolean)
  for (const text of sentences.reverse()) {
    if (!OPEN.test(text) || NOT_A_ROUTE.test(text)) continue
    for (const route of ROUTES) {
      if (route.needs.every(p => p.test(text)) && (!route.anyOf || route.anyOf.some(p => p.test(text)))) return route.url
    }
  }
  return null
}
