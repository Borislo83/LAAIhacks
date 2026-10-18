import { test } from 'node:test'
import assert from 'node:assert/strict'
import { LAPTOP_URLS, matchRoute, routeFor } from '../shared/routes'

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

test('a misheard greeting before the request still routes', () => {
  for (const said of ['Hey, Foko! Open up the AirPods page on Apple.', 'Hey Fogle, take me to the Apple AirPods page', 'hey, focal. open airpods pro'])
    assert.equal(routeFor(said), AIRPODS, said)
})

test('"open YouTube" and "open Spotify" open them on the laptop only', () => {
  for (const said of ['open youtube', 'Open up YouTube.', 'hey focal, open youtube on my laptop', 'launch the YouTube app'])
    assert.deepEqual([matchRoute(said)?.url, matchRoute(said)?.on], ['https://www.youtube.com/', 'laptop'], said)
  for (const said of ['open spotify', 'Open up the Spotify app', 'can you open spotify please'])
    assert.deepEqual([matchRoute(said)?.url, matchRoute(said)?.on], ['spotify:', 'laptop'], said)
  // A video or song on them is for the agents, which play it on the laptop.
  for (const said of ['open up Starboy on YouTube', 'open Blinding Lights on spotify', 'what is youtube'])
    assert.equal(matchRoute(said), null, said)
  assert.equal(matchRoute('open the airpods page on apple')?.on, 'both')
})

test('only the hardcoded routes may be opened on the laptop', () => {
  assert.deepEqual([...LAPTOP_URLS].sort(), ['https://www.apple.com/airpods-pro/', 'https://www.youtube.com/', 'spotify:'])
})
