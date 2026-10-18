import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseWake } from '../server/voice'

test('the command keeps numbers and hyphens after the wake phrase', () => {
  assert.deepEqual(parseWake('Hey Focal, compare prices for AirPods Pro 3'), { wake: true, command: 'compare prices for AirPods Pro 3' })
  assert.deepEqual(parseWake('Hey focal, compare prices for AirPods Pro 3.'), { wake: true, command: 'compare prices for AirPods Pro 3.' })
  assert.equal(parseWake('Hey Focal. Price of the Sony WH-1000XM5').command, 'Price of the Sony WH-1000XM5')
  assert.equal(parseWake('hey focal text Legend I will be there at 7').command, 'text Legend I will be there at 7')
  assert.equal(parseWake('Hey, focal. 3 things to do in LA').command, '3 things to do in LA')
  assert.deepEqual(parseWake('compare prices for AirPods Pro 3'), { wake: false, command: 'compare prices for AirPods Pro 3' })
  assert.equal(parseWake('Hey Focal').command, '')
})

test('common mishearings of "Focal" wake it', () => {
  assert.deepEqual(parseWake('hey vocal what is the weather'), { wake: true, command: 'what is the weather' })
  assert.deepEqual(parseWake('Heyfocal, show recommendations'), { wake: true, command: 'show recommendations' })
  assert.deepEqual(parseWake('Heyvocal show recommendations'), { wake: true, command: 'show recommendations' })
  assert.equal(parseWake('Hey-Focal, open my cart').command, 'open my cart')
  assert.equal(parseWake('Hey for cal, compare prices').command, 'compare prices')
  assert.equal(parseWake('hey folk all, what can you do here').command, 'what can you do here')
  assert.equal(parseWake('Hey fokal. Latest news').command, 'Latest news')
  assert.equal(parseWake('Okay focal summarize this').command, 'summarize this')
  assert.equal(parseWake("I'm good, hey focal, compare prices").command, 'compare prices')
})

test('ordinary speech does not wake it', () => {
  for (const said of [
    'focus on the task',
    'a local store',
    'hey there',
    'Hey, how are you?',
    'Focal point of the room',
    'vocal cords',
    'hey for real',
    'hi folks',
    'okay so the local news',
    'hey, call me later',
  ]) assert.equal(parseWake(said).wake, false, said)
})
