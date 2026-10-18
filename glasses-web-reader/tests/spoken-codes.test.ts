import { test } from 'node:test'
import assert from 'node:assert/strict'
import { normalizeSpokenCodes as n } from '../server/voice'

test('spelled-out model numbers become product codes', () => {
  assert.equal(n('compare prices for sony w h one thousand x m five.'), 'compare prices for sony WH 1000 XM5.')
  assert.equal(n('price of the p s five'), 'price of the PS5')
  assert.equal(n('compare prices for Sony WH-1000XM5'), 'compare prices for Sony WH-1000XM5')
})

test('ordinary speech is left alone', () => {
  for (const s of ['text Legend one sec', 'I am a fan', 'play twenty one pilots on spotify', "what's the latest on SpaceX", 'text Legend I will be there at five']) {
    assert.equal(n(s), s)
  }
})
