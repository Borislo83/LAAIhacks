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
    <div class="backend-panel">
      <div class="row-head"><h2 class="label" style="margin:0">Orchestrator</h2><span class="label" style="margin:0" data-badge></span></div>
      <p class="msg" data-status role="status"></p>
      <form data-connect>
        <label>access_token<input name="token" type="password" autocomplete="off" required placeholder="Supabase access token" /></label>
        <details><summary>Separate glasses credential</summary><label>g2_token (optional)<input name="g2" type="password" autocomplete="off" /></label>
          <p class="sub">For session approvals, use a G2 token for the same account. Otherwise the account token needs the G2 role.</p></details>
        <p class="sub">Tokens stay in memory. Reconnect after restarting or when a token expires.</p>
        <button class="btn dark console-btn" type="submit">connect</button>
      </form>
      <div class="backend-panel" data-connected hidden>
        <p class="sub" data-session></p>
        <label class="check"><input name="proactive" type="checkbox" /> Suggest useful actions for pages I open</label>
        <div class="backend-buttons">
          <button class="btn console-btn" type="button" data-tasks>suggestions</button>
          <button class="btn console-btn" type="button" data-actions>session actions</button>
          <button class="btn console-btn" type="button" data-history>task history</button>
          <button class="btn console-btn" type="button" data-sync>sync page</button>
          <button class="btn console-btn" type="button" data-back>reader on glasses</button>
          <button class="btn warn console-btn" type="button" data-disconnect>disconnect</button>
        </div>
        <p class="sub">Review a suggestion on the display, then confirm to run it. Use Back to cancel.</p>
      </div>
    </div>`
  const find = <T extends HTMLElement>(selector: string) => root.querySelector<T>(selector)!
  const token = find<HTMLInputElement>('[name=token]'), g2 = find<HTMLInputElement>('[name=g2]')
  const proactive = find<HTMLInputElement>('[name=proactive]')
  find('form[data-connect]').addEventListener('submit', event => {
    event.preventDefault()
    const access = token.value.trim(), glasses = g2.value.trim()
    token.value = ''; g2.value = ''
    if (access) options.connect(access, glasses)
  })
  proactive.addEventListener('change', () => {
    const p = options.state().preferences
    if (p) options.save({ proactive_enabled: proactive.checked, display_density: p.display_density, shopping_priority: p.shopping_priority })
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
    find('[data-badge]').textContent = state.connected ? '● connected' : 'not connected'
    find('[data-badge]').style.color = state.connected ? 'var(--lens)' : ''
    find('[data-connect]').hidden = state.connected
    find<HTMLButtonElement>('[data-connect] button').disabled = state.busy
    find('[data-connected]').hidden = !state.connected
    find('[data-session]').textContent = `session=${state.sessionId}`
    find<HTMLButtonElement>('[data-tasks]').textContent = `suggestions [${state.tasks.length}]`
    find<HTMLButtonElement>('[data-actions]').textContent = `session actions [${state.display?.actions.length ?? 0}]`
    find<HTMLButtonElement>('[data-history]').textContent = `task history [${state.history.length}]`
    find<HTMLButtonElement>('[data-sync]').disabled = state.busy
    const p = state.preferences
    const signature = p ? String(p.proactive_enabled) : ''
    if (p && signature !== preferenceSignature) proactive.checked = p.proactive_enabled
    preferenceSignature = signature
  } }
}
