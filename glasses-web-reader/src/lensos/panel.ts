import type { ActionGroup, BackendState } from './session'
import type { Preferences } from './types'

interface Options {
  state(): BackendState
  connect(token: string, g2Token: string): void
  disconnect(): void
  save(p: Omit<Preferences, 'notes'>): void
  review(group: ActionGroup): void
  sync(): void
  back(): void
}

export function mountBackendPanel(root: HTMLElement, options: Options) {
  root.innerHTML = `
    <label class="lbl">Account & recommendations</label>
    <div class="card backend-panel">
      <p class="msg" data-status role="status"></p>
      <form data-connect>
        <label>Account access token<input name="token" type="password" autocomplete="off" required placeholder="Supabase access token" /></label>
        <details><summary>Separate glasses credential</summary><label>G2 access token (optional)<input name="g2" type="password" autocomplete="off" /></label>
          <p class="sub">For session approvals, use a G2 token for the same account. Otherwise the account token needs the G2 role.</p></details>
        <p class="sub">Tokens stay in memory. Reconnect after restarting or when a token expires.</p>
        <button class="go" type="submit">Connect account</button>
      </form>
      <div data-connected hidden>
        <p class="sub" data-session></p>
        <form data-preferences>
          <label class="check"><input name="proactive" type="checkbox" /> Suggest useful actions for pages I open</label>
          <label>Shopping priority<select name="priority"><option value="balanced">Balanced</option><option value="price">Price</option><option value="quality">Quality</option></select></label>
          <label>Panel density<select name="density"><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></label>
          <button type="submit">Save preferences</button>
        </form>
        <div class="backend-buttons">
          <button type="button" data-tasks>Recommendations</button>
          <button type="button" data-actions>Session actions</button>
          <button type="button" data-history>Saved history</button>
          <button type="button" data-sync>Sync current page</button>
          <button type="button" data-back>Back to reader</button>
          <button type="button" data-disconnect>Disconnect</button>
        </div>
        <p class="sub">Review a suggestion on the display, then confirm to run it. Use Back to cancel.</p>
      </div>
    </div>`
  const find = <T extends HTMLElement>(selector: string) => root.querySelector<T>(selector)!
  const token = find<HTMLInputElement>('[name=token]'), g2 = find<HTMLInputElement>('[name=g2]')
  const proactive = find<HTMLInputElement>('[name=proactive]')
  const density = find<HTMLSelectElement>('[name=density]'), priority = find<HTMLSelectElement>('[name=priority]')
  find('form[data-connect]').addEventListener('submit', event => {
    event.preventDefault()
    const access = token.value.trim(), glasses = g2.value.trim()
    token.value = ''; g2.value = ''
    if (access) options.connect(access, glasses)
  })
  find('form[data-preferences]').addEventListener('submit', event => {
    event.preventDefault()
    options.save({ proactive_enabled: proactive.checked, display_density: density.value as Preferences['display_density'],
      shopping_priority: priority.value as Preferences['shopping_priority'] })
  })
  find('[data-tasks]').addEventListener('click', () => options.review('tasks'))
  find('[data-actions]').addEventListener('click', () => options.review('session'))
  find('[data-history]').addEventListener('click', () => options.review('history'))
  find('[data-sync]').addEventListener('click', options.sync)
  find('[data-disconnect]').addEventListener('click', options.disconnect)
  find('[data-back]').addEventListener('click', options.back)
  let preferenceSignature = ''
  return { update() {
    const state = options.state()
    find('[data-status]').textContent = state.message
    find('[data-connect]').hidden = state.connected
    find<HTMLButtonElement>('[data-connect] button').disabled = state.busy
    find('[data-connected]').hidden = !state.connected
    find('[data-session]').textContent = `Session: ${state.sessionId}`
    find<HTMLButtonElement>('[data-tasks]').textContent = `Recommendations (${state.tasks.length})`
    find<HTMLButtonElement>('[data-actions]').textContent = `Session actions (${state.display?.actions.length ?? 0})`
    find<HTMLButtonElement>('[data-history]').textContent = `Saved history (${state.history.length})`
    find<HTMLButtonElement>('[data-sync]').disabled = state.busy
    const p = state.preferences
    const signature = p ? JSON.stringify([p.display_density, p.shopping_priority, p.proactive_enabled]) : ''
    if (p && signature !== preferenceSignature) {
      proactive.checked = p.proactive_enabled; density.value = p.display_density; priority.value = p.shopping_priority
      root.classList.toggle('compact', p.display_density === 'compact')
    }
    preferenceSignature = signature
  } }
}
