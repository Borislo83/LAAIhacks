import { getTextWidth, measureTextWrap, pxTruncate } from '@evenrealities/pretext'
import { POPUP_TAP_GRACE_MS } from './action-popup'
import { BackendError, type BackendClient } from './client'
import type { AgentCall, ChatReply, MessageSent } from './types'
import type { PageSpec } from '../screens'
import { lensText } from './glyphs'

// Voice commands for the Focal agents: the orchestrator's POST /v1/chat picks the
// agent (news, YouTube, Spotify, shopping, Messenger). Its reply becomes pages of
// lens text; anything chat can't route falls back to web search in main.ts. A laptop
// action (Spotify, or a video asked to play) is never replaced by a search, and neither is
// a chat that may have run but didn't answer in time.

/** Text width inside the reader body (576 minus its 4px padding). */
export const CARD_W = 568
/** Rows of answer text under each page's title line (9 rows fit the body). */
export const CARD_LINES = 8
const MAX_PAGES = 12

export interface MessageDraft { draft_id: string; recipient: string; text: string; /** Contact name, for re-recording. */ name?: string }
/** One choice in a side-by-side comparison: a store offer or a rival product. */
export interface CompareColumn { head: string; price: string; note: string; extra?: string }
export interface CompareTable { title: string; columns: CompareColumn[] }
export type AgentAnswer =
  | { kind: 'answer'; title: string; pages: string[]; compare?: CompareTable }
  | { kind: 'draft'; title: string; pages: string[]; draft: MessageDraft }

const str = (v: unknown) => (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim() : '')

/** Wraps text to glasses rows at real glyph widths; a word wider than a row is split. */
export function wrap(text: string, width = CARD_W): string[] {
  const rows: string[] = []
  for (const paragraph of text.split('\n')) {
    let row = ''
    for (let word of paragraph.split(/\s+/).filter(Boolean)) {
      while (getTextWidth(word) > width) {
        // The longest prefix that fits, by binary search (a long URL is hundreds of characters).
        const chars = Array.from(word)
        let lo = 1, hi = chars.length - 1
        while (lo < hi) {
          const mid = (lo + hi + 1) >> 1
          if (getTextWidth(chars.slice(0, mid).join('')) <= width) lo = mid
          else hi = mid - 1
        }
        if (row) { rows.push(row); row = '' }
        rows.push(chars.slice(0, lo).join('')); word = chars.slice(lo).join('')
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

/** Model replies arrive as Markdown; the lens shows plain text (no **, #, or link syntax). */
export function plain(text: string): string {
  return text
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '$1 ($2)')
    .replace(/(\*\*|__)(?=\S)([^*_\n]+?)\1/g, '$2')
    .replace(/`([^`\n]+)`/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[*+]\s+/gm, '- ')
    // An inline list run together on one line ("compare: * Noise ... * Battery"), not "5 * 3".
    .split('\n').map(line => /:\s\*\s+\S/.test(line) ? line.replace(/\s\*\s+(?=\S)/g, '\n- ') : line).join('\n')
}

/** Pages of `CARD_LINES` rows, each under the title line. */
export function paginate(title: string, body: string): string[] {
  // Measured as drawn: symbols the G2 font lacks ("⏸", "🔊") are mapped or dropped first.
  const rows = wrap(lensText(plain(body)))
  const heading = pxTruncate(lensText(title), CARD_W)
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
  // The orchestrator adds result.laptop {status, summary} when it was asked to play the
  // video; only status "opened" means it plays. Otherwise the link is the answer.
  const laptop = r.laptop && typeof r.laptop === 'object' ? r.laptop as Record<string, unknown> : null
  if (laptop?.status === 'opened') return { title: 'YouTube', body: `Playing ${title || 'the video'} on your laptop${str(r.channel) ? `\n${str(r.channel)}` : ''}` }
  const found = [title, str(r.channel), str(r.url)].filter(Boolean).join('\n') || str(r.summary)
  if (laptop) return { title: 'YouTube', body: [found, `Not opened on your laptop: ${str(laptop.summary) || 'it did not open.'}`].filter(Boolean).join('\n') }
  return { title: 'YouTube', body: found }
}

/** A call that may have changed something on the laptop: it is always shown, error or not. */
const laptopCall = (c: AgentCall) => c.kind === 'spotify' || (c.kind === 'youtube' && !!c.result?.laptop)

/** Shown when chat may still be running (or ran) an action but didn't answer in time. */
export const NO_ANSWER = 'No answer yet. Check the laptop.'

const SHOPPING = new Set(['price', 'alternatives', 'reviews', 'buyer'])

function messageAnswer(call: AgentCall): AgentAnswer {
  const r = call.result ?? {}
  const name = str(r.recipient_name)
  const draft = { draft_id: str(r.draft_id), recipient: str(r.recipient_masked) || 'your contact', text: typeof r.text === 'string' ? r.text.trim() : '', ...(name ? { name } : {}) }
  if (/^[A-Za-z0-9_.:-]{1,160}$/.test(draft.draft_id) && draft.text && draft.text.length <= 1000 && !call.error) {
    const who = name || draft.recipient
    return { kind: 'draft', title: `Message to ${who}`, draft,
      pages: paginate(`Send to ${who}?`, `"${draft.text}"${name ? `\n${draft.recipient}` : ''}`) }
  }
  // No draft: the agent asked who or what to send, or couldn't reach Messages.
  return { kind: 'answer', title: 'Messages',
    pages: paginate('Messages', `${str(r.summary) || call.error || 'No message was drafted.'}\nNothing was sent.`) }
}

const money = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? `$${v.toFixed(2).replace(/\.00$/, '')}` : '')
const TRUST_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 }
type Rec = Record<string, unknown>
const recs = (v: unknown): Rec[] => (Array.isArray(v) ? v.filter((x): x is Rec => !!x && typeof x === 'object') : [])

/**
 * 2-3 choices to show side by side: the product's best offer next to its rivals when the
 * reply has alternatives, otherwise the top store offers (verified first, then cheapest).
 */
export function compareTable(reply: ChatReply): CompareTable | undefined {
  const result = (kind: string) => reply.calls.find(c => c.kind === kind && c.result && !c.error)?.result as Rec | undefined
  const price = result('price'), alts = result('alternatives')
  const offerColumn = (o: Rec): CompareColumn => ({
    head: str(o.store) || 'Store', price: money(o.price) || 'No price',
    // The price agent marks each offer: verified (major store, new), or why not.
    note: o.condition === 'used/refurb' ? 'Used/refurb'
      : (o.verified ?? o.trust === 'high') ? 'Verified' : 'Unverified',
    extra: typeof o.rating === 'number' ? `${o.rating}★` : undefined,
  })
  const ranked = recs(price?.offers).filter(o => typeof o.price === 'number')
    .sort((a, b) => (TRUST_RANK[str(a.trust)] ?? 3) - (TRUST_RANK[str(b.trust)] ?? 3)
      || Number(a.condition === 'used/refurb') - Number(b.condition === 'used/refurb')
      || (a.price as number) - (b.price as number))
  const rivals = recs(alts?.alternatives)
  if (rivals.length) {
    const product = str(price?.product) || str(alts?.product) || str(reply.product)
    const best = (price?.best as Rec | undefined) ?? ranked[0]
    const columns: CompareColumn[] = []
    if (product) columns.push({ head: product, price: money(best?.price) || 'No price', note: str(best?.store) || 'This page' })
    for (const a of rivals) {
      if (columns.length >= 3) break
      columns.push({ head: str(a.product) || 'Alternative', price: money(a.price) || 'No price',
        note: str(a.store) || 'Lead', extra: typeof a.rating === 'number' ? `${a.rating}★` : undefined })
    }
    return columns.length >= 2 ? { title: product ? `${product} vs rivals` : 'Compare', columns } : undefined
  }
  if (ranked.length >= 2) {
    const product = str(price?.product) || str(reply.product)
    return { title: product ? `Prices: ${product}` : 'Prices', columns: ranked.slice(0, 3).map(offerColumn) }
  }
  return undefined
}

const COL_GAP = 8
/** The comparison as a lens layout: a title row, one bordered box per choice, a footer. */
export function compareSpec(table: CompareTable, footer: string): PageSpec {
  const n = table.columns.length
  const w = Math.floor((576 - COL_GAP * (n + 1)) / n)
  const inner = w - 2 * 10
  const cell = (c: CompareColumn) => [...wrap(c.head, inner).slice(0, 2), '', c.price, c.note, c.extra ?? '']
    .map(line => pxTruncate(line, inner)).join('\n').replace(/\n+$/, '')
  return {
    texts: [
      { x: 4, y: 2, w: 568, h: 36, text: pxTruncate(table.title, CARD_W), pad: 4 },
      ...table.columns.map((c, i) => ({ x: COL_GAP + i * (w + COL_GAP), y: 42, w, h: 204, text: cell(c), pad: 10, border: 2, color: 13, radius: 8 })),
      { x: 4, y: 252, w: 568, h: 34, text: footer, pad: 4 },
    ],
    images: [],
  }
}

/**
 * Lens pages for a chat reply, or null when chat didn't use an agent (or every
 * agent failed), so the caller falls back to web search.
 */
export function answerFor(reply: ChatReply): AgentAnswer | null {
  const message = reply.calls.find(c => c.kind === 'message')
  if (message) return messageAnswer(message)
  // A Spotify or laptop error is the answer: a web search would hide what happened.
  const done = reply.calls.filter(c => (c.result && !c.error) || laptopCall(c))
  if (!done.length) return null
  if (done.some(c => SHOPPING.has(c.kind))) {
    const title = str(reply.product) || 'Shopping'
    return { kind: 'answer', title, compare: compareTable(reply),
      pages: paginate(title, str(reply.reply) || done.map(c => str(c.result?.summary)).join('\n\n')) }
  }
  const parts = done.map(c => c.kind === 'news' || c.kind === 'search' ? newsText(c.result!)
    : c.kind === 'youtube' ? youtubeText(c.result!)
    : c.kind === 'spotify' ? { title: 'Spotify', body: str(c.result?.summary) || str(c.error) || "Spotify didn't answer." }
    : { title: c.kind[0].toUpperCase() + c.kind.slice(1), body: str(c.result!.summary) })
  const title = parts[0].title
  return { kind: 'answer', title, pages: paginate(title, parts.map(p => p === parts[0] ? p.body : `${p.title}\n${p.body}`).join('\n\n')) }
}

/**
 * Sends one voice command to chat when signed in. Signed out, no agent, or a failure
 * before chat could run (network, 502 from the gateway, 4xx) returns null: the caller
 * searches the web instead. A timeout or another 5xx means chat may have acted, so the
 * lens says so instead of searching.
 */
export async function askAgents(client: BackendClient | undefined, command: string, signal?: AbortSignal): Promise<AgentAnswer | null> {
  if (!client) return null
  try {
    const reply = await client.chat(command, signal)
    if (!reply || typeof reply.reply !== 'string' || !Array.isArray(reply.calls)) return null
    return answerFor(reply)
  } catch (e) {
    if (signal?.aborted) throw e
    const timedOut = (e as Error)?.name === 'AbortError' || (e as Error)?.name === 'TimeoutError'
    const status = e instanceof BackendError ? e.status : 0
    if (timedOut || status === 408 || (status >= 500 && status !== 502)) {
      return { kind: 'answer', title: 'Focal', pages: paginate('Focal', NO_ANSWER) }
    }
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
export type CardStep = 'render' | 'close' | 'send' | 'rerecord' | 'cancel' | 'none'

export const DRAFT_CHOICES = ['Send', 'Re-record', 'Cancel'] as const

/** A tap this soon after a draft appears was meant for what was on screen before it. */
export const DRAFT_TAP_GRACE_MS = POPUP_TAP_GRACE_MS

/** An answer or draft on the lens: which page shows, its footer, and what each gesture does. */
export class AgentCard {
  page = 0
  sending = false
  /** A draft's choice on its last page: Send, Re-record or Cancel. */
  choice = 0
  private readonly shownAt: number
  constructor(readonly answer: AgentAnswer, readonly quiet: boolean, private readonly now: () => number = Date.now) {
    this.shownAt = now()
  }
  /** Hands-free answers fade back to the page; a draft never does, it waits for a tap. */
  get fades() { return this.quiet && this.answer.kind !== 'draft' }
  private get table() { return this.answer.kind === 'answer' ? this.answer.compare : undefined }
  /** A short draft shows its choices under the text; a long one gets a choices page after it. */
  private get inlineChoices() { return this.answer.pages.length === 1 && this.answer.pages[0].split('\n').length <= CARD_LINES - 3 }
  private get count() {
    if (this.answer.kind === 'draft') return this.answer.pages.length + (this.inlineChoices ? 0 : 1)
    return this.answer.pages.length + (this.table ? 1 : 0)
  }
  /** The comparison, when it is the page on screen (it comes first). */
  get compare() { return this.page === 0 ? this.table : undefined }
  private choices() { return DRAFT_CHOICES.map((c, i) => `${i === this.choice ? '>' : ' '} ${c}`).join('\n') }
  get text() {
    if (this.answer.kind === 'draft' && this.last) {
      if (this.inlineChoices) return `${this.answer.pages[0]}\n\n${this.choices()}`
      return `${this.answer.title}\n\n${this.choices()}`
    }
    return this.answer.pages[this.page - (this.table ? 1 : 0)] ?? ''
  }
  private get last() { return this.page >= this.count - 1 }
  footer() {
    const n = this.count
    // Same hint style as every other card: "2/3  Swipe: more | Tap: close".
    const hint = this.answer.kind === 'draft'
      ? this.sending ? 'Sending...' : this.last ? 'Swipe: choose | Tap: confirm | Double: cancel' : 'Tap: next | Double: cancel'
      : this.last ? 'Tap: close' : 'Swipe: more | Tap: close'
    return pxTruncate(`${n > 1 ? `${this.page + 1}/${n}  ` : ''}${hint}`, CARD_W)
  }
  gesture(g: CardGesture): CardStep {
    if (this.answer.kind === 'draft') {
      if (this.sending) return 'none'
      if (g === 'back') return 'cancel'
      if (!this.last) {
        // The whole draft is read before the choices.
        if (g === 'tap' || g === 'down') { this.page++; return 'render' }
        if (this.page === 0) return 'none'
        this.page--; return 'render'
      }
      if (g === 'up' || g === 'down') {
        this.choice = (this.choice + (g === 'down' ? 1 : DRAFT_CHOICES.length - 1)) % DRAFT_CHOICES.length
        return 'render'
      }
      // A tap right as the draft appears was meant for the screen before it.
      if (this.now() - this.shownAt < DRAFT_TAP_GRACE_MS) return 'none'
      const picked = DRAFT_CHOICES[this.choice]
      if (picked === 'Cancel') return 'cancel'
      if (picked === 'Re-record') return 'rerecord'
      this.sending = true
      return 'send'
    }
    if (g === 'tap' || g === 'back' || (g === 'down' && this.last)) return 'close'
    if (g === 'up' && this.page === 0) return 'none'
    this.page += g === 'down' ? 1 : -1
    return 'render'
  }
}
