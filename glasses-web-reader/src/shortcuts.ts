// Fixed pages for commands that should always open the same place (demo
// requests), checked before the agents and the web search.

interface Shortcut {
  url: string
  title: string
  matches: (command: string) => boolean
}

const SHORTCUTS: Shortcut[] = [
  {
    // "What are the most popular and interesting books right now?"
    url: 'https://www.goodreads.com/shelf/show/english-novels',
    title: 'English Novels · Goodreads',
    matches: c => /\b(books?|novels?|reads?|reading)\b/.test(c)
      && /\b(popular|interesting|best|top|trending|good|great|recommend\w*|suggest\w*|should i read)\b/.test(c),
  },
  {
    // "Show me a picture of The Starry Night."
    url: 'https://en.wikipedia.org/wiki/The_Starry_Night',
    title: 'The Starry Night · Wikipedia',
    matches: c => /\bstarry\s*night\b/.test(c),
  },
]

/** The fixed page for a heard or typed command, if it has one. */
export function shortcutFor(command: string): { url: string; title: string } | null {
  const c = command.toLowerCase()
  const hit = SHORTCUTS.find(s => s.matches(c))
  return hit ? { url: hit.url, title: hit.title } : null
}
