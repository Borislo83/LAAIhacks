import { RECOMMENDATION_SELECTED, validBatch, type RecommendationBatch,
  type RecommendationContext, type RecommendationSelection } from '../shared/recommendations'

export interface RecommendationEvents {
  ready?(batch: RecommendationBatch, context: RecommendationContext): void
  selected?(selection: RecommendationSelection): void
  closed?(): void
  opened?(): void
  reset?(): void
}

export function mountRecommendations(root: HTMLElement,
  request: (context: RecommendationContext, signal: AbortSignal) => Promise<RecommendationBatch>,
  events: RecommendationEvents = {}) {
  const document = root.ownerDocument
  const window = document.defaultView!
  root.classList.add('recommendations')
  root.hidden = true
  root.setAttribute('role', 'region')
  root.setAttribute('aria-label', 'Recommended actions')
  root.innerHTML = `
    <div class="rec-header">
      <div><span class="rec-eyebrow">Suggested by Claude</span><h2>A useful next step</h2></div>
      <button class="rec-close" type="button" data-close aria-label="Close recommendations" title="Close recommendations">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
      </button>
    </div>
    <p class="rec-page" data-page></p>
    <div class="rec-actions" data-actions></div>
    <p class="rec-status" role="status" aria-live="polite"></p>
    <button class="rec-retry" type="button" data-retry hidden>Try again</button>`
  const find = <T extends HTMLElement>(selector: string) => root.querySelector<T>(selector)!
  const list = find('[data-actions]'), status = find('[role=status]')
  const retry = find<HTMLButtonElement>('[data-retry]')
  let controller: AbortController | undefined
  let generation = 0
  let context: RecommendationContext | undefined
  let batch: RecommendationBatch | undefined

  function reset() {
    generation++
    controller?.abort()
    controller = undefined
    batch = undefined
    context = undefined
    root.hidden = true
    root.removeAttribute('aria-busy')
    list.replaceChildren()
    window.removeEventListener('keydown', escape)
    events.reset?.()
  }
  function close() {
    const hadFocus = root.contains(document.activeElement)
    if (batch) {
      root.hidden = true
      window.removeEventListener('keydown', escape)
    } else reset()
    events.closed?.()
    if (hadFocus) document.querySelector<HTMLElement>('#url')?.focus({ preventScroll: true })
  }
  function reopen() {
    if (!batch || !context) return false
    root.hidden = false
    window.addEventListener('keydown', escape)
    events.opened?.()
    return true
  }
  function escape(event: KeyboardEvent) {
    if (event.key === 'Escape' && !root.hidden) { event.preventDefault(); close() }
  }
  find('[data-close]').addEventListener('click', close)
  retry.addEventListener('click', () => { if (context) void show(context) })

  function select(index: number) {
    const action = batch?.actions[index]
    if (!context || !action || root.hidden) return
    for (const child of list.querySelectorAll<HTMLButtonElement>('button')) {
      child.setAttribute('aria-pressed', String(child.dataset.action === action.id))
    }
    status.textContent = 'Running on your glasses…'
    const detail: RecommendationSelection = { schema_version: '1.0', request_id: window.crypto.randomUUID(),
      executor: 'fetch.ai', source: 'claude', action: { ...action }, context: { ...context } }
    events.selected?.(detail)
    root.dispatchEvent(new window.CustomEvent(RECOMMENDATION_SELECTED, { bubbles: true, detail }))
  }

  async function show(input: RecommendationContext) {
    reset()
    const current = generation
    context = { ...input }
    controller = new AbortController()
    root.hidden = false
    root.setAttribute('aria-busy', 'true')
    find('[data-page]').textContent = input.title
    status.textContent = 'Finding three useful actions…'
    retry.hidden = true
    window.addEventListener('keydown', escape)
    try {
      const result = await request(input, controller.signal)
      if (current !== generation) return
      if (!validBatch(result) || result.page.url !== input.url) throw new Error('Invalid recommendations')
      batch = result
      status.textContent = 'Choose an action to get started.'
      for (const [index, action] of result.actions.entries()) {
        const button = document.createElement('button')
        button.type = 'button'
        button.className = 'rec-action'
        button.dataset.action = action.id
        button.setAttribute('aria-pressed', 'false')
        const number = document.createElement('span')
        number.className = 'rec-number'
        number.textContent = String(index + 1).padStart(2, '0')
        number.setAttribute('aria-hidden', 'true')
        const copy = document.createElement('span')
        const title = document.createElement('strong'), description = document.createElement('span')
        title.textContent = action.title
        description.textContent = action.description
        copy.append(title, description)
        button.append(number, copy)
        button.addEventListener('click', () => { if (current === generation) select(index) })
        list.append(button)
      }
      events.ready?.(result, { ...input })
    } catch {
      if (current !== generation) return
      status.textContent = 'Recommendations are unavailable. Try again when you’re ready.'
      retry.hidden = false
    } finally {
      if (current === generation) root.removeAttribute('aria-busy')
    }
  }
  return { show, reset, select, close, reopen }
}
