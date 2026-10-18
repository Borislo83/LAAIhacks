import { test } from 'node:test'
import assert from 'node:assert/strict'
import Anthropic from '@anthropic-ai/sdk'
import { JSDOM } from 'jsdom'
import { recommendationRoutes } from '../server/recommendations'
import { mountRecommendations } from '../src/recommendations'
import {
  CAPABILITIES, capabilityCommand, makeAction, mergeSessionActions, pickCapabilities, RECOMMENDATION_SELECTED,
  shoppingActions, STRIP_HEADING, TITLE_MAX, validBatch, type RecommendationBatch, type RecommendationContext,
  type SessionActionLike,
} from '../shared/recommendations'

const article: RecommendationContext = { url: 'https://example.com/hearing', title: 'AirPods Pro 2 get a hearing aid mode',
  text: 'Apple says the AirPods Pro 2 hearing aid feature is now cleared by the FDA. The Hearing Test runs on iPhone.', kind: 'reference' }
const product: RecommendationContext = { url: 'https://www.amazon.com/dp/B0D1XD1ZV3', title: 'Apple AirPods Pro 2 Wireless Earbuds',
  text: 'Active noise cancellation, USB-C charging case, hearing health features.', kind: 'shop', product: 'Apple AirPods Pro 2' }
const longArticle: RecommendationContext = { ...article, text: 'word '.repeat(700).trim() + ' AirPods Pro 2' }
const proposed = (id: string, type: string, label: string, status = 'proposed'): SessionActionLike => ({ action_id: id, type, label, status })
const compareAction = proposed('act-1', 'compare_prices', 'Compare prices and alternatives')
const reviewsAction = proposed('act-2', 'find_reviews', 'Check independent reviews')
const cartAction = proposed('act-3', 'add_to_amazon_cart', 'Add to Amazon cart')

function api(output: unknown, status = 200) {
  const requests: Record<string, any>[] = []
  const client = new Anthropic({ apiKey: 'test-only', maxRetries: 0, fetch: async (_input, init) => {
    requests.push(JSON.parse(init!.body as string))
    return Response.json(status === 200 ? { id: 'msg_test', type: 'message', role: 'assistant', model: 'claude-opus-5-5',
      stop_reason: 'end_turn', content: [{ type: 'text', text: JSON.stringify(output) }],
      usage: { input_tokens: 10, output_tokens: 10 } } : { error: { type: 'api_error', message: 'private upstream detail' } }, { status })
  } })
  const app = recommendationRoutes(() => client)
  const send = (body: unknown = article) => app.request('/', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
  return { requests, send }
}

// ── Catalog and validation ──────────────────────────────────────────────────

test('every catalog title fits the glasses strip', () => {
  for (const capability of Object.values(CAPABILITIES)) assert.ok(capability.title.length <= TITLE_MAX, capability.title)
})

test('product pages get exactly the shopping capabilities, with no model request', async () => {
  assert.deepEqual(shoppingActions(product)!.map(a => a.title), ['Compare prices', 'Review summary'])
  assert.equal(shoppingActions({ ...product, kind: 'news' }), null)
  assert.equal(shoppingActions({ ...product, product: undefined }), null)
  const { send, requests } = api({ actions: [] })
  const response = await send(product)
  assert.equal(response.status, 200)
  const batch = await response.json() as RecommendationBatch
  assert.equal(batch.source, 'catalog')
  assert.deepEqual(batch.actions.map(a => [a.capability, a.query]),
    [['compare_prices', 'Apple AirPods Pro 2'], ['review_summary', 'Apple AirPods Pro 2']])
  assert.ok(validBatch(batch))
  assert.equal(requests.length, 0)
})

test('Claude only chooses catalog ids; titles and descriptions are the server’s', async () => {
  const { send, requests } = api({ actions: [
    { capability: 'web_search', query: 'Hearing Test' },
    { capability: 'compare_prices', query: 'AirPods Pro 2' },
  ] })
  const response = await send()
  assert.equal(response.status, 200)
  const batch = await response.json() as RecommendationBatch
  assert.equal(batch.source, 'claude')
  assert.deepEqual(batch.page, { url: article.url, title: article.title })
  // Catalog order: prices before the general lookup.
  assert.deepEqual(batch.actions.map(a => [a.capability, a.title, a.query]),
    [['compare_prices', 'Compare prices', 'AirPods Pro 2'], ['web_search', 'Look this up', 'Hearing Test']])
  assert.ok(validBatch(batch))
  assert.ok(requests[0].messages[0].content.includes(article.text))
  assert.match(requests[0].system, /ONLY choose capabilities from this fixed catalog/)
  assert.match(requests[0].system, /untrusted source material/)
  const items = requests[0].output_config.format.schema.properties.actions.items
  assert.deepEqual(items.properties.capability.enum, ['compare_prices', 'review_summary', 'latest_news', 'web_search', 'page_summary'])
})

test('unknown ids, the cart, duplicates and products not on the page are rejected', () => {
  const picked = pickCapabilities([
    { capability: 'book_flight', query: 'AirPods Pro 2' },
    { capability: 'add_to_amazon_cart', query: 'AirPods Pro 2' },
    { capability: 'compare_prices', query: 'Sony WH-1000XM5' },
    { capability: 'review_summary', query: 'airpods pro 2' },
    { capability: 'review_summary', query: 'AirPods Pro 2' },
    { capability: 'web_search', query: 'x'.repeat(101) },
    { capability: 'web_search', query: '' },
    'compare_prices', null,
  ], article)
  assert.deepEqual(picked.map(a => [a.capability, a.query]), [['review_summary', 'airpods pro 2']])
  assert.deepEqual(pickCapabilities('nope', article), [])
  assert.equal(validBatch({ source: 'claude', page: { url: article.url, title: article.title },
    actions: [{ ...makeAction('compare_prices', 'x'), capability: 'book_flight' }] }), false)
  // A cart option is never valid without its session action.
  assert.equal(validBatch({ source: 'claude', page: { url: article.url, title: article.title },
    actions: [makeAction('add_to_amazon_cart')] }), false)
  assert.equal(validBatch({ source: 'claude', page: { url: article.url, title: article.title },
    actions: [{ ...makeAction('compare_prices', 'x'), title: 'Book a flight' }] }), false)
})

test('the page summary is rare: only alone, on long-form pages', () => {
  const summary = { capability: 'page_summary', query: '' }
  assert.deepEqual(pickCapabilities([summary, { capability: 'web_search', query: 'AirPods Pro 2' }], longArticle)
    .map(a => a.capability), ['web_search'])
  assert.deepEqual(pickCapabilities([summary], article), [])
  assert.deepEqual(pickCapabilities([summary], { ...longArticle, kind: 'shop' }), [])
  assert.deepEqual(pickCapabilities([summary], longArticle).map(a => a.title), ['Summarize page'])
})

test('latest news only on news pages; at most three options', () => {
  const news = { capability: 'latest_news', query: 'AirPods Pro 2' }
  assert.deepEqual(pickCapabilities([news], article), [])
  assert.deepEqual(pickCapabilities([news], { ...article, kind: 'news' }).map(a => a.capability), ['latest_news'])
  const all = pickCapabilities([news, { capability: 'web_search', query: 'Hearing Test' },
    { capability: 'review_summary', query: 'AirPods Pro 2' }, { capability: 'compare_prices', query: 'AirPods Pro 2' }],
  { ...article, kind: 'news' })
  assert.deepEqual(all.map(a => a.capability), ['compare_prices', 'review_summary', 'latest_news'])
})

test('nothing relevant returns an empty list, not filler', async () => {
  const response = await api({ actions: [{ capability: 'compare_prices', query: 'Pixel 9' }] }).send()
  assert.equal(response.status, 200)
  assert.deepEqual((await response.json() as RecommendationBatch).actions, [])
})

test('invalid page inputs are rejected before making a paid model request', async () => {
  const { send, requests } = api({ actions: [] })
  for (const body of [null, {}, { ...article, url: 'javascript:alert(1)' }, { ...article, text: '' },
    { ...article, title: 'a'.repeat(501) }, { ...article, text: 'a'.repeat(16001) }, { ...article, kind: 'casino' },
    { ...article, product: 'p'.repeat(201) }]) {
    assert.equal((await send(body)).status, 400)
  }
  assert.equal(requests.length, 0)
})

test('page text limits count Unicode characters the same way as the reader', async () => {
  const { send } = api({ actions: [] })
  assert.equal((await send({ ...article, text: '🎧'.repeat(16000) })).status, 200)
  assert.equal((await send({ ...article, text: '🎧'.repeat(16001) })).status, 400)
})

test('malformed replies and model outages report a retryable error without upstream details', async () => {
  for (const output of [{ nope: true }, { actions: 'compare_prices' }]) {
    assert.equal((await api(output).send()).status, 502)
  }
  const response = await api({}, 500).send()
  assert.equal(response.status, 502)
  assert.doesNotMatch(await response.text(), /private upstream detail|test-only/)
})

// ── Session actions: one strip ──────────────────────────────────────────────

test('product page with session actions: Compare prices, Add to Amazon cart, Review summary, each opening its action', () => {
  const merged = mergeSessionActions(shoppingActions(product)!, [reviewsAction, cartAction, compareAction], true)
  assert.deepEqual(merged.map(a => [a.title, a.session_action_id]),
    [['Compare prices', 'act-1'], ['Add to Amazon cart', 'act-3'], ['Review summary', 'act-2']])
  assert.equal(merged[0].query, 'Apple AirPods Pro 2')
})

test('the cart appears only from its session action, only on purchase pages, only while proposed', () => {
  const base = shoppingActions(product)!
  assert.ok(!mergeSessionActions(base, [], true).some(a => a.capability === 'add_to_amazon_cart'))
  assert.ok(!mergeSessionActions(base, [cartAction], false).some(a => a.capability === 'add_to_amazon_cart'))
  assert.ok(!mergeSessionActions(base, [{ ...cartAction, status: 'approved' }], true).some(a => a.capability === 'add_to_amazon_cart'))
  // Once approved, compare goes back to the chat fast path.
  const after = mergeSessionActions(base, [{ ...compareAction, status: 'completed' }], true)
  assert.deepEqual(after.map(a => [a.capability, a.session_action_id]), [['compare_prices', undefined], ['review_summary', undefined]])
  // Labels map too when an older API leaves out the type.
  assert.deepEqual(mergeSessionActions([], [{ action_id: 'x', label: 'Compare offers', status: 'proposed' }], false)
    .map(a => a.capability), ['compare_prices'])
  // A session shopping action outranks the rare page summary.
  assert.deepEqual(mergeSessionActions([makeAction('page_summary')], [compareAction], true).map(a => a.capability), ['compare_prices'])
})

test('each capability runs the right chat command; session-backed ones run none', () => {
  const run = (capability: Parameters<typeof makeAction>[0], query?: string) => capabilityCommand(makeAction(capability, query), article)
  assert.equal(run('compare_prices', 'AirPods Pro 2'), 'compare prices for AirPods Pro 2')
  assert.equal(run('review_summary', 'AirPods Pro 2'), 'show me reviews for AirPods Pro 2')
  assert.equal(run('latest_news', 'AirPods'), 'latest news on AirPods')
  assert.equal(run('web_search', 'Hearing Test'), 'look up Hearing Test')
  assert.equal(run('page_summary'), null)
  assert.equal(run('add_to_amazon_cart'), null)
})

// ── Phone panel (the source of truth for the glasses strip) ─────────────────

function panelFor(request: (context: RecommendationContext, signal: AbortSignal) => Promise<RecommendationBatch>,
  events: Parameters<typeof mountRecommendations>[2] = {}) {
  const dom = new JSDOM('<button id="reader">Reader</button><div id="popup"></div>')
  const root = dom.window.document.querySelector<HTMLElement>('#popup')!
  const panel = mountRecommendations(root, request, events)
  return { dom, root, panel }
}
const catalogBatch = (context = product): RecommendationBatch =>
  ({ source: 'catalog', page: { url: context.url, title: context.title }, actions: shoppingActions(context)! })

test('the panel is headed "Focal can help" and selection runs without a second request', async () => {
  let requests = 0, closed = 0
  const ready: RecommendationBatch[] = [], selections: any[] = []
  const { dom, root, panel } = panelFor(async () => { requests++; return catalogBatch() }, {
    ready: batch => ready.push(batch), closed: () => closed++, selected: selection => selections.push(selection),
  })
  const reader = dom.window.document.querySelector<HTMLButtonElement>('#reader')!
  reader.focus()
  await panel.show(product)
  assert.match(root.textContent!, new RegExp(STRIP_HEADING))
  assert.doesNotMatch(root.textContent!, /Suggested by Claude/)
  assert.equal(root.hidden, false)
  assert.equal(root.querySelectorAll('[data-action]').length, 2)
  assert.equal(dom.window.document.activeElement, reader)
  assert.equal(ready.length, 1)
  panel.select(1)
  assert.equal(selections[0].action.capability, 'review_summary')
  assert.deepEqual(selections[0].context, product)
  assert.equal(root.querySelectorAll('[aria-pressed=true]').length, 1)
  panel.close()
  assert.equal(closed, 1)
  assert.equal(root.hidden, true)
  panel.select(0)
  assert.equal(selections.length, 1)
  assert.equal(panel.reopen(), true)
  root.querySelectorAll<HTMLButtonElement>('[data-action]')[0].click()
  assert.equal(selections[1].action.capability, 'compare_prices')
  assert.equal(requests, 1)
  panel.reset()
  assert.equal(panel.reopen(), false)
  dom.window.close()
})

test('session actions join the same strip in place, and selecting one carries its action id', async () => {
  const ready: RecommendationBatch[] = [], changed: RecommendationBatch[] = [], selections: any[] = []
  const { dom, root, panel } = panelFor(async () => catalogBatch(), {
    ready: b => ready.push(b), changed: b => changed.push(b), selected: s => selections.push(s),
  })
  await panel.show(product)
  panel.session(product, [compareAction, reviewsAction, cartAction], true)
  assert.equal(ready.length, 1)
  assert.equal(changed.length, 1)
  assert.deepEqual([...root.querySelectorAll<HTMLElement>('[data-action] strong')].map(e => e.textContent),
    ['Compare prices', 'Add to Amazon cart', 'Review summary'])
  // The same poll again changes nothing.
  panel.session(product, [compareAction, reviewsAction, cartAction], true)
  assert.equal(changed.length, 1)
  panel.select(1)
  assert.equal(selections[0].action.capability, 'add_to_amazon_cart')
  assert.equal(selections[0].action.session_action_id, 'act-3')
  assert.match(root.querySelector('[role=status]')!.textContent!, /approve/i)
  // Another page's actions are ignored.
  panel.session({ ...product, url: 'https://example.com/other' }, [], true)
  assert.equal(changed.length, 1)
  dom.window.close()
})

test('session actions alone open the strip when no options were requested; closing keeps new ones from reopening it', async () => {
  const ready: RecommendationBatch[] = []
  const { dom, root, panel } = panelFor(async () => { throw new Error('not requested') }, { ready: b => ready.push(b) })
  panel.session(product, [{ ...cartAction, status: 'running' }], true)
  assert.equal(ready.length, 0)
  panel.session(product, [compareAction], true)
  assert.equal(ready.length, 1)
  assert.equal(ready[0].source, 'session')
  assert.equal(root.hidden, false)
  panel.close()
  panel.session(product, [compareAction, reviewsAction], true)
  assert.equal(root.hidden, true)
  assert.equal(ready.length, 1)
  dom.window.close()
})

test('a page with nothing to offer stays hidden', async () => {
  const ready: unknown[] = []
  const { dom, root, panel } = panelFor(async () => ({ source: 'claude', page: { url: article.url, title: article.title }, actions: [] }),
    { ready: b => ready.push(b) })
  await panel.show(article)
  assert.equal(root.hidden, true)
  assert.equal(ready.length, 0)
  dom.window.close()
})

test('selection emits a structured event for the glasses', async () => {
  const { dom, root, panel } = panelFor(async () => catalogBatch())
  const selections: any[] = []
  dom.window.addEventListener(RECOMMENDATION_SELECTED, event => selections.push((event as CustomEvent).detail))
  await panel.show(product)
  const button = root.querySelectorAll<HTMLButtonElement>('[data-action]')[0]
  button.click()
  assert.equal(selections.length, 1)
  assert.equal(selections[0].source, 'catalog')
  assert.equal(selections[0].action.capability, 'compare_prices')
  assert.ok(selections[0].request_id)
  assert.equal(button.getAttribute('aria-pressed'), 'true')
  assert.match(root.querySelector('[role=status]')!.textContent!, /running/i)
  dom.window.close()
})

test('close during loading cancels the request and late results cannot reopen the panel', async () => {
  let resolve!: (result: RecommendationBatch) => void
  let signal!: AbortSignal
  const { dom, root, panel } = panelFor(async (_context, requestSignal) => {
    signal = requestSignal
    return new Promise(r => { resolve = r })
  })
  const pending = panel.show(article)
  root.querySelector<HTMLButtonElement>('[data-close]')!.click()
  assert.equal(signal.aborted, true)
  resolve(catalogBatch())
  await pending
  assert.equal(root.hidden, true)
  dom.window.close()
})

test('navigation discards stale results and Escape closes the current panel', async () => {
  let resolve!: (result: RecommendationBatch) => void
  const next = { ...product, url: 'https://example.com/new', title: 'New page' }
  const { dom, root, panel } = panelFor(async input => input.url === product.url ? new Promise(r => { resolve = r }) : catalogBatch(next))
  const pending = panel.show(product)
  panel.reset()
  await panel.show(next)
  resolve(catalogBatch())
  await pending
  assert.match(root.querySelector('[data-page]')!.textContent!, /New page/)
  root.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  assert.equal(root.hidden, true)
  dom.window.close()
})

test('the panel escapes text, rejects invalid batches and offers retry after failure', async () => {
  let attempts = 0
  const { dom, root, panel } = panelFor(async () => {
    attempts++
    if (attempts === 1) throw new Error('Service temporarily unavailable.')
    if (attempts === 2) return { ...catalogBatch(), actions: [{ ...makeAction('compare_prices', 'x'), capability: 'book_flight' as never }] }
    return { ...catalogBatch(), actions: catalogBatch().actions.map(a => ({ ...a, description: '<img src=x onerror=alert(1)>' })) }
  })
  await panel.show(product)
  assert.equal(root.querySelectorAll('[data-action]').length, 0)
  assert.equal(root.querySelector<HTMLButtonElement>('[data-retry]')!.hidden, false)
  root.querySelector<HTMLButtonElement>('[data-retry]')!.click()
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(root.querySelectorAll('[data-action]').length, 0)
  root.querySelector<HTMLButtonElement>('[data-retry]')!.click()
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(root.querySelectorAll('[data-action]').length, 2)
  assert.equal(root.querySelector('img'), null)
  dom.window.close()
})
