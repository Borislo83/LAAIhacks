import type { CondensedPage } from '../../shared/types'
import type { PageSubmission, PageContext } from './types'

const clip = (s: string, n: number) => Array.from(s.replace(/[\u0000-\u0008]/g, '')).slice(0, n).join('')
const paragraphs = (page: CondensedPage) => page.sections.flatMap(s => s.blocks.map(b => b.text)).filter(t => t.trim())

export function toPageSubmission(page: CondensedPage, pageId: string, requestId: string): PageSubmission {
  return { schema_version: '1.0', request_id: requestId, page_id: pageId, url: page.url,
    kind: page.deck?.kind === 'shop' ? 'product' : ['news', 'reference'].includes(page.deck?.kind ?? '') ? 'article' : 'generic',
    title: clip(page.title || page.url, 200), summary: paragraphs(page).slice(0, 10).map(t => clip(t, 2000)),
    facts: [{ label: 'Source', value: clip(page.site || new URL(page.url).hostname, 1000) }],
    source_label: clip(page.site || 'Web Reader', 200), updated_at: new Date().toISOString() }
}

export function toContext(page: CondensedPage): PageContext {
  // Only a single-product screen identifies a product; never invent one from a listing.
  const products = page.deck?.screens.filter(s => s.layout === 'product') ?? []
  const product = products.length === 1 ? products[0].title.trim() : ''
  return { url: page.url, title: clip(page.title, 500), text: clip(paragraphs(page).join('\n'), 16000),
    ...(product ? { product: clip(product, 200) } : {}) }
}
