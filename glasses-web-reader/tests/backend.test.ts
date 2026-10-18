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
  assert.equal((await app.request('/api/backend/v1/chat/stream', { method: 'POST', headers: { Authorization: 'Bearer user-token' } })).status, 404)
  assert.equal((await app.request('/api/backend/v1/tasks')).status, 401)
  assert.equal((await app.request('/api/backend/v1/tasks', { headers: { Authorization: 'Bearer user-token', Origin: 'https://evil.example' } })).status, 403)
  assert.equal(calls.length, 1)
})

test('gateway forwards voice chat and confirmed message sends, and nothing else of chat or messages', async () => {
  const calls: Request[] = []
  const transport: typeof fetch = async (input, init) => { calls.push(new Request(input, init)); return Response.json({ reply: 'ok', calls: [] }) }
  const app = new Hono().route('/api/backend', orchestratorGateway({ base: 'http://127.0.0.1:8001', transport }))
  const post = (path: string, method = 'POST') => app.request(`/api/backend${path}`, { method,
    headers: { Authorization: 'Bearer user-token', 'Content-Type': 'application/json' }, body: method === 'GET' ? undefined : '{"x":1}' })
  assert.equal((await post('/v1/chat')).status, 200)
  assert.equal((await post('/v1/messages/send')).status, 200)
  assert.deepEqual(calls.map(c => `${c.method} ${new URL(c.url).pathname}`), ['POST /v1/chat', 'POST /v1/messages/send'])
  assert.equal(await calls[1].text(), '{"x":1}')
  for (const [path, method] of [['/v1/chat', 'GET'], ['/v1/chat/stream', 'POST'], ['/v1/messages', 'POST'],
    ['/v1/messages/send/x', 'POST'], ['/v1/agents/news/stream', 'POST'], ['/v1/buyer/prepare/stream', 'POST']]) {
    assert.equal((await post(path, method)).status, 404, `${method} ${path}`)
  }
  assert.equal(calls.length, 2)
  assert.equal((await app.request('/api/backend/v1/messages/send', { method: 'POST', headers: { Authorization: 'Bearer demo-all' }, body: '{}' })).status, 401)
})

test('gateway answers 504 when the orchestrator times out and 502 when it is unreachable', async () => {
  const gateway = (error: Error) => new Hono().route('/api/backend', orchestratorGateway({ base: 'http://127.0.0.1:8001',
    transport: async () => { throw error } }))
  const chat = (app: Hono) => app.request('/api/backend/v1/chat', { method: 'POST',
    headers: { Authorization: 'Bearer user-token', 'Content-Type': 'application/json' }, body: '{}' })
  assert.equal((await chat(gateway(new DOMException('slow', 'TimeoutError')))).status, 504)
  assert.equal((await chat(gateway(new TypeError('fetch failed')))).status, 502)
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

test('local dev token is attached only for unforwarded requests on this machine', async () => {
  const calls: Request[] = []
  const transport: typeof fetch = async (input, init) => { calls.push(new Request(input, init)); return Response.json([]) }
  const app = new Hono().route('/api/backend', orchestratorGateway({ base: 'http://127.0.0.1:8001', transport, devToken: 'demo-all', peerAddress: c => c.req.header('x-test-peer') ?? '127.0.0.1' }))
  const local = { Authorization: 'Bearer local-dev' }

  assert.deepEqual(await (await app.request('/api/backend/dev')).json(), { local: true, pairing: false })
  assert.equal((await app.request('/api/backend/v1/tasks', { headers: local })).status, 200)
  assert.equal(calls[0].headers.get('authorization'), 'Bearer demo-all')
  assert.ok(!calls[0].headers.has('x-forwarded-for'), 'DEMO_AUTH_MODE only trusts unforwarded loopback calls')

  // Through a tunnel or proxy: no dev token, and the stand-in itself is refused.
  for (const extra of [{ 'CF-Connecting-IP': '203.0.113.9' }, { 'X-Forwarded-For': '203.0.113.9' }, { 'Cf-Ray': 'abc' }]) {
    assert.deepEqual(await (await app.request('/api/backend/dev', { headers: extra })).json(), { local: false, pairing: false })
    assert.equal((await app.request('/api/backend/v1/tasks', { headers: { ...local, ...extra } })).status, 401)
  }
  // Another device on the Wi-Fi: directly, or through the Vite proxy (which appends its address),
  // even when it forges a loopback X-Forwarded-For or Host.
  for (const extra of [{ 'x-test-peer': '172.26.1.9' }, { 'X-Forwarded-For': '127.0.0.1, 172.26.1.9' }, { 'x-test-peer': '172.26.1.9', Host: 'localhost' }]) {
    assert.deepEqual(await (await app.request('/api/backend/dev', { headers: extra })).json(), { local: false, pairing: false })
    assert.equal((await app.request('/api/backend/v1/tasks', { headers: { ...local, ...extra } })).status, 401)
  }
  // The simulator on this machine through Vite: every hop is loopback.
  assert.deepEqual(await (await app.request('/api/backend/dev', { headers: { 'X-Forwarded-For': '127.0.0.1' } })).json(), { local: true, pairing: false })
  // Callers still can't send demo tokens themselves.
  assert.equal((await app.request('/api/backend/v1/tasks', { headers: { Authorization: 'Bearer demo-all' } })).status, 401)
  assert.equal(calls.length, 1)
})

test('without a dev token configured, local-dev is just an invalid bearer', async () => {
  const app = new Hono().route('/api/backend', orchestratorGateway({ base: 'http://127.0.0.1:8001', transport: async () => Response.json([]) }))
  assert.deepEqual(await (await app.request('/api/backend/dev')).json(), { local: false, pairing: false })
  assert.equal((await app.request('/api/backend/v1/tasks', { headers: { Authorization: 'Bearer local-dev' } })).status, 401)
  assert.throws(() => orchestratorGateway({ base: 'http://127.0.0.1:8001', devToken: 'real-jwt' }))
})

test('a phone with this run\'s pairing secret gets the dev token from anywhere; others do not', async () => {
  const calls: Request[] = []
  const transport: typeof fetch = async (input, init) => { calls.push(new Request(input, init)); return Response.json([]) }
  const secret = 'abcdefghijklmnopqrstuvwxyz012345'
  const app = new Hono().route('/api/backend', orchestratorGateway({ base: 'http://127.0.0.1:8001', transport, devToken: 'demo-all', pairSecret: secret, peerAddress: () => '127.0.0.1' }))
  const tunnel = { 'CF-Connecting-IP': '203.0.113.9' }
  assert.deepEqual(await (await app.request('/api/backend/dev', { headers: tunnel })).json(), { local: false, pairing: true })
  assert.equal((await app.request('/api/backend/v1/tasks', { headers: { ...tunnel, Authorization: `Bearer pair-${secret}` } })).status, 200)
  assert.equal(calls[0].headers.get('authorization'), 'Bearer demo-all')
  for (const auth of [`Bearer pair-${secret}x`, 'Bearer pair-wrongwrongwrongwrongwrongwrong', 'Bearer local-dev', 'Bearer demo-all'])
    assert.equal((await app.request('/api/backend/v1/tasks', { headers: { ...tunnel, Authorization: auth } })).status, 401, auth)
  assert.equal(calls.length, 1)
  assert.throws(() => orchestratorGateway({ base: 'http://127.0.0.1:8001', devToken: 'demo-all', pairSecret: 'short' }))
})

test('the laptop opens only hardcoded routes, for this machine or a paired phone', async () => {
  const opened: string[] = []
  const secret = 'p'.repeat(32)
  let peer = '127.0.0.1'
  const app = new Hono().route('/api/backend', orchestratorGateway({ base: 'http://127.0.0.1:8001', devToken: 'demo-all', pairSecret: secret,
    peerAddress: () => peer, openOnLaptop: async url => { opened.push(url) } }))
  const open = (auth: string, url: string) => app.request('/api/backend/laptop/open', { method: 'POST',
    headers: { Authorization: auth, 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) })
  assert.equal((await open('Bearer local-dev', 'https://www.youtube.com/')).status, 200)
  assert.equal((await open(`Bearer pair-${secret}`, 'spotify:')).status, 200)
  assert.equal((await open('Bearer local-dev', 'https://evil.example/')).status, 400)
  assert.equal((await open('Bearer pair-wrong', 'spotify:')).status, 401)
  assert.equal((await open('Bearer someone-elses-token', 'spotify:')).status, 401)
  peer = '203.0.113.9'
  assert.equal((await open('Bearer local-dev', 'spotify:')).status, 401)
  assert.deepEqual(opened, ['https://www.youtube.com/', 'spotify:'])
})
