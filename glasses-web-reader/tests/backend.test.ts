import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Hono } from 'hono'
import { orchestratorGateway } from '../server/orchestrator'
import { BackendClient } from '../src/lensos/client'
import { toPageSubmission, toContext } from '../src/lensos/page'
import type { CondensedPage } from '../shared/types'

const page: CondensedPage = { url: 'https://shop.example/item', title: 'Headphones', site: 'Shop',
  wordCount: 50, sections: [{ heading: '', blocks: [{ type: 'p', text: 'Useful detail' }] }], links: [],
  deck: { kind: 'shop', screens: [{ layout: 'product', title: 'Model A', subtitle: '', price: '$100',
    rating: '', stock: '', description: 'Good sound' }] } }

test('page and prediction context retain reader information within backend constraints', () => {
  const saved = toPageSubmission(page, 'page-one', 'request-one')
  assert.equal(saved.kind, 'product')
  assert.equal(saved.title, 'Headphones')
  assert.ok(saved.summary.includes('Useful detail'))
  assert.equal(saved.page_id, 'page-one')
  assert.equal(toContext(page).product, 'Model A')
  assert.equal(toContext({ ...page, sections: [{ heading: '', blocks: [{ type: 'p', text: 'a'.repeat(20000) }] }] }).text.length, 16000)
  assert.equal(toContext({ ...page, deck: { kind: 'shop', screens: [{ layout: 'list', heading: 'Products', items: [] }] } }).product, undefined)
})

test('gateway preserves bearer, marks forwarded requests and refuses non-reader routes/origins', async () => {
  const calls: Request[] = []
  const transport: typeof fetch = async (input, init) => {
    calls.push(new Request(input, init)); return Response.json({ session_id: 's', revision: 1 })
  }
  const app = new Hono().route('/api/backend', orchestratorGateway({ base: 'http://127.0.0.1:8001', transport }))
  const response = await app.request('/api/backend/v1/sessions', { method: 'POST', headers: { Authorization: 'Bearer user-token', 'Content-Type': 'application/json' }, body: '{}' })
  assert.equal(response.status, 200)
  assert.equal(calls[0].url, 'http://127.0.0.1:8001/v1/sessions')
  assert.equal(calls[0].headers.get('authorization'), 'Bearer user-token')
  assert.ok(calls[0].headers.has('x-forwarded-for'))
  assert.equal(calls[0].redirect, 'error')
  assert.equal((await app.request('/api/backend/v1/chat', { method: 'POST', headers: { Authorization: 'Bearer user-token' } })).status, 404)
  assert.equal((await app.request('/api/backend/v1/tasks')).status, 401)
  assert.equal((await app.request('/api/backend/v1/tasks', { headers: { Authorization: 'Bearer user-token', Origin: 'https://evil.example' } })).status, 403)
  assert.equal(calls.length, 1)
})

test('gateway rejects insecure remote upstreams and public demo tokens', async () => {
  assert.throws(() => orchestratorGateway({ base: 'http://remote.example' }), /HTTPS/)
  const app = new Hono().route('/api/backend', orchestratorGateway({ base: 'http://127.0.0.1:8001' }))
  assert.equal((await app.request('/api/backend/v1/tasks', { headers: { Authorization: 'Bearer demo-all' } })).status, 401)
})

test('client uses orchestrator create contract and treats unchanged display as empty', async () => {
  const calls: Request[] = []
  const client = new BackendClient('http://localhost:5173/api/backend', 'user-token', '', async (input, init) => {
    calls.push(new Request(input, init))
    return calls.length === 1 ? Response.json({ session_id: 'session', revision: 1 }) : new Response(null, { status: 204 })
  })
  assert.equal((await client.createSession()).session_id, 'session')
  assert.deepEqual(await calls[0].json(), {})
  assert.equal(await client.display('session', 4), null)
  assert.ok(calls[1].url.endsWith('/display?after_revision=4'))
})

test('client refuses cross-session and malformed display responses', async () => {
  const client = new BackendClient('http://localhost:5173/api/backend', 'user-token', '', async () => Response.json({ schema_version: '1.0', session_id: 'other', revision: 2, page: null, actions: [], mode: 'summary', notice: null }))
  await assert.rejects(client.display('session'), /Invalid.*display/)
})
