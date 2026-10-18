import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseWake } from '../server/voice'

test('the command keeps numbers and hyphens after the wake phrase', () => {
  assert.deepEqual(parseWake('Hey search, compare prices for AirPods Pro 3.'), { wake: true, command: 'compare prices for AirPods Pro 3.' })
  assert.equal(parseWake('Hey Search. Price of the Sony WH-1000XM5').command, 'Price of the Sony WH-1000XM5')
  assert.equal(parseWake('hey search text Legend I will be there at 7').command, 'text Legend I will be there at 7')
  assert.equal(parseWake('Hey, search. 3 things to do in LA').command, '3 things to do in LA')
  assert.deepEqual(parseWake('compare prices for AirPods Pro 3'), { wake: false, command: 'compare prices for AirPods Pro 3' })
  assert.equal(parseWake('Hey search').command, '')
})
