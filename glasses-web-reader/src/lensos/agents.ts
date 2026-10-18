import { getTextWidth, measureTextWrap, pxTruncate } from '@evenrealities/pretext'
import { BackendError, type BackendClient } from './client'
import type { AgentCall, ChatReply, MessageSent } from './types'

// Voice commands for the Focal agents: the orchestrator's POST /v1/chat picks the
// agent (news, YouTube, Spotify, shopping, Messenger). Its reply becomes pages of
// lens text; anything chat can't route falls back to web search in main.ts.

/** Text width inside the reader body (576 minus its 4px padding). */
export const CARD_W = 568
/** Rows of answer text under each page's title line (9 rows fit the body). */
export const CARD_LINES = 8
const MAX_PAGES = 12

export interface MessageDraft { draft_id: string; recipient: string; text: string }
export type AgentAnswer =
  | { kind: 'answer'; title: string; pages: string[] }
  | { kind: 'draft'; title: string; pages: string[]; draft: MessageDraft }

const str = (v: unknown) => (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim() : '')

/** Wraps text to glasses rows at real glyph widths; a word wider than a row is split. */
export function wrap(text: string, width = CARD_W): string[] {
  const rows: string[] = []
  for (const paragraph of text.split('\n')) {
    let row = ''
    for (let word of paragraph.split(/\s+/).filter(Boolean)) {
      while (getTextWidth(word) > width) {
        let n = word.length - 1
        while (n > 1 && getTextWidth(word.slice(0, n)) > width) n--
        if (row) { rows.push(row); row = '' }
        rows.push(word.slice(0, n)); word = word.slice(n)
      }
      const next = row ? `${row} ${word}` : word
      if (row && measureTextWrap(next, width).lineCount > 1) { rows.push(row); row = word }
      else row = next
    }
    rows.push(row)
  }
  while (rows.length && !rows[rows.length - 1]) rows.pop()
  return rows
}

/** Pages of `CARD_LINES` rows, each under the title line. */
export function paginate(title: string, body: string): string[] {
  const rows = wrap(body)
  const heading = pxTruncate(title, CARD_W)
  const pages: string[] = []
  for (let i = 0; i < Math.max(rows.length, 1) && pages.length < MAX_PAGES; i += CARD_LINES) {
    // A paragraph gap doesn't open a page.
    if (i && !rows[i]) rows.splice(i, 1)
    pages.push([heading, ...rows.slice(i, i + CARD_LINES)].join('\n'))
  }
  return pages
}

function newsText(r: Record<string, unknown>) {
  const stories = Array.isArray(r.stories) ? r.stories.filter(s => s && typeof s === 'object') as Record<string, unknown>[] : []
  if (r.brief && stories.length) {
    return { title: str(r.headline) || 'News brief',
      body: stories.slice(0, 6).map(s => `• ${str(s.headline)}${str(s.publisher) ? ` (${str(s.publisher)})` : ''}\n${str(s.summary)}`).join('\n\n') }
  }
  const sources = Array.isArray(r.sources)
    ? [...new Set(r.sources.map(s => str((s as Record<string, unknown>)?.publisher)).filter(Boolean))].slice(0, 4) : []
  return { title: str(r.headline) || 'News',
    body: [str(r.summary), sources.length ? `Sources: ${sources.join(', ')}` : ''].filter(Boolean).join('\n\n') }
}

function youtubeText(r: Record<string, unknown>) {
  const title = str(r.title)
  // The laptop opens the video when the agent says so; otherwise the link is the answer.
  const opened = r.opened === true || r.played === true || r.playing === true
  if (opened && title) return { title: 'YouTube', body: `Playing ${title}${str(r.channel) ? `\n${str(r.channel)}` : ''}` }
  return { title: 'YouTube', body: [title, str(r.channel), str(r.url)].filter(Boolean).join('\n') || str(r.summary) }
}

const SHOPPING = new Set(['price', 'alternatives', 'reviews', 'buyer'])

function messageAnswer(call: AgentCall): AgentAnswer {
  const r = call.result ?? {}
  const draft = { draft_id: str(r.draft_id), recipient: str(r.recipient_masked) || 'your contact', text: typeof r.text === 'string' ? r.text.trim() : '' }
  if (/^[A-Za-z0-9_.:-]{1,160}$/.test(draft.draft_id) && draft.text && draft.text.length <= 1000 && !call.error) {
    return { kind: 'draft', title: `Message to ${draft.recipient}`, draft,
      pages: paginate(`Send to ${draft.recipient}?`, `"${draft.text}"`) }
  }
  // No draft: the agent asked who or what to send, or couldn't reach Messages.
  return { kind: 'answer', title: 'Messages',
    pages: paginate('Messages', `${str(r.summary) || call.error || 'No message was drafted.'}\nNothing was sent.`) }
}

/**
 * Lens pages for a chat reply, or null when chat didn't use an agent (or every
 * agent failed), so the caller falls back to web search.
 */
export function answerFor(reply: ChatReply): AgentAnswer | null {
  const message = reply.calls.find(c => c.kind === 'message')
  if (message) return messageAnswer(message)
  const done = reply.calls.filter(c => c.result && !c.error)
  if (!done.length) return null
  if (done.some(c => SHOPPING.has(c.kind))) {
    const title = str(reply.product) || 'Shopping'
    return { kind: 'answer', title, pages: paginate(title, str(reply.reply) || done.map(c => str(c.result!.summary)).join('\n\n')) }
  }
  const parts = done.map(c => c.kind === 'news' ? newsText(c.result!)
    : c.kind === 'youtube' ? youtubeText(c.result!)
    : { title: c.kind === 'spotify' ? 'Spotify' : c.kind[0].toUpperCase() + c.kind.slice(1), body: str(c.result!.summary) })
  const title = parts[0].title
  return { kind: 'answer', title, pages: paginate(title, parts.map(p => p === parts[0] ? p.body : `${p.title}\n${p.body}`).join('\n\n')) }
}

/**
 * Sends one voice command to chat when signed in. Signed out, no agent, or any
 * gateway/chat failure returns null: the caller searches the web instead.
 */
export async function askAgents(client: BackendClient | undefined, command: string, signal?: AbortSignal): Promise<AgentAnswer | null> {
  if (!client) return null
  try {
    const reply = await client.chat(command, signal)
    if (!reply || typeof reply.reply !== 'string' || !Array.isArray(reply.calls)) return null
    return answerFor(reply)
  } catch (e) {
    if (signal?.aborted) throw e
    return null
  }
}

/** Sends a confirmed draft exactly as shown and returns an honest line for the lens. */
export async function sendDraft(client: BackendClient, draft: MessageDraft, signal?: AbortSignal): Promise<string> {
  let sent: MessageSent
  try {
    sent = await client.sendMessage(draft.draft_id, draft.text, signal)
  } catch (e) {
    const status = e instanceof BackendError ? e.status : 0
    if (status === 503) return 'Sending is off: the messaging executor isn\'t set up. Nothing was sent.'
    if (status === 401 || status === 403) return 'Sign in to your account again to send. Nothing was sent.'
    if (status === 429) return 'Too many messages; wait a minute. Nothing was sent.'
    if (status === 404 || status === 409 || status === 410 || status === 422) return 'That draft expired or changed. Nothing was sent; ask again.'
    // A lost response may still have sent it.
    return 'No answer from Messages. Check Messages before trying again.'
  }
  const summary = str(sent.summary)
  if (sent.status === 'sent') return sent.dry_run ? `Dry run: nothing was really sent. ${summary}`.trim() : summary || `Sent to ${draft.recipient}.`
  if (sent.status === 'needs_permission') return `Not sent: Messages needs permission on the laptop. ${summary}`.trim()
  if (sent.status === 'open_link') return `Not sent from the glasses: finish it in Messages on your phone. ${summary}`.trim()
  return `Not sent. ${summary}`.trim()
}

export type CardGesture = 'tap' | 'up' | 'down' | 'back'
/** What a gesture on the card asks main.ts to do. Only 'send' sends, and only from a tap. */
export type CardStep = 'render' | 'close' | 'send' | 'cancel' | 'none'

/** An answer or draft on the lens: which page shows, its footer, and what each gesture does. */
export class AgentCard {
  page = 0
  sending = false
  constructor(readonly answer: AgentAnswer, readonly quiet: boolean) {}
  /** Hands-free answers fade back to the page; a draft never does, it waits for a tap. */
  get fades() { return this.quiet && this.answer.kind !== 'draft' }
  get text() { return this.answer.pages[this.page] }
  private get last() { return this.page >= this.answer.pages.length - 1 }
  footer() {
    const n = this.answer.pages.length
    const hint = this.answer.kind === 'draft'
      ? this.sending ? 'Sending...' : this.last ? 'Tap: send   Swipe: cancel' : 'Tap: next   Swipe: cancel'
      : this.last ? 'Tap: close' : 'Swipe: more   Tap: close'
    return pxTruncate(`${n > 1 ? `${this.page + 1}/${n}   ` : ''}${hint}`, CARD_W)
  }
  gesture(g: CardGesture): CardStep {
    if (this.answer.kind === 'draft') {
      if (this.sending) return 'none'
      if (g !== 'tap') return 'cancel'
      // The whole draft is read before the tap that sends it.
      if (!this.last) { this.page++; return 'render' }
      this.sending = true
      return 'send'
    }
    if (g === 'tap' || g === 'back' || (g === 'down' && this.last)) return 'close'
    if (g === 'up' && this.page === 0) return 'none'
    this.page += g === 'down' ? 1 : -1
    return 'render'
  }
}
