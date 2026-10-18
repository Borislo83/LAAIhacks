import type { CondensedPage } from '../../shared/types'
import { BackendClient, BackendError } from './client'
import { toContext, toPageSubmission } from './page'
import type { DecisionIntent, DecisionOutcome, DisplayState, Preferences, Task } from './types'

export type ActionGroup = 'tasks' | 'session' | 'history'
export interface BackendState {
  connected: boolean; online: boolean; busy: boolean; message: string; sessionId: string
  pageTitle: string; preferences: Preferences | null; display: DisplayState | null
  tasks: Task[]; history: Task[]; taskRevision: number
}
interface Options { changed(): void; saveSession(id: string, pageUrl?: string): void; pollMs?: number }
const empty = (): BackendState => ({ connected: false, online: false, busy: false,
  message: 'Connect to save pages and receive recommendations.', sessionId: '', pageTitle: '',
  preferences: null, display: null, tasks: [], history: [], taskRevision: 0 })
const labels = { price: 'Compare prices', alternatives: 'Find alternatives', reviews: 'Find reviews' }
const errorText = (e: unknown) => e instanceof Error ? e.message : 'Backend unavailable.'

export class BackendSession {
  state = empty()
  private client?: BackendClient
  private lifetime = new AbortController()
  private generation = 0
  private pageGeneration = 0
  private pageId: string | null = null
  private timer?: ReturnType<typeof setTimeout>
  private writes: Promise<void> = Promise.resolve()
  private dismissed = new Set<string>()
  private dispatched = new Set<string>()
  private polling = false
  private suspended = false

  constructor(private readonly options: Options) {}
  private changed() { this.options.changed() }
  disconnect() {
    this.generation++; this.pageGeneration++; this.lifetime.abort(); clearTimeout(this.timer)
    this.client = undefined; this.pageId = null; this.dismissed.clear(); this.dispatched.clear()
    this.polling = false; this.state = empty(); this.changed()
  }
  async connect(client: BackendClient, savedSession = '', restoreDisplay = true) {
    this.disconnect()
    const generation = this.generation
    const pageGeneration = this.pageGeneration
    this.client = client; this.lifetime = new AbortController()
    const signal = this.lifetime.signal
    this.state.busy = true; this.state.message = 'Connecting…'; this.changed()
    try {
      let display: DisplayState | null = null
      if (savedSession) {
        try { display = await client.display(savedSession, undefined, signal) }
        catch (e) { if (!(e instanceof BackendError && e.status === 404)) throw e }
      }
      const sessionId = display?.session_id ?? (await client.createSession(signal)).session_id
      if (!display) display = await client.display(sessionId, undefined, signal)
      const [preferences, history] = await Promise.all([client.preferences(signal), client.history(signal)])
      if (generation !== this.generation) return
      if (!restoreDisplay || pageGeneration !== this.pageGeneration) display = null
      this.state = { ...empty(), connected: true, online: true, sessionId, display, preferences, history,
        pageTitle: display?.page?.title ?? '', message: 'Connected · pages and recommendations are saved to your account.' }
      this.pageId = display?.page?.page_id ?? null
      this.options.saveSession(sessionId); this.changed(); this.schedule()
    } catch (e) {
      if (generation !== this.generation) return
      this.client = undefined; this.state.busy = false; this.state.message = errorText(e); this.changed()
    }
  }
  beginPage() {
    this.pageGeneration++; this.pageId = null; this.dismissed.clear()
    this.state.tasks = []; this.state.display = null; this.state.pageTitle = ''; this.state.taskRevision++
    if (this.state.connected) this.state.busy = false
    if (this.state.connected) this.state.message = 'Reading page; it will sync when ready.'
    this.changed()
  }
  publish(page: CondensedPage): Promise<void> {
    const client = this.client, generation = this.generation, pageGeneration = this.pageGeneration
    if (!client || !this.state.connected) return Promise.resolve()
    const current = () => generation === this.generation && pageGeneration === this.pageGeneration
    const signal = this.lifetime.signal
    const session = this.state.sessionId
    const pageId = `page-${crypto.randomUUID()}`
    const submission = toPageSubmission(page, pageId, `page-${crypto.randomUUID()}`)
    const write = async () => {
      if (!current()) return
      this.state.busy = true; this.state.message = 'Saving page…'; this.changed()
      try {
        // Wait for each write's acknowledgment before the next page is submitted.
        await client.savePage(session, submission, signal)
        if (!current()) return
        this.pageId = pageId; this.state.pageTitle = page.title; this.state.online = true
        this.options.saveSession(session, page.url)
        this.state.message = 'Page saved.'
        if (this.state.preferences?.proactive_enabled) {
          this.state.message = 'Page saved · finding recommendations…'; this.changed()
          const prediction = await client.predict(toContext(page), signal)
          if (!current()) return
          this.state.tasks = prediction.tasks; this.state.taskRevision++
          this.state.message = prediction.tasks.length ? 'Page saved · recommendations ready.' : 'Page saved · no recommendations for this page.'
        } else this.state.message = 'Page saved · proactive recommendations are off.'
        await this.refresh()
      } catch (e) {
        if (current()) { this.state.message = errorText(e); this.state.online = false }
      } finally { if (current()) { this.state.busy = false; this.changed() } }
    }
    this.writes = this.writes.catch(() => {}).then(write)
    return this.writes
  }
  private schedule() {
    clearTimeout(this.timer)
    if (!this.state.connected || this.suspended || this.options.pollMs === 0) return
    this.timer = setTimeout(() => { void this.refresh().finally(() => this.schedule()) }, this.options.pollMs ?? 4000)
  }
  setSuspended(value: boolean) { this.suspended = value; if (value) clearTimeout(this.timer); else this.schedule() }
  async refresh() {
    const client = this.client, generation = this.generation, pageGeneration = this.pageGeneration
    if (!client || !this.state.connected || this.polling) return
    this.polling = true
    try {
      const [display, history] = await Promise.all([
        client.display(this.state.sessionId, undefined, this.lifetime.signal), client.history(this.lifetime.signal),
      ])
      if (generation !== this.generation) return
      this.state.history = history
      if (pageGeneration === this.pageGeneration) {
        if (this.pageId && display?.page?.page_id === this.pageId) this.state.display = display
        else this.state.display = null
        const tasks = this.state.tasks.map(t => history.find(h => h.id === t.id) ?? t)
        if (JSON.stringify(tasks) !== JSON.stringify(this.state.tasks)) { this.state.tasks = tasks; this.state.taskRevision++ }
      }
      this.state.online = true; this.changed()
    } catch (e) {
      if (generation !== this.generation) return
      this.state.online = false; this.state.message = errorText(e)
      if (e instanceof BackendError && [401, 403].includes(e.status)) {
        this.state.connected = false; this.state.message += ' Reconnect with a current token.'
        clearTimeout(this.timer)
      }
      this.changed()
    } finally { if (generation === this.generation) this.polling = false }
  }
  async updatePreferences(change: Omit<Preferences, 'notes'>) {
    const client = this.client, generation = this.generation
    if (!client || !this.state.connected) return
    try {
      // Read before replacing; private notes never become page/model context.
      const latest = await client.preferences(this.lifetime.signal)
      const saved = await client.savePreferences({ ...latest, ...change }, this.lifetime.signal)
      if (generation !== this.generation) return
      this.state.preferences = saved; this.state.message = 'Preferences saved.'; this.changed()
    } catch (e) { if (generation === this.generation) { this.state.message = errorText(e); this.changed() } }
  }
  actionDisplay(group: ActionGroup): DisplayState {
    if (group === 'session' && this.state.display) return this.state.display
    const tasks = group === 'history' ? this.state.history : this.state.tasks
    return { schema_version: '1.0', session_id: this.state.sessionId, revision: this.state.taskRevision,
      page: { page_id: this.pageId ?? 'history', title: group === 'history' ? 'Saved task history' : this.state.pageTitle || 'Recommendations', kind: 'generic', summary: [], facts: [] },
      mode: 'summary', notice: null,
      actions: group === 'session' ? [] : tasks.map(t => ({ action_id: `task:${t.id}`, source: 'task',
        label: labels[t.kind], description: `${typeof t.payload.product === 'string' ? t.payload.product : ''}${t.result?.summary ? `\n${t.result.summary}` : t.error ? `\n${t.error}` : ''}`,
        status: group === 'history' && t.status === 'predicted' ? 'saved' : this.dismissed.has(t.id) ? 'dismissed' : ({ predicted: 'proposed', running: 'running', succeeded: 'completed', failed: 'failed' } as const)[t.status],
        requires_approval: true })) }
  }
  async decide(group: ActionGroup, intent: DecisionIntent): Promise<{ outcome: DecisionOutcome; message: string }> {
    const client = this.client, generation = this.generation
    if (!client || !this.state.connected) return { outcome: 'failed', message: 'Reconnect before confirming.' }
    if (group === 'session') {
      try {
        await client.decide(this.state.sessionId, intent, this.lifetime.signal)
        await this.refresh()
        return { outcome: 'success', message: intent.operation === 'approve' ? 'Approved; waiting for worker.' : 'Rejected.' }
      } catch (e) { return { outcome: !(e instanceof BackendError) || e.uncertain ? 'uncertain' : 'failed', message: errorText(e) } }
    }
    const id = intent.action_id.replace(/^task:/, '')
    const task = this.state.tasks.find(t => t.id === id)
    if (group === 'history' || !task) return { outcome: 'failed', message: 'This recommendation belongs to an earlier page.' }
    if (intent.operation === 'reject') {
      this.dismissed.add(id); this.state.taskRevision++; this.changed()
      return { outcome: 'success', message: 'Dismissed for this page.' }
    }
    try {
      // A lost dispatch response is not permission to run it again. Retry only reads.
      const checking = this.dispatched.has(id)
      this.dispatched.add(id)
      const updated = checking ? await client.task(id, this.lifetime.signal) : await client.dispatch(id, this.lifetime.signal)
      if (generation !== this.generation) return { outcome: 'failed', message: 'Connection changed.' }
      this.state.tasks = this.state.tasks.map(t => t.id === id ? updated : t)
      this.state.history = [updated, ...this.state.history.filter(t => t.id !== id)]
      this.state.taskRevision++; this.changed()
      if (updated.status === 'running' || updated.status === 'predicted') return { outcome: 'uncertain', message: 'Outcome pending. Tap to check status; this will not run it again.' }
      return { outcome: updated.status === 'succeeded' ? 'success' : 'failed', message: updated.result?.summary ?? updated.error ?? updated.status }
    } catch (e) {
      // Even HTTP errors can follow a claim. Poll before considering another run.
      return { outcome: 'uncertain', message: `${errorText(e)} Tap to check status.` }
    }
  }
}
