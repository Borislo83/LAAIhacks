import { test } from 'node:test'
import assert from 'node:assert/strict'
import { routeFor } from '../shared/routes'

const AIRPODS = 'https://www.apple.com/airpods-pro/'

test('ways of asking for the AirPods page on Apple open it', () => {
  for (const said of [
    'open up the airpods page on apple',
    'Open the AirPods page on Apple.',
    'open airpods pro',
    'take me to AirPods Pro on apple.com',
    "pull up Apple's AirPods page",
    'go to the apple air pods page',
    'show me the AirPods Pro page',
    'can you open the airpods pro page please',
    'navigate to apple airpods',
    'open up the airpods page',
  ]) assert.equal(routeFor(said), AIRPODS, said)
})

test('shopping questions and other pages are not routes', () => {
  for (const said of [
    'compare prices for AirPods Pro 3',
    'show me reviews for AirPods Pro',
    'open the cheapest airpods deal',
    'add airpods pro to my cart',
    'what are AirPods Pro',
    'open the iPhone page on apple',
    'open airpods',
    'play music on my airpods',
  ]) assert.equal(routeFor(said), null, said)
})

test('a route in a later sentence of the same capture still opens', () => {
  assert.equal(routeFor('Play Star Boy on Spotify for me. Open up the AirPods page on Apple.'), AIRPODS)
  assert.equal(routeFor('Open up the AirPods page on Apple. Then compare prices.'), AIRPODS)
  assert.equal(routeFor('Play Star Boy on Spotify. Compare prices for AirPods Pro.'), null)
})
