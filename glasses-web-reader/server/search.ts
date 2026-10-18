import Anthropic from '@anthropic-ai/sdk'
import type { SearchResult } from '../shared/types'

// Voice command → one page to open. Claude searches the web and picks the
// single best URL; the glasses app then opens it like a typed URL.

import { MODEL, refusalFallback, webSearchType } from './model'

const MAX_TURNS = 3

const SYSTEM = `You turn a spoken request from someone wearing smart glasses into ONE web page to open.
Search the web, then pick the single page that best answers the request: a specific article, product, listing or results page, not a homepage, unless they asked for a site by name.
Prefer pages that read well as text (no login walls, no video-only pages).
Reply with only a JSON object, no other text: {"url": "https://...", "title": "short page title"}
If nothing fits, reply {"url": null, "title": "why"}.`

let client: Anthropic | null = null

export async function findPage(command: string): Promise<SearchResult> {
  client ??= new Anthropic()
  const messages: Anthropic.Beta.BetaMessageParam[] = [{ role: 'user', content: command }]
  let response: Anthropic.Beta.BetaMessage | undefined
  // A long search can pause mid-turn; resending the turn lets the server carry on.
  for (let turn = 0; turn < MAX_TURNS; turn++) {
    response = await client.beta.messages.create({
      model: MODEL,
      max_tokens: 4000,
      ...refusalFallback,
      output_config: { effort: 'low' },
      system: SYSTEM,
      tools: [{ type: webSearchType, name: 'web_search', max_uses: 3 }],
      messages,
    })
    if (response.stop_reason !== 'pause_turn') break
    messages.push({ role: 'assistant', content: response.content })
  }
  if (!response) throw new Error('Search returned nothing')
  if (response.stop_reason === 'refusal') throw new Error('The search was declined')

  const text = response.content.flatMap(b => (b.type === 'text' ? [b.text] : [])).join('')
  const picked = parsePick(text)
  if (picked) return { query: command, ...picked }

  // No usable JSON: fall back to the top search result.
  for (const block of response.content) {
    if (block.type === 'web_search_tool_result' && Array.isArray(block.content) && block.content[0]) {
      return { query: command, url: block.content[0].url, title: block.content[0].title }
    }
  }
  throw new Error(`Couldn't find a page for "${command}"`)
}

function parsePick(text: string): { url: string; title: string } | null {
  const json = text.match(/\{[\s\S]*\}/)?.[0]
  if (!json) return null
  try {
    const { url, title } = JSON.parse(json) as { url?: unknown; title?: unknown }
    if (typeof url !== 'string' || !/^https?:\/\//.test(url)) return null
    return { url, title: typeof title === 'string' && title ? title : url }
  } catch {
    return null
  }
}
