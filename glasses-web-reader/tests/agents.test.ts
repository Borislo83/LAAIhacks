import { test } from 'node:test'
import assert from 'node:assert/strict'
import { BackendClient } from '../src/lensos/client'
import { AgentCard, CARD_LINES, DRAFT_TAP_GRACE_MS, NO_ANSWER, askAgents, answerFor, paginate, sendDraft, type AgentAnswer } from '../src/lensos/agents'
import type { AgentCall, ChatReply } from '../src/lensos/types'

const call = (kind: string, result: Record<string, unknown> | null, error: string | null = null): AgentCall =>
  ({ kind, payload: {}, result, error, ms: 5 })
const reply = (calls: AgentCall[], extra: Partial<ChatReply> = {}): ChatReply => ({ reply: 'Reply text.', calls, source: 'rules', ...extra })
const news = call('news', { summary: 'Rates held steady as inflation cooled.', headline: 'Fed holds rates',
  sources: [{ publisher: 'Reuters', url: 'https://reuters.com/a' }, { publisher: 'AP', url: 'https://apnews.com/b' }, { publisher: 'Reuters', url: 'https://reuters.com/c' }] })
const draftCall = call('message', { draft_id: 'draft-1', recipient_masked: 'J•••', text: 'Running ten minutes late', summary: 'Draft ready for J•••' })

function harness(respond: (req: Request) => Response | Promise<Response>) {
  const requests: Request[] = []
  const client = new BackendClient('http://localhost/api/backend', 'signed-token', '', async (input, init) => {
    const req = new Request(input, init); requests.push(req.clone()); return respond(req)
  })
  return { client, requests }
}
const lens = (a: AgentAnswer | null) => a!.pages.join('\n')

test('voice command reaches chat when signed in and a news hit becomes lens pages', async () => {
  const { client, requests } = harness(() => Response.json(reply([news])))
  const answer = await askAgents(client, 'what is the latest on the fed')
  assert.equal(requests[0].method, 'POST')
  assert.ok(requests[0].url.endsWith('/api/backend/v1/chat'))
  assert.equal(requests[0].headers.get('authorization'), 'Bearer signed-token')
  assert.deepEqual(await requests[0].json(), { messages: [{ role: 'user', content: 'what is the latest on the fed' }] })
  assert.equal(answer?.kind, 'answer')
  assert.match(lens(answer), /^Fed holds rates\nRates held steady/)
  assert.match(lens(answer), /Sources: Reuters, AP/)
})

test('no agent, signed out, chat errors and failed agents all fall back to web search', async () => {
  assert.equal(await askAgents(harness(() => Response.json(reply([]))).client, 'how tall is everest'), null)
  const signedOut = harness(() => Response.json(reply([news])))
  assert.equal(await askAgents(undefined, 'news'), null)
  assert.equal(signedOut.requests.length, 0)
  assert.equal(await askAgents(harness(() => Response.json({ error: 'Orchestrator unavailable.' }, { status: 502 })).client, 'news'), null)
  assert.equal(await askAgents(harness(() => { throw new TypeError('network') }).client, 'news'), null)
  assert.equal(await askAgents(harness(() => Response.json({ detail: 'odd' })).client, 'news'), null)
  assert.equal(answerFor(reply([call('news', null, 'Agent unavailable')])), null)
})

test('a chat that timed out or failed upstream says so instead of searching the web', async () => {
  for (const status of [500, 503, 504, 408]) {
    const answer = await askAgents(harness(() => Response.json({ detail: 'No answer yet.' }, { status })).client, 'play bad guy')
    assert.equal(lens(answer), `Focal\n${NO_ANSWER}`, String(status))
  }
  // The client's own timer gave up after the request went out.
  const timedOut = await askAgents(harness(() => { throw new DOMException('aborted', 'AbortError') }).client, 'play bad guy')
  assert.equal(lens(timedOut), `Focal\n${NO_ANSWER}`)
  // The caller's own abort (a newer voice command) still throws.
  const controller = new AbortController(); controller.abort()
  const aborting = harness(req => { if (req.signal.aborted) throw new DOMException('aborted', 'AbortError'); return Response.json(reply([news])) })
  await assert.rejects(askAgents(aborting.client, 'news', controller.signal))
})

test('Spotify and laptop errors are the answer, never a web search', () => {
  const failed = answerFor(reply([call('spotify', { summary: "Spotify isn't open on the laptop.", error: "Spotify isn't open on the laptop." }, "Spotify isn't open on the laptop.")]))
  assert.equal(lens(failed), "Spotify\nSpotify isn't open on the laptop.")
  assert.equal(lens(answerFor(reply([call('spotify', null, 'Agent unavailable or returned an invalid response')]))),
    'Spotify\nAgent unavailable or returned an invalid response')
  // safe_laptop's shape: result.laptop = {status, summary}.
  const notOpened = answerFor(reply([call('youtube', { title: 'Bad Guy', url: 'https://www.youtube.com/watch?v=DyDfgMOUjCI', summary: 's',
    laptop: { status: 'failed', summary: "Couldn't open the browser on the laptop." } }, 'Agent error')]))
  assert.match(lens(notOpened), /Bad Guy\nhttps:\/\/www\.youtube\.com\/watch\?v=DyDfgMOUjCI\nNot opened on your laptop: Couldn't open the browser/)
  assert.doesNotMatch(lens(notOpened), /Playing/)
})

test('YouTube, Spotify and shopping replies fit the lens', () => {
  assert.match(lens(answerFor(reply([call('youtube', { title: 'Blinding Lights', url: 'https://www.youtube.com/watch?v=4NRXx6U8ABQ', summary: 's',
    laptop: { status: 'opened', summary: 'Opened the video on the laptop.' } })]))), /Playing Blinding Lights on your laptop/)
  // Old field names the orchestrator never sends don't count as playing.
  assert.doesNotMatch(lens(answerFor(reply([call('youtube', { title: 'Blinding Lights', url: 'https://youtu.be/4NRXx6U8ABQ', summary: 's', opened: true })]))), /Playing/)
  assert.match(lens(answerFor(reply([call('youtube', { title: 'Blinding Lights', url: 'https://youtu.be/4NRXx6U8ABQ', summary: 's' })]))), /youtu\.be\/4NRXx6U8ABQ/)
  assert.equal(lens(answerFor(reply([call('spotify', { summary: 'Playing Levitating on MacBook.' })]))), 'Spotify\nPlaying Levitating on MacBook.')
  const shop = answerFor(reply([call('price', { summary: 'p' }), call('reviews', { summary: 'r' })], { reply: 'Best price $199 at Best Buy.', product: 'AirPods Pro', source: 'coordinator' }))
  assert.equal(lens(shop), 'AirPods Pro\nBest price $199 at Best Buy.')
  // Long answers are paged: a title row plus CARD_LINES rows, each about 48 characters.
  const pages = paginate('News', 'word '.repeat(400))
  assert.ok(pages.length > 1)
  for (const page of pages) {
    const rows = page.split('\n')
    assert.ok(rows.length <= CARD_LINES + 1)
    assert.ok(rows.every(r => r.length <= 60), rows.find(r => r.length > 60))
  }
  assert.ok(paginate('Link', 'https://example.com/' + 'a'.repeat(200)).join('\n').split('\n').every(r => r.length <= 60))
})

test('a message draft waits for a tap on its last page and sends exactly the draft', async () => {
  const answer = answerFor(reply([draftCall]))!
  assert.equal(answer.kind, 'draft')
  assert.match(lens(answer), /Send to J•••\?\n"Running ten minutes late"/)
  let now = 1000
  const card = new AgentCard(answer, true, () => now)
  assert.equal(card.fades, false)
  assert.equal(card.footer(), 'Tap: send   Swipe: cancel')
  // A tap right as the draft appears was meant for the screen before it.
  assert.equal(card.gesture('tap'), 'none')
  assert.equal(card.sending, false)
  now += DRAFT_TAP_GRACE_MS
  assert.equal(card.gesture('tap'), 'send')
  assert.equal(card.gesture('tap'), 'none')
  assert.equal(card.footer(), 'Sending...')
  const { client, requests } = harness(() => Response.json({ status: 'sent', summary: 'Sent to J•••.' }))
  assert.equal(await sendDraft(client, answer.kind === 'draft' ? answer.draft : (null as never)), 'Sent to J•••.')
  assert.ok(requests[0].url.endsWith('/v1/messages/send'))
  assert.deepEqual(await requests[0].json(), { draft_id: 'draft-1', text: 'Running ten minutes late' })
  // A long draft is read page by page before the send tap.
  const long = answerFor(reply([call('message', { ...draftCall.result, text: 'long words '.repeat(80) })]))!
  let later = 0
  const paged = new AgentCard(long, false, () => later)
  later = DRAFT_TAP_GRACE_MS
  assert.ok(long.pages.length > 1)
  assert.equal(paged.gesture('tap'), 'render')
  assert.match(paged.footer(), /^2\/\d+   /)
})

test('swiping a draft cancels without sending', () => {
  for (const gesture of ['up', 'down', 'back'] as const) {
    const card = new AgentCard(answerFor(reply([draftCall]))!, false)
    assert.equal(card.gesture(gesture), 'cancel')
    assert.equal(card.sending, false)
  }
})

test('message without a draft is shown, not searched, and says nothing was sent', () => {
  const asked = answerFor(reply([call('message', { summary: 'Who should I text? I found two Jonathans.' })]))
  assert.equal(asked?.kind, 'answer')
  assert.match(lens(asked), /two Jonathans\.\nNothing was sent\./)
  assert.match(lens(answerFor(reply([call('message', null, 'iMessage agent unavailable')]))), /iMessage agent unavailable/)
})

test('send outcomes are reported honestly', async () => {
  const draft = { draft_id: 'draft-1', recipient: 'J•••', text: 'hi' }
  const outcome = (r: () => Response) => sendDraft(harness(r).client, draft)
  assert.match(await outcome(() => Response.json({ status: 'sent', summary: 'Sent.', dry_run: true })), /^Dry run: nothing was really sent/)
  assert.match(await outcome(() => Response.json({ status: 'needs_permission', summary: 'Allow Automation.' })), /^Not sent: Messages needs permission/)
  assert.match(await outcome(() => Response.json({ status: 'failed', summary: 'No such buddy.' })), /^Not sent\. No such buddy\./)
  assert.match(await outcome(() => Response.json({ detail: 'Messaging executor not configured' }, { status: 503 })), /isn't set up\. Nothing was sent/)
  assert.match(await outcome(() => Response.json({ detail: 'x' }, { status: 502 })), /Check Messages before trying again/)
})

test('answers page with swipes, close on tap, and only hands-free answers fade', () => {
  const answer: AgentAnswer = { kind: 'answer', title: 'News', pages: ['one', 'two'] }
  const card = new AgentCard(answer, true)
  assert.equal(card.fades, true)
  assert.equal(new AgentCard(answer, false).fades, false)
  assert.equal(card.footer(), '1/2   Swipe: more   Tap: close')
  assert.equal(card.gesture('up'), 'none')
  assert.equal(card.gesture('down'), 'render')
  assert.equal(card.text, 'two')
  assert.equal(card.gesture('down'), 'close')
  assert.equal(new AgentCard(answer, false).gesture('tap'), 'close')
})
