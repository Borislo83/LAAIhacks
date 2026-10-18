import { test } from 'node:test'
import assert from 'node:assert/strict'
import { JSDOM } from 'jsdom'
import { mountBackendPanel } from '../src/lensos/panel'
import type { BackendState } from '../src/lensos/session'

test('connection form clears credentials and preferences preserve unchecked values', () => {
  const dom = new JSDOM('<section id="panel"></section>')
  const root = dom.window.document.querySelector<HTMLElement>('#panel')!
  const connected: unknown[] = [], preferences: unknown[] = []
  const state: BackendState = { connected: false, busy: false, online: false, message: 'Connect', sessionId: '', pageTitle: '',
    preferences: null, tasks: [], history: [], display: null, taskRevision: 0 }
  const panel = mountBackendPanel(root, { state: () => state, connect: (a, b) => connected.push([a, b]),
    disconnect() {}, save: p => preferences.push(p), review() {}, sync() {}, back() {} })
  const input = root.querySelector<HTMLInputElement>('[name=token]')!
  input.value = 'signed-token'
  root.querySelector('[data-connect]')!.dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
  assert.deepEqual(connected, [['signed-token', '']])
  assert.equal(input.value, '')
  state.connected = true; state.online = true
  state.preferences = { display_density: 'comfortable', shopping_priority: 'balanced', proactive_enabled: true, notes: 'private' }
  panel.update()
  const proactive = root.querySelector<HTMLInputElement>('[name=proactive]')!
  proactive.checked = false
  proactive.dispatchEvent(new dom.window.Event('change', { bubbles: true }))
  assert.deepEqual(preferences[0], { display_density: 'comfortable', shopping_priority: 'balanced', proactive_enabled: false })
  assert.equal(root.textContent!.includes('private'), false)
  state.message = '<img src=x onerror=alert(1)>'
  panel.update()
  assert.equal(root.querySelector('img'), null)
  dom.window.close()
})
