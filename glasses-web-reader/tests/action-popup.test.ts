import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ActionPopup, POPUP_DISMISS, popupAllowed, popupLabel } from '../src/lensos/action-popup'
import { mergeSessionActions, STRIP_HEADING } from '../shared/recommendations'
import { apiActionCards, type DecisionIntent } from '../src/lensos/action-cards'
import { glassesRecommendationSpec } from '../src/glasses-recommendations'
import type { DisplayAction, DisplayState } from '../src/lensos/types'
import type { PageSpec } from '../src/screens'

const action = (id: string, label: string, status = 'proposed'): DisplayAction =>
  ({ action_id: id, label, description: `${label} for this product.`, status, requires_approval: true })
const display = (actions: DisplayAction[], revision = 2, pageId = 'page-one', mode = 'actions'): DisplayState => ({
  schema_version: '1.0', session_id: 'session', revision, mode, notice: null,
  page: { page_id: pageId, title: 'Headphones', kind: 'product', summary: [], facts: [] }, actions })
const compare = { ...action('a-compare', 'Compare prices and alternatives'), type: 'compare_prices' }
const reviews = { ...action('a-reviews', 'Check independent reviews'), type: 'find_reviews' }
const cart = { ...action('a-cart', 'Add to Amazon cart'), type: 'add_to_amazon_cart' }
const base: PageSpec = { texts: [{ x: 0, y: 0, w: 576, h: 252, text: 'Page content' }, { x: 0, y: 252, w: 576, h: 36, text: 'Footer' }], images: [] }
const reader = { view: 'reader', loading: false, status: false, recording: false, card: false, page: true }

test('proposed actions never open a second popup; they are options on the one strip', () => {
  const popup = new ActionPopup()
  assert.equal(popup.update(display([compare, reviews, cart])), false)
  assert.equal(popup.view, null)
  assert.deepEqual(popup.labels(), [])
  const options = mergeSessionActions([], display([compare, reviews, cart]).actions, true)
  assert.deepEqual(options.map(o => [o.title, o.session_action_id]),
    [['Compare prices', 'a-compare'], ['Add to Amazon cart', 'a-cart'], ['Review summary', 'a-reviews']])
  const spec = glassesRecommendationSpec(base, options.map(o => o.title), 0)
  const text = spec.texts.map(t => t.text).join('\n')
  assert.match(text, new RegExp(STRIP_HEADING))
  assert.match(text, /> Compare prices/)
  assert.match(text, /Add to Amazon cart/)
  assert.equal((text.match(/Focal can help/g) ?? []).length, 1)
  assert.equal(popupLabel('x'.repeat(80) + '\n'), 'x'.repeat(46))
  assert.ok(new TextEncoder().encode(popupLabel('é'.repeat(80))).length <= 63)
})

test('choosing the cart option opens its review; approval still needs the last review page, confirm and a tap', () => {
  const state = display([compare, reviews, cart], 7)
  const option = mergeSessionActions([], state.actions, true).find(o => o.capability === 'add_to_amazon_cart')!
  const sent: DecisionIntent[] = []
  const cards = apiActionCards(intent => sent.push(intent))
  cards.show(state); cards.setEnabled(true); cards.open()
  cards.focus(option.session_action_id!)
  assert.match(cards.view().title, /Review/)
  assert.ok(cards.view().lines.join(' ').includes('Add to Amazon cart'))
  cards.dispatch('tap') // review -> confirm
  assert.equal(sent.length, 0)
  assert.equal(cards.view().title, 'Confirm action')
  cards.dispatch('tap') // confirm -> approve
  assert.equal(sent.length, 1)
  assert.deepEqual({ ...sent[0], request_id: '' }, { request_id: '', action_id: 'a-cart', expected_revision: 7, operation: 'approve' })
})

test('popups wait while voice, an answer card, a menu, loading or another popup is on screen', () => {
  assert.equal(popupAllowed(reader), true)
  assert.equal(popupAllowed({ ...reader, view: 'deck' }), true)
  for (const busy of [{ view: 'voice' }, { view: 'menu' }, { view: 'actions' }, { view: 'recommendations' },
    { loading: true }, { recording: true }, { card: true }, { status: true }, { page: false }]) {
    assert.equal(popupAllowed({ ...reader, ...busy }), false, JSON.stringify(busy))
  }
  // A result queued while busy waits for the reader.
  const popup = new ActionPopup()
  popup.update(display([compare]))
  popup.expect('a-compare')
  assert.equal(popup.update(display([{ ...compare, status: 'completed' }], 3, 'page-one', 'result')), true)
  assert.deepEqual(popup.labels(), ['View: Compare prices', POPUP_DISMISS])
  // A new page drops it.
  popup.update(display([], 4, 'page-two'))
  assert.equal(popup.view, null)
})

test('the result of an approved action pops up on its page', () => {
  const popup = new ActionPopup()
  popup.update(display([compare]))
  popup.dismiss()
  popup.expect('a-compare')
  assert.equal(popup.update(display([{ ...compare, status: 'completed', result: { message: '$199 at Best Buy' } }], 4, 'page-one', 'result')), true)
  assert.equal(popup.view?.heading, 'Focal finished')
  assert.deepEqual(popup.gesture('tap'), { kind: 'review', action_id: 'a-compare' })
  // Seen running on one poll and failed on the next also pops up.
  popup.update(display([{ ...reviews, status: 'running' }], 5))
  assert.equal(popup.update(display([{ ...reviews, status: 'failed' }], 6, 'page-one', 'error')), true)
  assert.equal(popup.view?.heading, "Focal couldn't finish")
  // An action nobody approved here finishing is not news on this page.
  const other = new ActionPopup()
  other.update(display([compare]))
  assert.equal(other.update(display([{ ...compare, status: 'completed' }], 3, 'page-one', 'result')), false)
  assert.equal(other.view, null)
})

test('result popup cursor stays on its item and Dismiss, and never approves', () => {
  const popup = new ActionPopup()
  popup.update(display([compare]))
  popup.expect('a-compare')
  popup.update(display([{ ...compare, status: 'failed' }], 3, 'page-one', 'error'))
  for (let i = 0; i < 5; i++) popup.gesture('down')
  assert.equal(popup.cursor, 1)
  for (let i = 0; i < 5; i++) popup.gesture('up')
  assert.equal(popup.cursor, 0)
  const steps = ['down', 'up', 'tap'].map(g => popup.gesture(g as 'down').kind)
  assert.deepEqual(steps, ['render', 'render', 'review'])
})
