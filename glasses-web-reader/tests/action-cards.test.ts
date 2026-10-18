import { test } from 'node:test'
import assert from 'node:assert/strict'
import { getTextWidth } from '@evenrealities/pretext'
import { apiActionCards, CARD_ROWS, resultText, type DecisionIntent } from '../src/lensos/action-cards'
import { CARD_W, answerFor, wrap } from '../src/lensos/agents'
import { drawable, guardGlyphs, lensText } from '../src/lensos/glyphs'
import type { DisplayAction, DisplayState } from '../src/lensos/types'

// The compare_prices result as the watcher posts it (services/agents integration.result_message).
const PRICE_RESULT = [
  '8 offers, 3 verified: best $199.00 at Amazon (verified). Verify the listing before buying.',
  'Source: https://www.apple.com/shop/buy-airpods/airpods-pro-3',
  'Source: https://www.amazon.com/clp/B0FQFB8FMG',
  'Source: https://www.apple.com/airpods-pro/',
  'Retrieved: 2026-10-18T08:28:58.975912+00:00',
].join('\n')
const REVIEW_RESULT = 'Reviewers rate AirPods Pro 3 5.0/5 on average (pcmag.com 5). The AirPods Pro 3 are widely praised for their leading noise cancellation, superb sound quality, and useful features like Live Translation and a heart rate monitor. They are considered the best choice for Apple users, though some reviewers note the fit is different from the AirPods Pro 2 and the case lost its lanyard loop. Battery life is up to 8 hours with noise cancellation on.\nSource: https://www.pcmag.com/reviews/apple-airpods-pro-3\nSource: https://www.rtings.com/headphones/reviews/apple/airpods-pro-3\nSource: https://www.walmart.com/ip/123\nRetrieved: 2026-10-18T08:30:00Z'

const compare: DisplayAction = { action_id: 'a-compare', type: 'compare_prices', label: 'Compare prices and alternatives',
  description: 'Research AirPods Pro 3', status: 'proposed', requires_approval: true }
const reviews: DisplayAction = { action_id: 'a-reviews', type: 'find_reviews', label: 'Check independent reviews',
  description: 'Research AirPods Pro 3', status: 'proposed', requires_approval: true }
const cart: DisplayAction = { action_id: 'a-cart', type: 'add_to_amazon_cart', label: 'Add to Amazon cart',
  description: 'Add to your Amazon cart: Apple AirPods Pro 3 Wireless Earbuds with Active Noise Cancellation (ASIN B0FQFB8FMG), $199.00, new, sold by Amazon.com Amazon.com. Quantity 1. Checkout is not started.',
  status: 'proposed', requires_approval: true }
const state = (actions: DisplayAction[], revision = 2, mode = 'summary'): DisplayState => ({
  schema_version: '1.0', session_id: 'session', revision, mode, notice: null,
  page: { page_id: 'page', title: 'AirPods Pro 3', kind: 'product', summary: [], facts: [] }, actions })

const fits = (line: string) => getTextWidth(line) <= CARD_W
const words = (text: string) => text.split(/\s+/).filter(Boolean)

test('a result message shows compact sources and no timestamp', () => {
  assert.equal(resultText(PRICE_RESULT),
    '8 offers, 3 verified: best $199.00 at Amazon (verified). Verify the listing before buying.\nSources: apple.com, amazon.com')
  assert.equal(resultText('Nothing found.\nRetrieved: now'), 'Nothing found.')
  // A named source that isn't a URL is kept as written.
  assert.equal(resultText('Fine.\nSource: Wirecutter'), 'Fine.\nSources: Wirecutter')
})

test('the Done card wraps whole words at pixel widths and pages long results with swipes', () => {
  const sent: DecisionIntent[] = []
  const cards = apiActionCards(i => sent.push(i))
  cards.setEnabled(true)
  cards.show(state([{ ...reviews, status: 'completed', result: { message: REVIEW_RESULT } }], 5, 'result'))
  const first = cards.view()
  assert.equal(first.title, 'Done: Review summary')
  assert.match(first.hint, /^1\/2 {2}Swipe: page \| Tap: all actions \| Double: close$/)
  cards.dispatch('down')
  const second = cards.view()
  assert.match(second.hint, /^2\/2 /)
  cards.dispatch('down') // stays on the last page
  assert.deepEqual(cards.view(), second)
  const shown = [...first.lines, ...second.lines]
  assert.ok(first.lines.length <= CARD_ROWS && shown.every(fits))
  // Every word of the summary is on the lens whole, in order; no URL or timestamp.
  const summary = REVIEW_RESULT.split('\n')[0]
  assert.deepEqual(words(shown.join(' ')).slice(0, words(summary).length), words(summary))
  assert.equal(shown.at(-1), 'Sources: pcmag.com, rtings.com, walmart.com')
  assert.doesNotMatch(shown.join('\n'), /https?:|Retrieved|\d{4}-\d\d-\d\dT/)
  cards.dispatch('up')
  assert.deepEqual(cards.view(), first)
  assert.equal(cards.dispatch('back'), 'exit')
  assert.equal(sent.length, 0)
})

test('the compare_prices result card is one page of text with its sources', () => {
  const cards = apiActionCards(() => {})
  cards.setEnabled(true)
  cards.show(state([{ ...compare, status: 'completed', result: { message: PRICE_RESULT } }], 4, 'result'))
  const view = cards.view()
  assert.equal(view.title, 'Done: Compare prices')
  assert.deepEqual(view.lines, [...wrap('8 offers, 3 verified: best $199.00 at Amazon (verified). Verify the listing before buying.', CARD_W),
    'Sources: apple.com, amazon.com'])
  assert.equal(view.hint, 'Tap: all actions | Double: close')
})

test('a review opened from the strip closes back to the page; from the list it goes back to the list', () => {
  const cards = apiActionCards(() => {})
  cards.show(state([compare, reviews, cart])); cards.setEnabled(true)
  cards.open(); cards.focus('a-cart')
  const view = cards.view()
  assert.equal(view.title, 'Review action')
  assert.equal(view.hint, 'Tap: approve or reject | Double: close')
  // The cart's exact listing is reviewed in whole words, then its status.
  assert.ok(view.lines.every(fits))
  assert.deepEqual(view.lines, ['Add to Amazon cart', ...wrap(cart.description, CARD_W), 'Status: needs approval'])
  assert.deepEqual(words(view.lines.slice(1, -1).join(' ')), words(cart.description))
  assert.equal(cards.dispatch('back'), 'exit')

  cards.open() // the list
  assert.equal(cards.view().lines[0], '> Compare prices (needs approval)')
  cards.dispatch('tap')
  assert.equal(cards.view().title, 'Review action')
  assert.equal(cards.view().hint, 'Tap: approve or reject | Double: back')
  assert.equal(cards.dispatch('back'), undefined)
  assert.equal(cards.view().title, 'Actions')
})

test('a confirmation never survives a new revision; the review follows its action', () => {
  const sent: DecisionIntent[] = []
  const cards = apiActionCards(i => sent.push(i))
  cards.show(state([compare, reviews], 2)); cards.setEnabled(true)
  cards.open(); cards.focus('a-reviews'); cards.dispatch('tap')
  assert.equal(cards.view().title, 'Confirm action')
  // Another action finished: the list order changed and the revision moved on.
  cards.show(state([reviews, { ...compare, status: 'completed' }], 3))
  assert.equal(cards.view().title, 'Review action')
  assert.equal(cards.view().lines[0], 'Review summary')
  cards.dispatch('tap'); cards.dispatch('tap')
  assert.equal(sent.length, 1)
  assert.deepEqual({ ...sent[0], request_id: '' }, { request_id: '', action_id: 'a-reviews', expected_revision: 3, operation: 'approve' })
})

test('every card fits the lens: one title, at most 7 rows, a hint, all within 568px and drawable', () => {
  const cards = apiActionCards(() => {})
  cards.setEnabled(true)
  const many = Array.from({ length: 9 }, (_, i) => ({ ...compare, action_id: `a${i}`, label: `Action number ${i} with a long label that runs on and on` , type: undefined }))
  const views = []
  cards.show(state(many)); cards.open(); views.push(cards.view())
  for (let i = 0; i < 8; i++) cards.dispatch('down')
  views.push(cards.view())
  assert.equal(views[1].title, 'Actions 2/2')
  cards.dispatch('tap'); views.push(cards.view())
  cards.show(state([{ ...compare, status: 'running' }], 3, 'loading')); views.push(cards.view())
  cards.show(state([{ ...compare, status: 'failed', result: { message: 'Search failed ◉ ⏸ try later' } }], 4, 'error')); views.push(cards.view())
  for (const v of views) {
    assert.ok(v.lines.length <= CARD_ROWS)
    for (const line of [v.title, v.hint, ...v.lines]) {
      assert.ok(fits(line), line)
      assert.ok(Array.from(line).every(drawable), line)
    }
  }
  assert.equal(views[3].title, 'Working: Compare prices')
  assert.equal(views[4].title, 'Failed: Compare prices')
})

test('symbols the G2 font lacks are mapped or dropped before they reach the lens', () => {
  assert.equal(lensText('● Search'), '● Search')
  assert.equal(lensText('◉ Search'), '● Search')
  assert.equal(lensText('Agent ▸'), 'Agent ▶')
  assert.equal(lensText('▶️ Playing Diana'), '▶ Playing Diana')
  assert.equal(lensText('⏸ Paused Spotify.'), 'Paused Spotify.')
  assert.equal(lensText('🔊 Volume set to 50%.'), 'Volume set to 50%.')
  assert.equal(lensText('• 4.5★ · $199 — “good”'), '• 4.5★ · $199 — “good”')
  // A Spotify reply on an answer card has nothing the font can't draw.
  const answer = answerFor({ reply: '', source: 'chat', calls: [{ kind: 'spotify', payload: {}, result: { summary: '⏸ Paused: Diana by One Direction' }, error: null, ms: 1 }] })!
  assert.ok(answer.pages.every(p => Array.from(p).every(drawable)), answer.pages.join('|'))
  assert.match(answer.pages[0], /^Spotify\nPaused: Diana by One Direction$/)
})

test('every container text and list label sent through the bridge is made lens-safe', async () => {
  const calls: unknown[] = []
  const raw = {
    createStartUpPageContainer: async (p: unknown) => { calls.push(p); return 0 },
    rebuildPageContainer: async (p: unknown) => { calls.push(p); return true },
    textContainerUpgrade: async (u: unknown) => { calls.push(u); return true },
    other: 1,
  }
  const bridge = guardGlyphs(raw)
  await bridge.rebuildPageContainer({ textObject: [{ content: '◉ Web Reader' }], listObject: [{ itemContainer: { itemName: ['Agent ▸', '🔊'] } }] } as never)
  await bridge.textContainerUpgrade({ containerID: 2, content: '⏭ Next' } as never)
  await bridge.createStartUpPageContainer({ textObject: [{ content: 'Plain' }] } as never)
  assert.deepEqual(calls, [
    { textObject: [{ content: '● Web Reader' }], listObject: [{ itemContainer: { itemName: ['Agent ▶', ' '] } }] },
    { containerID: 2, content: 'Next' },
    { textObject: [{ content: 'Plain' }] },
  ])
})

test('a very long word (a URL) wraps quickly and every piece fits', () => {
  const url = `https://example.com/${'x'.repeat(3000)}`
  const start = performance.now()
  const rows = wrap(`see ${url} here`)
  assert.ok(performance.now() - start < 500, 'wrap took too long')
  assert.ok(rows.every(fits))
  assert.equal(rows.join('').replace(/\s/g, ''), `see${url}here`)
})

test('after approving a second action the card reports it, not the earlier result', () => {
  const sent: DecisionIntent[] = []
  const cards = apiActionCards(i => sent.push(i))
  const done = { ...compare, status: 'completed', result: { message: PRICE_RESULT } }
  cards.show(state([done, reviews], 4, 'result')); cards.setEnabled(true)
  cards.open(); cards.focus('a-reviews'); cards.dispatch('tap'); cards.dispatch('tap')
  cards.finish(sent[0].request_id, 'success', 'Approved; waiting for worker.')
  // The session still says "result" (for compare) while reviews is only approved.
  cards.show(state([done, { ...reviews, status: 'approved' }], 5, 'result'))
  assert.equal(cards.view().title, 'Working: Review summary')
  cards.show(state([done, { ...reviews, status: 'completed', result: { message: REVIEW_RESULT } }], 6, 'result'))
  assert.equal(cards.view().title, 'Done: Review summary')
  assert.match(cards.view().lines[0], /^Reviewers rate AirPods Pro 3/)
})

test("a result popup's View opens the Done card, and closing it returns to the page", () => {
  const cards = apiActionCards(() => {})
  const done = { ...reviews, status: 'completed', result: { message: REVIEW_RESULT } }
  // The session's latest outcome is another action: View still shows the one it named.
  cards.show(state([done, { ...compare, status: 'failed', result: { message: 'No offers.' } }], 7, 'error')); cards.setEnabled(true)
  cards.open(); cards.focus('a-reviews')
  assert.equal(cards.view().title, 'Done: Review summary')
  assert.match(cards.view().hint, /^1\/2 {2}Swipe: page/)
  assert.equal(cards.dispatch('back'), 'exit')
})
