import { test } from 'node:test'
import assert from 'node:assert/strict'
import { glassesRecommendationSpec, recommendationCursor, RECOMMENDATION_LAYOUTS } from '../src/glasses-recommendations'
import type { PageSpec } from '../src/screens'

const labels = ['Compare prices', 'Find alternatives', 'Read reviews']
const base: PageSpec = { texts: [
  { x: 0, y: 0, w: 576, h: 36, text: 'Current page' },
  { x: 0, y: 40, w: 576, h: 240, text: 'Page content', scroll: true },
], images: [] }

test('glasses panel contains three choices and close in a bounded corner, retaining page context', () => {
  const spec = glassesRecommendationSpec(base, labels, 0, false, 'corner')
  const popup = spec.texts.at(-1)!
  assert.ok(popup.w * popup.h < 576 * 288 * 0.35)
  assert.ok(popup.x + popup.w <= 576 && popup.y + popup.h <= 288)
  assert.match(popup.text, /> Compare prices/)
  assert.match(popup.text, /Find alternatives/)
  assert.match(popup.text, /Read reviews/)
  assert.match(popup.text, /Close recommendations/)
  assert.ok(spec.texts.some(t => t.text === 'Current page'))
  assert.ok(spec.texts.length <= 7)
  assert.ok(spec.texts.every(t => !t.scroll))
  for (const box of spec.texts.slice(0, -1)) {
    assert.ok(box.x + box.w <= popup.x || box.y + box.h <= popup.y || box.x >= popup.x + popup.w)
  }
  assert.equal(base.texts[1].h, 240)
})

test('swiping reaches the close choice without changing the reader position', () => {
  let cursor = 0
  for (let i = 0; i < 6; i++) cursor = recommendationCursor(cursor, 1)
  assert.equal(cursor, 3)
  assert.match(glassesRecommendationSpec(base, labels, cursor).texts.at(-1)!.text, /> Close recommendations/)
  for (let i = 0; i < 6; i++) cursor = recommendationCursor(cursor, -1)
  assert.equal(cursor, 0)
})

test('selection status never claims execution and model line breaks cannot create extra choices', () => {
  const spec = glassesRecommendationSpec(base, ['Compare\nprices', ...labels.slice(1)], 1, true, 'corner')
  const content = spec.texts.at(-1)!.text
  assert.equal(content.split('\n').length, 5)
  assert.match(content, /Selected; not run/)
  assert.match(content, /> Find alternatives/)
})

test('all offered layouts show three actions and close within G2 limits', () => {
  for (const layout of RECOMMENDATION_LAYOUTS) {
    const spec = glassesRecommendationSpec(base, labels, 3, false, layout.id)
    const text = spec.texts.map(box => box.text).join('\n')
    for (const label of labels) assert.ok(text.includes(label), layout.id)
    assert.match(text, /> Close recommendations/)
    assert.ok(spec.texts.length <= 7)
    assert.ok(spec.images.length <= 4)
    assert.ok(spec.texts.every(box => box.x >= 0 && box.y >= 0 && box.x + box.w <= 576 && box.y + box.h <= 288 && !box.scroll))
  }
  const corner = glassesRecommendationSpec(base, labels, 0, false, 'corner')
  const bottom = glassesRecommendationSpec(base, labels, 0, false, 'bottom')
  const full = glassesRecommendationSpec(base, labels, 0, false, 'full')
  assert.notDeepEqual(corner, bottom)
  assert.notDeepEqual(bottom, full)
  assert.ok(bottom.texts.some(box => box.text === 'Current page'))
  assert.ok(!full.texts.some(box => box.text === 'Current page'))
})
