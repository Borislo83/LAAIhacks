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

test('common mishearings of "Focal" still wake it', () => {
  for (const said of ['Hey, Foko! Play Blinding Lights on YouTube.', 'Hey, Foco. Open up Blinding Lights on YouTube.', 'Hey Focco, what is playing'])
    assert.equal(parseWake(said).wake, true, said)
  assert.equal(parseWake('Hey, Foko! Play Blinding Lights on YouTube.').command, 'Play Blinding Lights on YouTube.')
})

test('"Hey Focal" wakes it however it was said or spelled', () => {
  for (const [said, command] of [
    ['Hey Fogle, play Starboy', 'play Starboy'],
    ['Hey Phocal. Open YouTube.', 'Open YouTube.'],
    ['Hey Foco! Open Spotify', 'Open Spotify'],
    ['Hey fickle, what is the weather', 'what is the weather'],
    ['Hi focal? Compare prices', 'Compare prices'],
    ['Heya Focal, add AirPods Pro 3 to my Amazon cart', 'add AirPods Pro 3 to my Amazon cart'],
    ['A, focal. Send a message saying hi', 'Send a message saying hi'],
    ['Eh focal, hide the display', 'hide the display'],
    ['He focal, open the AirPods page on Apple', 'open the AirPods page on Apple'],
    ['Focal, open YouTube', 'open YouTube'],
    ['Focal. Pause Spotify.', 'Pause Spotify.'],
  ]) assert.deepEqual(parseWake(said), { wake: true, command }, said)
})

test('sound-alikes in ordinary speech still do not wake it', () => {
  for (const said of ['a local store near me', 'a focus on the task', 'Vocal coaching is fun', 'Local news today',
    'a vocal performance', 'a focal length lens', 'Focal point of the room', 'Fickle weather today', 'focal length of this lens', 'Focus on the road'])
    assert.equal(parseWake(said).wake, false, said)
})

test('a plain "Hey" opening a command wakes it; other "hey"s do not', () => {
  assert.deepEqual(parseWake('Hey, open YouTube.'), { wake: true, command: 'open YouTube.' })
  assert.deepEqual(parseWake('Hey play Starboy on Spotify'), { wake: true, command: 'play Starboy on Spotify' })
  assert.deepEqual(parseWake('Hey, weather in Los Angeles'), { wake: true, command: 'weather in Los Angeles' })
  assert.deepEqual(parseWake('Hey, add AirPods Pro 3 to my Amazon cart'), { wake: true, command: 'add AirPods Pro 3 to my Amazon cart' })
  for (const said of ["Hey, what's up", 'Hey, how are you?', 'Hey man, how are you', 'Hey Sarah, nice to see you', 'I said hey, open the door', 'Hey!', 'hey there buddy'])
    assert.equal(parseWake(said).wake, false, said)
})
