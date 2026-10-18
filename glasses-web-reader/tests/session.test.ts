import { test } from 'node:test'
import assert from 'node:assert/strict'
import { BackendSession } from '../src/lensos/session'
import { BackendClient } from '../src/lensos/client'
import { apiActionCards } from '../src/lensos/action-cards'
import type { CondensedPage } from '../shared/types'
import type { DecisionIntent, DisplayState, Task } from '../src/lensos/types'

const display: DisplayState = { schema_version: '1.0', session_id: 'session', revision: 2,
  page: { page_id: 'page', title: 'Test', kind: 'generic', summary: [], facts: [] }, mode: 'summary', notice: null,
  actions: [{ action_id: 'action', label: 'Find reviews', description: 'Compare published reviews.', status: 'proposed', requires_approval: true }] }
const page = (title: string): CondensedPage => ({ url: `https://example.com/${title}`, title, site: 'Example', wordCount: 1, links: [], sections: [{ heading: '', blocks: [{ type: 'p', text: title }] }] })
const task: Task = { id: 'task-one', context_id: 'context', kind: 'reviews', status: 'predicted', payload: { product: 'Model A' }, result: null, error: null, created_at: '2026-10-17T00:00:00Z' }
const preferences = { display_density: 'compact', shopping_priority: 'quality', proactive_enabled: true, notes: 'private notes' }

function harness(extra: (request: Request) => Promise<Response | undefined> = async () => undefined) {
  const requests: Request[] = []
  const client = new BackendClient('http://localhost/api/backend', 'signed-token', '', async (input, init) => {
    const req = new Request(input, init); requests.push(req.clone())
    const custom = await extra(req)
    if (custom) return custom
    const url = new URL(req.url)
    if (url.pathname.endsWith('/preferences')) return Response.json(preferences)
    if (url.pathname.endsWith('/tasks')) return Response.json([task])
    if (url.pathname.endsWith('/sessions')) return Response.json({ session_id: 'session', revision: 1 })
    if (url.pathname.endsWith('/display')) return Response.json(display)
    if (url.pathname.endsWith('/contexts')) return Response.json({ context_id: 'context', prediction_source: 'rules', tasks: [task] })
    return Response.json({ session_id: 'session', revision: 3 })
  })
  const session = new BackendSession({ changed() {}, saveSession() {}, pollMs: 0 })
  return { session, client, requests }
}

test('LensOS cards require review then confirmation, and retry the identical uncertain decision', () => {
  const sent: DecisionIntent[] = []
  const cards = apiActionCards(intent => sent.push(intent))
  cards.show(display); cards.setEnabled(true)
  cards.dispatch('tap'); cards.dispatch('tap'); cards.dispatch('tap')
  assert.equal(sent.length, 0)
  cards.dispatch('back'); assert.equal(sent.length, 0)
  cards.dispatch('tap'); cards.dispatch('tap'); cards.dispatch('tap')
  assert.equal(sent.length, 1)
  cards.finish(sent[0].request_id, 'uncertain', 'Response lost')
  cards.dispatch('tap')
  assert.deepEqual(sent[1], sent[0])
})

test('new revision invalidates a confirmation and long descriptions are fully reviewed', () => {
  const sent: DecisionIntent[] = []
  const cards = apiActionCards(i => sent.push(i))
  cards.show({ ...display, actions: [{ ...display.actions[0], description: 'A'.repeat(900) + ' final condition' }] }); cards.setEnabled(true)
  cards.dispatch('tap'); cards.dispatch('tap')
  const seen: string[] = []
  for (let n = 0; cards.view().title.includes('Review') && n < 40; n++) { seen.push(...cards.view().lines); cards.dispatch('tap') }
  // Every row of the description was on some page, and the last one ends it.
  const text = seen.join(' ')
  assert.equal(text.match(/A/g)?.length, 900)
  assert.match(text, /A final condition Status: needs approval$/)
  assert.equal(cards.view().title, 'Confirm action')
  cards.show({ ...display, revision: 3 }); cards.dispatch('tap')
  assert.equal(sent.length, 0)
})

test('reopening actions cannot confirm a pending choice and back exits the summary', () => {
  const sent: DecisionIntent[] = []
  const cards = apiActionCards(i => sent.push(i))
  cards.show(display); cards.setEnabled(true); cards.open()
  cards.dispatch('tap'); cards.dispatch('tap')
  assert.ok(cards.view().title.includes('Confirm'))
  cards.open()
  assert.equal(sent.length, 0)
  assert.ok(cards.view().title.includes('Actions'))
  cards.dispatch('back')
  assert.equal(cards.dispatch('back'), 'exit')
})

test('offline review and uncertain requests can return to the reader without losing retry identity', () => {
  const sent: DecisionIntent[] = []
  const cards = apiActionCards(i => sent.push(i))
  cards.show(display); cards.setEnabled(true); cards.open(); cards.dispatch('tap')
  cards.setEnabled(false)
  cards.dispatch('back'); cards.dispatch('back')
  assert.equal(cards.dispatch('back'), 'exit')
  cards.setEnabled(true); cards.open()
  cards.dispatch('tap'); cards.dispatch('tap'); cards.dispatch('tap')
  cards.finish(sent[0].request_id, 'uncertain', 'Response lost')
  assert.equal(cards.dispatch('back'), 'exit')
  cards.open(); cards.dispatch('tap')
  assert.deepEqual(sent[1], sent[0])
})

test('reconnect checks session ownership and restores persisted history without auto-dispatch', async () => {
  const { session, client, requests } = harness()
  await session.connect(client, 'session')
  assert.equal(session.state.sessionId, 'session')
  assert.equal(session.state.history[0].id, 'task-one')
  assert.equal(requests.some(r => r.method === 'POST'), false)
  session.disconnect()
  assert.equal(session.state.history.length, 0)
})

test('new navigation suppresses late predictions and serializes page writes', async () => {
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  const writes: string[] = []
  const { session, client } = harness(async req => {
    if (req.url.endsWith('/page')) {
      const body = await req.json(); writes.push(body.title)
      if (body.title === 'first') await gate
    }
    return undefined
  })
  await session.connect(client)
  session.beginPage()
  const first = session.publish(page('first'))
  await new Promise(resolve => setImmediate(resolve))
  session.beginPage()
  const second = session.publish(page('second'))
  release(); await Promise.all([first, second])
  assert.deepEqual(writes, ['first', 'second'])
  assert.equal(session.state.pageTitle, 'second')
  assert.equal(session.state.tasks.length, 1)
  session.disconnect()
})

test('navigation during reconnect cannot restore an old page or its proposals', async () => {
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  const { session, client } = harness(async req => {
    if (req.url.endsWith('/preferences')) await gate
    return undefined
  })
  const connection = session.connect(client, 'session')
  await new Promise(resolve => setImmediate(resolve))
  session.beginPage()
  release(); await connection
  assert.equal(session.state.display, null)
  assert.equal(session.state.pageTitle, '')
  session.disconnect()
})

test('navigation clears the busy state owned by the previous page', async () => {
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  const { session, client } = harness(async req => {
    if (req.url.endsWith('/page')) await gate
    return undefined
  })
  await session.connect(client); session.beginPage()
  const save = session.publish(page('old'))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(session.state.busy, true)
  session.beginPage(); release(); await save
  assert.equal(session.state.busy, false)
  session.disconnect()
})

test('preference edits preserve private notes and exclude them from prediction input', async () => {
  const { session, client, requests } = harness()
  await session.connect(client)
  await session.updatePreferences({ shopping_priority: 'price', proactive_enabled: false, display_density: 'comfortable' })
  const saved = requests.find(r => r.method === 'PUT' && r.url.endsWith('/preferences'))!
  assert.equal((await saved.json()).notes, 'private notes')
  session.beginPage(); await session.publish(page('article'))
  const contexts = requests.filter(r => r.url.endsWith('/contexts'))
  for (const req of contexts) assert.equal((await req.text()).includes('private notes'), false)
  session.disconnect()
})

test('uncertain task execution polls status and never dispatches it twice', async () => {
  let dispatches = 0
  const { session, client } = harness(async req => {
    if (req.url.endsWith('/dispatch')) { dispatches++; throw new TypeError('lost response') }
    if (req.url.endsWith('/tasks/task-one')) return Response.json({ ...task, status: 'succeeded', result: { summary: 'Reviews ready' } })
    return undefined
  })
  await session.connect(client); session.beginPage(); await session.publish(page('product'))
  const intent: DecisionIntent = { request_id: 'decision', action_id: 'task:task-one', expected_revision: 2, operation: 'approve' }
  assert.equal((await session.decide('tasks', intent)).outcome, 'uncertain')
  assert.equal((await session.decide('tasks', intent)).outcome, 'success')
  assert.equal(dispatches, 1)
  assert.equal(session.state.tasks[0].status, 'succeeded')
  session.disconnect()
})
