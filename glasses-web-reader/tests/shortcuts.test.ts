import { test } from 'node:test'
import assert from 'node:assert/strict'
import { shortcutFor } from '../src/shortcuts'

const GOODREADS = 'https://www.goodreads.com/shelf/show/english-novels'

test('popular / interesting book requests open the Goodreads shelf', () => {
  for (const said of [
    'What are the most popular and interesting books right now?',
    'what are most the popular and interesting books right now',
    'Show me some interesting books',
    'top novels this year',
    'What books should I read?',
    'recommend me a good book',
  ]) assert.equal(shortcutFor(said)?.url, GOODREADS, said)
})

test('The Starry Night opens its Wikipedia page', () => {
  for (const said of ['Show me a picture of The Starry Night.', 'starry night by van gogh', 'Show me Starry Night'])
    assert.equal(shortcutFor(said)?.url, 'https://en.wikipedia.org/wiki/The_Starry_Night', said)
})

test('other commands fall through to the agents and search', () => {
  for (const said of [
    'what is the weather in los angeles',
    'Text legend. I\'m running late',
    'compare prices for AirPods Pro 3',
    'book a table for two tonight',
    'most popular restaurants near me',
  ]) assert.equal(shortcutFor(said), null, said)
})
