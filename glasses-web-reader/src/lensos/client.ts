import type { ChatReply, DecisionIntent, DisplayState, MessageSent, PageContext, PageSubmission, Preferences, Task } from './types'

export class BackendError extends Error {
  constructor(message: string, readonly status = 0) { super(message) }
  get uncertain() { return this.status === 0 || this.status === 408 || this.status >= 500 }
}

function validDisplay(value: unknown): value is DisplayState {
  if (!value || typeof value !== 'object') return false
  const d = value as DisplayState
  const text = (v: unknown) => typeof v === 'string'
  return d.schema_version === '1.0' && text(d.session_id) && Number.isSafeInteger(d.revision) && d.revision >= 0 &&
    text(d.mode) && (d.notice === null || text(d.notice)) &&
    (d.page === null || (!!d.page && text(d.page.page_id) && text(d.page.title) && text(d.page.kind) &&
      Array.isArray(d.page.summary) && d.page.summary.every(text) && Array.isArray(d.page.facts) &&
      d.page.facts.every(f => f && text(f.label) && text(f.value)))) && Array.isArray(d.actions) &&
    d.actions.every(a => a && text(a.action_id) && text(a.label) && text(a.description) && text(a.status) && a.requires_approval === true)
}

export class BackendClient {
  constructor(private readonly base: string, private readonly token: string,
    private readonly g2Token = '', private readonly transport: typeof fetch = fetch) {
    const url = new URL(base, typeof location === 'undefined' ? 'http://localhost' : location.href)
    if (url.username || url.password || url.search || url.hash ||
      (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))) {
      throw new BackendError('Use HTTPS or localhost to connect your account.')
    }
    for (const credential of [token, g2Token || token]) {
      if (!credential || /\s/.test(credential) || credential.length > 8192 || credential.startsWith('demo-')) throw new BackendError('Enter a signed access token.')
    }
    this.base = url.href.replace(/\/$/, '')
  }

  private id(id: string) {
    if (!/^[A-Za-z0-9_.:-]{1,160}$/.test(id)) throw new BackendError('Invalid backend identifier.')
    return encodeURIComponent(id)
  }

  async request<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal, decision = false): Promise<T> {
    const controller = new AbortController()
    const abort = () => controller.abort()
    if (signal?.aborted) abort()
    else signal?.addEventListener('abort', abort, { once: true })
    // Agents can take a while; the gateway gives up a few seconds before these.
    const timer = setTimeout(abort, path === '/v1/chat' ? 95_000 : path === '/v1/messages/send' ? 70_000 : path.endsWith('/dispatch') ? 65_000 : 35_000)
    try {
      const transport = this.transport
      const response = await transport(this.base + path, { method, redirect: 'error', cache: 'no-store',
        signal: controller.signal, headers: { Authorization: `Bearer ${decision ? this.g2Token || this.token : this.token}`,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: body === undefined ? undefined : JSON.stringify(body) })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        const message = typeof data.error === 'string' ? data.error : data.error?.message
        throw new BackendError(message ?? `Backend request failed (${response.status}).`, response.status)
      }
      if (response.status === 204) return null as T
      return await response.json() as T
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
  }
  /** Opens a hardcoded route (YouTube, Spotify, a product page) on the laptop. */
  openOnLaptop(url: string, signal?: AbortSignal) { return this.request<{ opened: string }>('/laptop/open', 'POST', { url }, signal) }
  createSession(signal?: AbortSignal) { return this.request<{ session_id: string; revision: number }>('/v1/sessions', 'POST', {}, signal) }
  async display(session: string, after?: number, signal?: AbortSignal) {
    const d = await this.request<DisplayState | null>(`/v1/sessions/${this.id(session)}/display${after === undefined ? '' : `?after_revision=${after}`}`, 'GET', undefined, signal)
    if (d === null && after !== undefined) return null
    if (!validDisplay(d) || d.session_id !== session) throw new BackendError('Invalid backend display response.', 422)
    return d
  }
  savePage(session: string, page: PageSubmission, signal?: AbortSignal) { return this.request(`/v1/sessions/${this.id(session)}/page`, 'PUT', page, signal) }
  predict(context: PageContext, signal?: AbortSignal) { return this.request<{ context_id: string; prediction_source: string; tasks: Task[] }>('/v1/contexts', 'POST', context, signal) }
  history(signal?: AbortSignal) { return this.request<Task[]>('/v1/tasks?limit=20', 'GET', undefined, signal) }
  task(id: string, signal?: AbortSignal) { return this.request<Task>(`/v1/tasks/${this.id(id)}`, 'GET', undefined, signal) }
  dispatch(id: string, signal?: AbortSignal) { return this.request<Task>(`/v1/tasks/${this.id(id)}/dispatch`, 'POST', { confirmed: true }, signal) }
  preferences(signal?: AbortSignal) { return this.request<Preferences>('/v1/preferences', 'GET', undefined, signal) }
  savePreferences(p: Preferences, signal?: AbortSignal) {
    const { display_density, shopping_priority, proactive_enabled, notes } = p
    return this.request<Preferences>('/v1/preferences', 'PUT', { display_density, shopping_priority, proactive_enabled, notes }, signal)
  }
  /** One voice command; the orchestrator picks and calls the agents. */
  chat(text: string, signal?: AbortSignal) {
    // From the glasses: YouTube, Spotify and Amazon cart requests run on the laptop.
    return this.request<ChatReply>('/v1/chat', 'POST', { messages: [{ role: 'user', content: text.slice(0, 4000) }], act_on_laptop: true }, signal)
  }
  /** Sends a reviewed iMessage draft exactly as drafted; only after the user's tap. */
  sendMessage(draft_id: string, text: string, signal?: AbortSignal) {
    this.id(draft_id)
    return this.request<MessageSent>('/v1/messages/send', 'POST', { draft_id, text }, signal)
  }
  async decide(session: string, intent: DecisionIntent, signal?: AbortSignal) {
    const { request_id, expected_revision } = intent
    const response = await this.request<{ action_id: string; status: string; event_id?: string }>(
      `/v1/sessions/${this.id(session)}/actions/${this.id(intent.action_id)}/${intent.operation}`, 'POST',
      { schema_version: '1.0', request_id, expected_revision }, signal, true)
    if (response.action_id !== intent.action_id || response.status !== (intent.operation === 'approve' ? 'approved' : 'rejected') ||
      (intent.operation === 'approve' && typeof response.event_id !== 'string')) throw new BackendError('Decision acknowledgment missing; retry the same decision.')
  }
}
