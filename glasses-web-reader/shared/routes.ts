// Hardcoded voice routes: "open the AirPods page on Apple" goes straight to a known page
// instead of a web search or the agents. Add a route by adding a row to ROUTES.

export interface Route {
  url: string
  /** What the glasses call it ("Opened YouTube on your laptop"). */
  name: string
  /** Where it opens: on the laptop only (an app or site to use there), or the laptop and the glasses. */
  on: 'laptop' | 'both'
  /** When given, what follows the open verb must match it whole ("open YouTube", not "open X on YouTube"). */
  only?: RegExp
  /** Every pattern must match the command (after the open verb is found). */
  needs?: RegExp[]
  /** At least one must match, when given (e.g. the site, or the product line). */
  anyOf?: RegExp[]
}

export const ROUTES: Route[] = [
  {
    url: 'https://www.apple.com/airpods-pro/',
    name: 'the AirPods Pro page',
    on: 'both',
    needs: [/\bair\s*pods?\b/i],
    anyOf: [/\bapple(?:'s|\.com)?\b/i, /\bpro\b/i, /\bpage\b/i],
  },
  {
    // "Open YouTube" on its own; "open up Starboy on YouTube" is a video for the agents.
    url: 'https://www.youtube.com/',
    name: 'YouTube',
    on: 'laptop',
    only: /^(?:the\s+)?you\s*tube(?:\s+app)?(?:\s+on\s+(?:my\s+|the\s+)?(?:laptop|computer|mac(?:book)?))?$/i,
  },
  {
    url: 'spotify:',
    name: 'Spotify',
    on: 'laptop',
    only: /^(?:the\s+)?spotify(?:\s+app)?(?:\s+on\s+(?:my\s+|the\s+)?(?:laptop|computer|mac(?:book)?))?$/i,
  },
]

/** The only URLs the laptop may be asked to open. */
export const LAPTOP_URLS: ReadonlySet<string> = new Set(ROUTES.map(r => r.url))

/** Asking to go somewhere: "open (up)", "go to", "take me to", "show me", "pull up"... */
// An optional greeting with the assistant's name however speech-to-text spells it ("Hey, Foko!").
const OPEN = /^(?:(?:hey|hi|ok|okay|yo)\b[\s,!.]*(?:[a-z]+\b[\s,!.]+)?)?(?:(?:please|can you|could you|would you)[\s,]+)*(?:open(?:\s+up)?|go\s+to|take\s+me\s+to|bring\s+up|pull\s+up|show(?:\s+me)?|navigate\s+to|load|visit|launch|head\s+to|get\s+me\s+to)\b/i
/** A shopping or research request about the product is for the agents, not a route. */
const NOT_A_ROUTE = /\b(?:price|prices|pricing|compare|comparison|cheap(?:er|est)?|deals?|reviews?|rating|buy|cart|cost|how\s+much|vs\.?|versus|alternatives?)\b/i

/** The page a spoken command asks to open, or null. */
export function routeFor(command: string): string | null {
  return matchRoute(command)?.url ?? null
}

/** The route a spoken or typed command asks for, or null. */
export function matchRoute(command: string): Route | null {
  // A capture can hold more than one request ("Play Starboy. Open the AirPods page."):
  // the last sentence that asks to open a known page wins.
  const parts = command.split(/(?<=[.!?])\s+/).map(t => t.trim().replace(/[.!?]+$/, '')).filter(Boolean)
  // "Hey, Foko! Open the AirPods page." splits after the greeting: also try neighbours joined.
  const sentences = [...parts, ...parts.slice(0, -1).map((p, i) => `${p} ${parts[i + 1]}`)]
  for (const text of sentences.reverse()) {
    const verb = text.match(OPEN)
    if (!verb || NOT_A_ROUTE.test(text)) continue
    const rest = text.slice(verb[0].length).trim().replace(/[\s,]+please$/i, '')
    for (const route of ROUTES) {
      if (route.only && !route.only.test(rest)) continue
      if ((route.needs ?? []).every(p => p.test(text)) && (!route.anyOf || route.anyOf.some(p => p.test(text)))) return route
    }
  }
  return null
}
