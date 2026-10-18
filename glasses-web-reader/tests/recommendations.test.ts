import { test } from 'node:test'
import assert from 'node:assert/strict'
import Anthropic from '@anthropic-ai/sdk'
import { JSDOM } from 'jsdom'
import { recommendationRoutes } from '../server/recommendations'
import { mountRecommendations } from '../src/recommendations'
import { RECOMMENDATION_SELECTED, TITLE_MAX, type RecommendationBatch, type RecommendationContext } from '../shared/recommendations'

const context = { url: 'https://example.com/headphones', title: 'Model A headphones', text: 'Wireless headphones for $100 with a comfortable fit.' }
const actions = [
  { title: 'Compare prices', description: 'Check other stores for a better offer.', instruction: 'Compare current offers for Model A headphones.' },
  { title: 'Find alternatives', description: 'Explore similar wireless headphones.', instruction: 'Find comparable wireless headphones around $100.' },
  { title: 'Read owner reviews', description: 'See what listeners say about comfort.', instruction: 'Summarize owner reviews of Model A headphones.' },
]
const batch: RecommendationBatch = { source: 'claude', page: { url: context.url, title: context.title },
  actions: actions.map((action, i) => ({ ...action, id: `action-${i}` })) }

function api(output: unknown = { actions }, status = 200) {
  const requests: Record<string, any>[] = []
  const client = new Anthropic({ apiKey: 'test-only', maxRetries: 0, fetch: async (_input, init) => {
    requests.push(JSON.parse(init!.body as string))
    return Response.json(status === 200 ? { id: 'msg_test', type: 'message', role: 'assistant', model: 'claude-opus-5-5',
      stop_reason: 'end_turn', content: [{ type: 'text', text: JSON.stringify(output) }],
      usage: { input_tokens: 10, output_tokens: 10 } } : { error: { type: 'api_error', message: 'private upstream detail' } }, { status })
  } })
  const app = recommendationRoutes(() => client)
  const send = (body: unknown = context) => app.request('/', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
  return { requests, send }
}

test('Claude receives page context and returns three distinct actions with server-owned IDs', async () => {
  const { send, requests } = api()
  const response = await send()
  assert.equal(response.status, 200)
  const result = await response.json() as RecommendationBatch
  assert.equal(result.source, 'claude')
  assert.deepEqual(result.page, { url: context.url, title: context.title })
  assert.deepEqual(result.actions.map(({ id, ...action }) => action), actions)
  assert.equal(new Set(result.actions.map(action => action.id)).size, 3)
  assert.ok(result.actions.every(action => action.id.length > 0))
  assert.ok(requests[0].messages[0].content.includes(context.text))
  assert.equal(requests[0].output_config.format.type, 'json_schema')
})

test('invalid page inputs are rejected before making a paid model request', async () => {
  const { send, requests } = api()
  for (const body of [null, {}, { ...context, url: 'javascript:alert(1)' }, { ...context, text: '' },
    { ...context, title: 'a'.repeat(501) }, { ...context, text: 'a'.repeat(16001) }]) {
    assert.equal((await send(body)).status, 400)
  }
  assert.equal(requests.length, 0)
})

test('page text limits count Unicode characters the same way as the reader', async () => {
  const { send } = api()
  assert.equal((await send({ ...context, text: '🎧'.repeat(16000) })).status, 200)
  assert.equal((await send({ ...context, text: '🎧'.repeat(16001) })).status, 400)
})

test('incomplete, duplicate and malformed Claude results never become recommendations', async () => {
  for (const output of [{ actions: actions.slice(0, 2) }, { actions: [...actions, actions[0]] },
    { actions: [actions[0], actions[0], actions[2]] }, { actions: [{ ...actions[0], instruction: '' }, ...actions.slice(1)] }]) {
    const response = await api(output).send()
    assert.equal(response.status, 502)
    assert.equal((await response.json() as any).actions, undefined)
  }
})

test('titles longer than the glasses can show are rewritten once, then rejected', async () => {
  const long = [{ ...actions[0], title: 'Compare prices at other online stores' }, ...actions.slice(1)]
  const replies = [{ actions: long }, { actions }]
  const requests: Record<string, any>[] = []
  const client = new Anthropic({ apiKey: 'test-only', maxRetries: 0, fetch: async (_input, init) => {
    requests.push(JSON.parse(init!.body as string))
    return Response.json({ id: 'msg_test', type: 'message', role: 'assistant', model: 'claude-opus-5-5',
      stop_reason: 'end_turn', content: [{ type: 'text', text: JSON.stringify(replies[requests.length - 1] ?? { actions: long }) }],
      usage: { input_tokens: 10, output_tokens: 10 } })
  } })
  const send = () => recommendationRoutes(() => client).request('/', { method: 'POST',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify(context) })
  const fixed = await send()
  assert.equal(fixed.status, 200)
  assert.equal(requests.length, 2)
  assert.match(requests[1].messages.at(-1).content, new RegExp(`${TITLE_MAX} characters`))
  assert.ok((await fixed.json() as RecommendationBatch).actions.every(a => a.title.length <= TITLE_MAX))

  replies.length = 0
  requests.length = 0
  assert.equal((await send()).status, 502)
  assert.equal(requests.length, 2)
})

test('model outages report a retryable error without exposing upstream details', async () => {
  const response = await api({}, 500).send()
  assert.equal(response.status, 502)
  assert.doesNotMatch(await response.text(), /private upstream detail|test-only/)
})

function popup(request: (context: RecommendationContext, signal: AbortSignal) => Promise<RecommendationBatch> = async () => batch,
  events: Parameters<typeof mountRecommendations>[2] = {}) {
  const dom = new JSDOM('<button id="reader">Reader</button><div id="popup"></div>')
  const root = dom.window.document.querySelector<HTMLElement>('#popup')!
  const panel = mountRecommendations(root, request, events)
  return { dom, root, panel }
}

test('the glasses receive the same generated batch and can select and close without a second request', async () => {
  let requests = 0, closed = 0
  const ready: unknown[] = [], selections: any[] = []
  const { dom, root, panel } = popup(async () => { requests++; return batch }, {
    ready: (result, input) => ready.push([result, input]), closed: () => closed++,
    selected: selection => selections.push(selection),
  })
  await panel.show(context)
  assert.deepEqual(ready, [[batch, context]])
  panel.select(2)
  assert.deepEqual(selections[0].action, batch.actions[2])
  assert.equal(root.querySelectorAll('[aria-pressed=true]').length, 1)
  assert.equal(requests, 1)
  panel.close()
  assert.equal(closed, 1)
  assert.equal(root.hidden, true)
  panel.select(0)
  assert.equal(selections.length, 1)
  dom.window.close()
})

test('popup shows three actions plus one close control without stealing reader focus', async () => {
  const { dom, root, panel } = popup()
  const reader = dom.window.document.querySelector<HTMLButtonElement>('#reader')!
  reader.focus()
  await panel.show(context)
  assert.equal(root.hidden, false)
  assert.equal(root.querySelectorAll('[data-action]').length, 3)
  assert.equal(root.querySelectorAll('button:not([hidden])').length, 4)
  assert.equal(dom.window.document.activeElement, reader)
  root.querySelector<HTMLButtonElement>('[data-close]')!.click()
  assert.equal(root.hidden, true)
  dom.window.close()
})

test('dismissed recommendations reopen with working selections and no new Claude request', async () => {
  let requests = 0, reopened = 0
  const selections: any[] = []
  const { dom, root, panel } = popup(async () => { requests++; return batch }, {
    opened: () => reopened++, selected: selection => selections.push(selection),
  })
  await panel.show(context)
  panel.close()
  assert.equal(panel.reopen(), true)
  assert.equal(root.hidden, false)
  root.querySelectorAll<HTMLButtonElement>('[data-action]')[1].click()
  assert.deepEqual(selections[0].action, batch.actions[1])
  assert.equal(requests, 1)
  assert.equal(reopened, 1)
  panel.reset()
  assert.equal(panel.reopen(), false)
  assert.equal(root.hidden, true)
  dom.window.close()
})

test('selection emits the chosen action and page context for the glasses to run', async () => {
  const { dom, root, panel } = popup()
  const selections: any[] = []
  dom.window.addEventListener(RECOMMENDATION_SELECTED, event => selections.push((event as CustomEvent).detail))
  await panel.show(context)
  const button = root.querySelectorAll<HTMLButtonElement>('[data-action]')[1]
  button.click()
  assert.equal(selections.length, 1)
  assert.equal(selections[0].executor, 'fetch.ai')
  assert.deepEqual(selections[0].action, batch.actions[1])
  assert.deepEqual(selections[0].context, context)
  assert.ok(selections[0].request_id)
  assert.equal(button.getAttribute('aria-pressed'), 'true')
  assert.match(root.querySelector('[role=status]')!.textContent!, /running/i)
  dom.window.close()
})

test('close during loading cancels the request and late results cannot reopen the popup', async () => {
  let resolve!: (result: RecommendationBatch) => void
  let signal!: AbortSignal
  const { dom, root, panel } = popup(async (_context, requestSignal) => {
    signal = requestSignal
    return new Promise(r => { resolve = r })
  })
  const pending = panel.show(context)
  root.querySelector<HTMLButtonElement>('[data-close]')!.click()
  assert.equal(signal.aborted, true)
  resolve(batch)
  await pending
  assert.equal(root.hidden, true)
  dom.window.close()
})

test('navigation discards stale results and Escape closes the current popup', async () => {
  let resolve!: (result: RecommendationBatch) => void
  const { dom, root, panel } = popup(async input => input.url === context.url ? new Promise(r => { resolve = r }) :
    { ...batch, page: { url: input.url, title: input.title } })
  const pending = panel.show(context)
  panel.reset()
  await panel.show({ ...context, url: 'https://example.com/new', title: 'New page' })
  resolve(batch)
  await pending
  assert.match(root.textContent!, /New page/)
  assert.doesNotMatch(root.querySelector('[data-page]')!.textContent!, /Model A/)
  root.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  assert.equal(root.hidden, true)
  dom.window.close()
})

test('popup escapes model text and offers retry after failure', async () => {
  let attempts = 0
  const { dom, root, panel } = popup(async () => {
    if (++attempts === 1) throw new Error('Service temporarily unavailable.')
    return { ...batch, actions: batch.actions.map(action => ({ ...action, description: '<img src=x onerror=alert(1)>' })) }
  })
  await panel.show(context)
  assert.equal(root.querySelectorAll('[data-action]').length, 0)
  assert.equal(root.querySelector<HTMLButtonElement>('[data-retry]')!.hidden, false)
  root.querySelector<HTMLButtonElement>('[data-retry]')!.click()
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(root.querySelectorAll('[data-action]').length, 3)
  assert.equal(root.querySelector('img'), null)
  dom.window.close()
})
