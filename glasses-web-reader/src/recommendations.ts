import { mergeSessionActions, RECOMMENDATION_SELECTED, STRIP_HEADING, validBatch, type RecommendationBatch,
  type RecommendationContext, type RecommendationSelection, type SessionActionLike } from '../shared/recommendations'

export interface RecommendationEvents {
  /** Options are ready for this page: open the strip. */
  ready?(batch: RecommendationBatch, context: RecommendationContext): void
  /** The options changed (session actions arrived, ran or expired); update without reopening. */
  changed?(batch: RecommendationBatch): void
  selected?(selection: RecommendationSelection): void
  closed?(): void
  opened?(): void
  reset?(): void
}

/**
 * The phone's "Focal can help" panel, and the source of truth for the glasses strip:
 * catalog/Claude options for the page, merged with the watcher's session actions.
 */
export function mountRecommendations(root: HTMLElement,
  request: (context: RecommendationContext, signal: AbortSignal) => Promise<RecommendationBatch>,
  events: RecommendationEvents = {}) {
  const document = root.ownerDocument
  const window = document.defaultView!
  root.classList.add('recommendations')
  root.hidden = true
  root.setAttribute('role', 'region')
  root.setAttribute('aria-label', STRIP_HEADING)
  root.innerHTML = `
    <div class="rec-header">
      <div><span class="rec-eyebrow">${STRIP_HEADING}</span><h2>A useful next step</h2></div>
      <button class="rec-close" type="button" data-close aria-label="Close suggestions" title="Close suggestions">
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
  /** The page's own options (catalog or Claude); undefined while loading or after a failure. */
  let base: RecommendationBatch | undefined
  let session: SessionActionLike[] = []
  let purchase = false
  /** What's offered now: base merged with session actions. */
  let current: RecommendationBatch | undefined
  /** ready() has fired for this page. */
  let announced = false
  /** Closed by the user: new session actions don't reopen it on this page. */
  let dismissed = false
  let failed = false

  function reset() {
    generation++
    controller?.abort()
    controller = undefined
    base = current = context = undefined
    session = []
    purchase = announced = dismissed = failed = false
    root.hidden = true
    root.removeAttribute('aria-busy')
    list.replaceChildren()
    window.removeEventListener('keydown', escape)
    events.reset?.()
  }
  function close() {
    const hadFocus = root.contains(document.activeElement)
    dismissed = true
    if (current?.actions.length) {
      root.hidden = true
      window.removeEventListener('keydown', escape)
    } else reset()
    events.closed?.()
    if (hadFocus) document.querySelector<HTMLElement>('#url')?.focus({ preventScroll: true })
  }
  function reopen() {
    if (!current?.actions.length || !context) return false
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
    const action = current?.actions[index]
    if (!context || !current || !action || root.hidden) return
    for (const child of list.querySelectorAll<HTMLButtonElement>('button')) {
      child.setAttribute('aria-pressed', String(child.dataset.action === action.id))
    }
    status.textContent = action.session_action_id ? 'Review and approve it on your glasses.' : 'Running on your glasses…'
    const detail: RecommendationSelection = { schema_version: '1.0', request_id: window.crypto.randomUUID(),
      source: current.source, action: { ...action }, context: { ...context } }
    events.selected?.(detail)
    root.dispatchEvent(new window.CustomEvent(RECOMMENDATION_SELECTED, { bubbles: true, detail }))
  }

  function draw(batch: RecommendationBatch) {
    const gen = generation
    list.replaceChildren(...batch.actions.map((action, index) => {
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'rec-action'
      button.dataset.action = action.id
      button.dataset.capability = action.capability
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
      button.addEventListener('click', () => { if (gen === generation) select(index) })
      return button
    }))
  }

  /** Recomputes the options from the page's own and the session's, and tells the glasses. */
  function update() {
    if (!context) return
    const page = { url: context.url, title: context.title }
    const actions = mergeSessionActions(base?.actions ?? [], session, purchase)
    const next: RecommendationBatch = { source: base?.source ?? 'session', page, actions }
    const same = current && JSON.stringify(current.actions) === JSON.stringify(next.actions)
    current = next
    if (!same) draw(next)
    if (!actions.length) {
      if (controller) return
      if (failed) { status.textContent = 'Suggestions are unavailable. Try again when you’re ready.'; retry.hidden = false }
      else status.textContent = 'Nothing Focal can do on this page.'
      if (announced && !same) events.changed?.(next)
      return
    }
    retry.hidden = true
    status.textContent = 'Choose an action to get started.'
    if (!announced) {
      if (dismissed) return
      announced = true
      root.hidden = false
      window.addEventListener('keydown', escape)
      events.ready?.(next, { ...context })
    } else if (!same) events.changed?.(next)
  }

  /** The watcher's actions for the page on the glasses (null page: none). */
  function setSession(page: RecommendationContext | null, actions: SessionActionLike[], isPurchase: boolean) {
    if (!page) { if (session.length) { session = []; update() } return }
    if (context && context.url !== page.url) return
    if (!context) {
      if (!actions.some(a => a.status === 'proposed')) return
      context = { ...page }
      find('[data-page]').textContent = page.title
    }
    session = actions.map(a => ({ action_id: a.action_id, label: a.label, status: a.status, ...(a.type ? { type: a.type } : {}) }))
    purchase = isPurchase
    update()
  }

  async function show(input: RecommendationContext) {
    const keep = context?.url === input.url ? { session, purchase } : { session: [], purchase: false }
    reset()
    session = keep.session
    purchase = keep.purchase
    const now = generation
    context = { ...input }
    controller = new AbortController()
    root.hidden = false
    root.setAttribute('aria-busy', 'true')
    find('[data-page]').textContent = input.title
    status.textContent = 'Checking what Focal can do here…'
    retry.hidden = true
    window.addEventListener('keydown', escape)
    try {
      const result = await request(input, controller.signal)
      if (now !== generation) return
      if (!validBatch(result) || result.page.url !== input.url) throw new Error('Invalid recommendations')
      base = result
    } catch {
      if (now !== generation) return
      failed = true
    } finally {
      if (now === generation) { controller = undefined; root.removeAttribute('aria-busy') }
    }
    update()
    if (!current?.actions.length && !failed) root.hidden = true
  }
  return { show, reset, select, close, reopen, session: setSession, current: () => current }
}
