import { test } from 'node:test'
import assert from 'node:assert/strict'
import { listScroll, nativeListSpec } from '../src/native-list'

// Highlight tops (px) read off the simulator's 10-item menu.
const labels = ['Resume', 'Focal can help (2)', 'Voice search', 'Contents (3)', 'Links on page (40)',
  'Bookmark this page', 'Recent pages (1)', 'Hide display', 'Settings', 'Exit']

function walk(path: number[]) {
  let scroll = 0
  return path.map(i => {
    scroll = listScroll(scroll, i, labels.length)
    return 40 + i * 40 - scroll
  })
}

test('swiping down keeps the next item in view, then sits flush with the bottom', () => {
  assert.deepEqual(walk([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]), [40, 80, 120, 160, 200, 200, 200, 200, 204, 244])
})

test('swiping back up keeps the previous item in view', () => {
  assert.deepEqual(walk([9, 8, 7, 6, 5, 4, 3, 2, 3, 4, 5, 6]).slice(1), [204, 164, 124, 84, 80, 80, 80, 120, 160, 200, 200])
})

test('a short list never scrolls', () => {
  assert.equal(listScroll(0, 4, 5), 0)
})

test('only the selected item has a visible border; the rest are dimmed and fully on screen', () => {
  const spec = nativeListSpec('Menu', labels, 9, 156)
  const items = spec.texts.slice(1)
  assert.deepEqual(items.map(t => t.text.trim()), labels.slice(4))
  const lit = items.filter(t => t.color !== 0)
  assert.equal(lit.length, 1)
  assert.deepEqual([lit[0].y, lit[0].h, lit[0].textColor], [244, 40, undefined])
  assert.ok(items.slice(0, -1).every(t => t.textColor === 0))
  assert.ok(items.every(t => t.y >= 36 && t.y + t.h <= 288))
  // Header, six rows and the capture layer: the firmware's 8-container cap.
  assert.ok(spec.texts.length <= 7)
})
