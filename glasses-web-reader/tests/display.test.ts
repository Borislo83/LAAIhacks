import { test } from 'node:test'
import assert from 'node:assert/strict'
import { displayCommand } from '../shared/display'

test('hide and show the display by voice', () => {
  for (const said of ['hide the display', 'Hide display.', 'please turn off the screen', 'Hey Focal, hide the lens', 'go dark', 'display off', 'clear the screen'])
    assert.equal(displayCommand(said), 'hide', said)
  for (const said of ['show the display', 'Unhide.', 'turn on the screen', 'show the display again', 'wake up', 'display on'])
    assert.equal(displayCommand(said), 'show', said)
  for (const said of ['show me the airpods page', 'hide my messages from Alex', 'compare prices for a 4k display', 'open the AirPods page on Apple', 'show recommendations'])
    assert.equal(displayCommand(said), null, said)
})
