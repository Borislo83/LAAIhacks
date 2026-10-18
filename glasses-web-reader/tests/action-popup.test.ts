import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ActionPopup, POPUP_DISMISS, POPUP_HEADING, popupAllowed, popupLabel } from '../src/lensos/action-popup'
import { apiActionCards, type DecisionIntent } from '../src/lensos/action-cards'
import { glassesRecommendationSpec } from '../src/glasses-recommendations'
import type { DisplayAction, DisplayState } from '../src/lensos/types'
import type { PageSpec } from '../src/screens'

const action = (id: string, label: string, status = 'proposed'): DisplayAction =>
  ({ action_id: id, label, description: `${label} for this product.`, status, requires_approval: true })
const display = (actions: DisplayAction[], revision = 2, pageId = 'page-one', mode = 'actions'): DisplayState => ({
  schema_version: '1.0', session_id: 'session', revision, mode, notice: null,
  page: { page_id: pageId, title: 'Headphones', kind: 'product', summary: [], facts: [] }, actions })
const compare = action('a-compare', 'Compare prices'), reviews = action('a-reviews', 'Find reviews')
const base: PageSpec = { texts: [{ x: 0, y: 0, w: 576, h: 252, text: 'Page content' }, { x: 0, y: 252, w: 576, h: 36, text: 'Footer' }], images: [] }
const reader = { view: 'reader', loading: false, status: false, recording: false, card: false, page: true }

test('new proposed actions pop up once, with a Dismiss item and clipped labels on the bottom strip', () => {
  const popup = new ActionPopup()
  assert.equal(popup.update(display([compare, reviews])), true)
  assert.equal(popup.view?.heading, POPUP_HEADING)
  assert.deepEqual(popup.labels(), ['Compare prices', 'Find reviews', POPUP_DISMISS])
  // The same set on the next poll (or a new revision of it) does not pop up again.
  popup.gesture('back')
  assert.equal(popup.update(display([compare, reviews], 3)), false)
  assert.equal(popup.view, null)
  assert.equal(popupLabel('x'.repeat(80) + '\n'), 'x'.repeat(46))
  assert.ok(new TextEncoder().encode(popupLabel('é'.repeat(80))).length <= 63)
  const spec = glassesRecommendationSpec(base, ['Compare prices', 'Find reviews'], 0, false, 'bottom', { heading: POPUP_HEADING, close: POPUP_DISMISS })
  const text = spec.texts.map(t => t.text).join('\n')
  assert.match(text, /Focal can help/)
  assert.match(text, /> Compare prices/)
  assert.match(text, /Dismiss/)
  assert.ok(spec.texts.length <= 7 && spec.texts.every(b => b.x + b.w <= 576 && b.y + b.h <= 288 && !b.scroll))
})

test('a newly proposed action pops up again, but not after Dismiss on that page; a new page starts over', () => {
  const popup = new ActionPopup()
  popup.update(display([compare]))
  popup.view = null
  assert.equal(popup.update(display([compare, reviews], 3)), true)
  popup.gesture('down'); popup.gesture('down')
  assert.deepEqual(popup.gesture('tap'), { kind: 'close' }) // Dismiss
  assert.equal(popup.update(display([compare, reviews, action('a-news', 'Summarize news')], 4)), false)
  assert.equal(popup.update(display([compare], 5, 'page-two')), true)
})

test('tapping an action opens review; approval still needs the last review page, confirm and a tap', () => {
  const popup = new ActionPopup()
  const state = display([compare, reviews], 7)
  popup.update(state)
  popup.gesture('down')
  const step = popup.gesture('tap')
  assert.deepEqual(step, { kind: 'review', action_id: 'a-reviews' })
  assert.equal(popup.view, null)
  const sent: DecisionIntent[] = []
  const cards = apiActionCards(intent => sent.push(intent))
  cards.show(state); cards.setEnabled(true); cards.open()
  cards.focus('a-reviews')
  assert.match(cards.view().title, /Review/)
  assert.ok(cards.view().lines.join(' ').includes('Find reviews'))
  cards.dispatch('tap') // review -> confirm
  assert.equal(sent.length, 0)
  assert.equal(cards.view().title, 'Confirm action')
  cards.dispatch('tap') // confirm -> approve
  assert.equal(sent.length, 1)
  assert.deepEqual({ ...sent[0], request_id: '' }, { request_id: '', action_id: 'a-reviews', expected_revision: 7, operation: 'approve' })
})

test('popups wait while voice, an answer card, a menu, loading or another popup is on screen', () => {
  assert.equal(popupAllowed(reader), true)
  assert.equal(popupAllowed({ ...reader, view: 'deck' }), true)
  for (const busy of [{ view: 'voice' }, { view: 'menu' }, { view: 'actions' }, { view: 'recommendations' },
    { loading: true }, { recording: true }, { card: true }, { status: true }, { page: false }]) {
    assert.equal(popupAllowed({ ...reader, ...busy }), false, JSON.stringify(busy))
  }
  // Queued while busy: the popup keeps its list until the reader is back, minus anything no longer proposed.
  const popup = new ActionPopup()
  popup.update(display([compare, reviews]))
  popup.update(display([compare, { ...reviews, status: 'invalidated' }], 3))
  assert.deepEqual(popup.labels(), ['Compare prices', POPUP_DISMISS])
  popup.update(display([{ ...compare, status: 'invalidated' }], 4))
  assert.equal(popup.view, null)
})

test('the result of an approved action pops up on its page; the toggle turns popups off', () => {
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
  const off = new ActionPopup()
  off.setEnabled(false)
  assert.equal(off.update(display([compare])), false)
  assert.equal(off.view, null)
})

test('popup cursor stays on its items and Dismiss, and never approves', () => {
  const popup = new ActionPopup()
  popup.update(display([compare]))
  for (let i = 0; i < 5; i++) popup.gesture('down')
  assert.equal(popup.cursor, 1)
  for (let i = 0; i < 5; i++) popup.gesture('up')
  assert.equal(popup.cursor, 0)
  const steps = ['down', 'up', 'tap'].map(g => popup.gesture(g as 'down').kind)
  assert.deepEqual(steps, ['render', 'render', 'review'])
})
