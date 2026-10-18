// One recommendation surface, built only from capabilities Focal really has: the
// shopping agents (prices, reviews), the approval-gated Amazon cart session action,
// the news and web search agents, and the reader's own condensed page.

export const CAPABILITY_IDS = [
  'compare_prices', 'add_to_amazon_cart', 'review_summary', 'latest_news', 'web_search', 'page_summary',
] as const
export type CapabilityId = typeof CAPABILITY_IDS[number]

export interface Capability {
  title: string
  description: string
  /** Needs a product or topic copied from the page. */
  needsQuery: boolean
}

/** Fixed catalog. Titles fit one cell of the glasses' Bottom strip (TITLE_MAX). Order is priority. */
export const CAPABILITIES: Record<CapabilityId, Capability> = {
  compare_prices: { title: 'Compare prices', description: 'Side-by-side prices from the shopping agents.', needsQuery: true },
  add_to_amazon_cart: { title: 'Add to Amazon cart',
    description: 'Review the exact listing, then approve. Nothing is added without your approval.', needsQuery: false },
  review_summary: { title: 'Review summary', description: 'What reviewers and owners say, from the reviews agent.', needsQuery: true },
  latest_news: { title: 'Latest news', description: 'Recent coverage of this story from the news agent.', needsQuery: true },
  web_search: { title: 'Look this up', description: 'Ask the search agent about a topic on this page.', needsQuery: true },
  page_summary: { title: 'Summarize page', description: 'The reader’s condensed summary of this page.', needsQuery: false },
}
/** What Claude may choose. The cart only ever comes from its approval-gated session action. */
export const MODEL_CAPABILITIES = ['compare_prices', 'review_summary', 'latest_news', 'web_search', 'page_summary'] as const
export const SHOPPING_CAPABILITIES: CapabilityId[] = ['compare_prices', 'add_to_amazon_cart', 'review_summary']

export const STRIP_HEADING = 'Focal can help'
export const MAX_ACTIONS = 3

export interface RecommendationContext {
  url: string; title: string; text: string
  /** The reader's layout for the page (shared/types SiteKind). */
  kind?: string
  /** The one product a single-product page is about. */
  product?: string
}
export interface RecommendedAction {
  id: string
  capability: CapabilityId
  title: string
  description: string
  /** Product or topic taken from the page. */
  query?: string
  /** Selecting opens this session action's review → confirm → approve flow. */
  session_action_id?: string
}
export interface RecommendationBatch {
  /** catalog: decided from the page kind; claude: chosen from the catalog by Claude; session: proposed by the watcher. */
  source: 'catalog' | 'claude' | 'session'
  page: { url: string; title: string }
  actions: RecommendedAction[]
}

export interface RecommendationSelection {
  schema_version: '1.0'
  request_id: string
  source: RecommendationBatch['source']
  action: RecommendedAction
  context: RecommendationContext
}
export const RECOMMENDATION_SELECTED = 'lensos:recommendation-selected'

/** Fits one cell of the glasses' Bottom strip layout (about 244px) without truncation. */
export const TITLE_MAX = 24
export const DESCRIPTION_MAX = 100
export const QUERY_MAX = 100
export const PRODUCT_MAX = 200
const SITE_KINDS = ['news', 'shop', 'software', 'forum', 'live', 'reference']
/** Long enough that the reader's condensed summary is worth offering. */
export const LONG_FORM_WORDS = 600

const text = (value: unknown, max: number): value is string =>
  typeof value === 'string' && value.trim().length > 0 && Array.from(value.trim()).length <= max
const isCapability = (value: unknown): value is CapabilityId => CAPABILITY_IDS.includes(value as CapabilityId)

export function validContext(value: unknown): value is RecommendationContext {
  if (!value || typeof value !== 'object') return false
  const c = value as RecommendationContext
  if (!text(c.url, 2048) || !text(c.title, 500) || !text(c.text, 16000)) return false
  if (c.kind !== undefined && !SITE_KINDS.includes(c.kind)) return false
  if (c.product !== undefined && !text(c.product, PRODUCT_MAX)) return false
  try {
    const url = new URL(c.url)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
  } catch { return false }
}

export function validAction(action: unknown): action is RecommendedAction {
  if (!action || typeof action !== 'object') return false
  const a = action as RecommendedAction
  return text(a.id, 100) && isCapability(a.capability) && a.title === CAPABILITIES[a.capability].title &&
    text(a.description, DESCRIPTION_MAX) && (a.query === undefined || text(a.query, PRODUCT_MAX)) &&
    (a.session_action_id === undefined || text(a.session_action_id, 200)) &&
    // The cart is never offered without its approval-gated session action.
    (a.capability !== 'add_to_amazon_cart' || a.session_action_id !== undefined)
}

export function validBatch(value: unknown): value is RecommendationBatch {
  if (!value || typeof value !== 'object') return false
  const batch = value as RecommendationBatch
  return ['catalog', 'claude', 'session'].includes(batch.source) && !!batch.page && text(batch.page.url, 2048) &&
    text(batch.page.title, 500) && Array.isArray(batch.actions) && batch.actions.length <= MAX_ACTIONS &&
    batch.actions.every(validAction) && new Set(batch.actions.map(a => a.id)).size === batch.actions.length &&
    new Set(batch.actions.map(a => a.capability)).size === batch.actions.length
}

const describe = (capability: CapabilityId, query?: string) => {
  const what = query ? Array.from(query).slice(0, 60).join('') : ''
  const line = !what ? CAPABILITIES[capability].description : {
    compare_prices: `Side-by-side prices for ${what}.`,
    review_summary: `What reviewers say about ${what}.`,
    latest_news: `Recent news on ${what}.`,
    web_search: `Ask the search agent about ${what}.`,
    add_to_amazon_cart: CAPABILITIES.add_to_amazon_cart.description,
    page_summary: CAPABILITIES.page_summary.description,
  }[capability]
  return Array.from(line).slice(0, DESCRIPTION_MAX).join('')
}

export function makeAction(capability: CapabilityId, query?: string, session_action_id?: string): RecommendedAction {
  return { id: session_action_id ? `session-${session_action_id}` : `${capability}`, capability,
    title: CAPABILITIES[capability].title, description: describe(capability, query),
    ...(query ? { query } : {}), ...(session_action_id ? { session_action_id } : {}) }
}

/** A product/purchase page: the shopping capabilities, decided without a model call. */
export function shoppingActions(context: RecommendationContext): RecommendedAction[] | null {
  const product = context.product?.trim()
  if (context.kind !== 'shop' || !product) return null
  return [makeAction('compare_prices', product), makeAction('review_summary', product)]
}

/** Lowercase words only, so "AirPods Pro (2nd gen)" matches "airpods pro 2nd gen". */
export const normalize = (value: string) =>
  ` ${value.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim()} `
export const mentions = (context: RecommendationContext, query: string) =>
  normalize(query).trim().length > 0 && normalize(`${context.title}\n${context.text}`).includes(normalize(query))
export const longForm = (context: RecommendationContext) =>
  context.text.split(/\s+/).filter(Boolean).length >= LONG_FORM_WORDS

/**
 * Keeps only real capabilities from a model reply: unknown ids and the cart are dropped,
 * every query must be copied from the page, news only on news pages, and the page summary
 * only when nothing else fits a long-form page. At most three, in catalog order.
 */
export function pickCapabilities(raw: unknown, context: RecommendationContext): RecommendedAction[] {
  if (!Array.isArray(raw)) return []
  const picked = new Map<CapabilityId, RecommendedAction>()
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const capability = (item as { capability?: unknown }).capability
    if (!MODEL_CAPABILITIES.includes(capability as never) || picked.has(capability as CapabilityId)) continue
    const id = capability as CapabilityId
    if (id === 'page_summary') { picked.set(id, makeAction(id)); continue }
    const query = (item as { query?: unknown }).query
    if (typeof query !== 'string') continue
    const clean = query.replace(/[\u0000-\u001f]/g, ' ').replace(/\s+/g, ' ').trim()
    if (!clean || Array.from(clean).length > QUERY_MAX || !mentions(context, clean)) continue
    if (id === 'latest_news' && context.kind !== 'news') continue
    picked.set(id, makeAction(id, clean))
  }
  if (picked.size > 1 || (picked.has('page_summary') && (!longForm(context) || context.kind === 'shop'))) picked.delete('page_summary')
  return order([...picked.values()])
}

const order = (actions: RecommendedAction[]) =>
  [...actions].sort((a, b) => CAPABILITY_IDS.indexOf(a.capability) - CAPABILITY_IDS.indexOf(b.capability)).slice(0, MAX_ACTIONS)

/** The fields of a watcher session action this surface reads. */
export interface SessionActionLike { action_id: string; label: string; status: string; type?: string }

/** Which capability a proposed session action is (by its handler type, else its label). */
export function sessionCapability(action: SessionActionLike): CapabilityId | null {
  const type = action.type ?? ''
  if (type === 'add_to_amazon_cart' || /\bamazon\b.*\bcart\b/i.test(action.label)) return 'add_to_amazon_cart'
  if (type === 'find_reviews' || (!type && /\breviews?\b/i.test(action.label))) return 'review_summary'
  if (type === 'compare_prices' || (!type && /\b(?:compare|prices?|offers?)\b/i.test(action.label))) return 'compare_prices'
  return null
}

/**
 * The strip: the catalog/Claude options, with proposed session actions merged in so one
 * option = one capability. A session action replaces how its option runs (review and
 * approve). The cart appears only from its session action on a product/purchase page.
 */
export function mergeSessionActions(base: RecommendedAction[], session: SessionActionLike[], purchase: boolean): RecommendedAction[] {
  const proposed = new Map<CapabilityId, SessionActionLike>()
  for (const action of session) {
    const capability = action.status === 'proposed' ? sessionCapability(action) : null
    if (!capability || proposed.has(capability)) continue
    if (capability === 'add_to_amazon_cart' && !purchase) continue
    proposed.set(capability, action)
  }
  const product = base.find(a => a.capability === 'compare_prices' || a.capability === 'review_summary')?.query
  const merged = base.filter(a => a.capability !== 'add_to_amazon_cart').map(a => {
    const s = proposed.get(a.capability)
    if (!s) return a.session_action_id ? makeAction(a.capability, a.query) : a
    proposed.delete(a.capability)
    return makeAction(a.capability, a.query, s.action_id)
  })
  for (const [capability, s] of proposed) merged.push(makeAction(capability, capability === 'add_to_amazon_cart' ? undefined : product, s.action_id))
  // Shopping options outrank the rare page summary.
  const shopping = merged.some(a => SHOPPING_CAPABILITIES.includes(a.capability))
  return order(shopping ? merged.filter(a => a.capability !== 'page_summary') : merged)
}

/** The chat command for an option without a session action (the agents' fast paths). */
export function capabilityCommand(action: RecommendedAction, context: RecommendationContext): string | null {
  const query = action.query ?? context.product ?? context.title
  switch (action.capability) {
    case 'compare_prices': return `compare prices for ${query}`
    // The orchestrator's reviews rule needs a verb ("show me reviews for X").
    case 'review_summary': return `show me reviews for ${query}`
    case 'latest_news': return `latest news on ${query}`
    case 'web_search': return `look up ${query}`
    default: return null
  }
}
