// The Claude model for deck building and voice search, plus the request
// options that depend on it. Override with CLAUDE_MODEL.

export const MODEL = process.env.CLAUDE_MODEL ?? 'claude-haiku-5-5'

const isHaiku = MODEL.includes('haiku')

/**
 * Re-run a declined request on another model. Haiku has no server-side
 * fallback (a declined request just stays declined), so it sends nothing.
 */
export const refusalFallback = isHaiku
  ? {}
  : { betas: ['server-side-fallback-2026-07-01'], fallbacks: 'default' as const }

/** Haiku only takes the basic web search tool; Sonnet and Opus get dynamic filtering. */
export const webSearchType = isHaiku ? ('web_search_20250305' as const) : ('web_search_20260209' as const)
