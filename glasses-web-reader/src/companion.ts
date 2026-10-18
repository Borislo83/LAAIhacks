import type { CondensedPage, CondenseEvent, SiteKind } from '../shared/types'
import type { PageSpec } from './screens'
import { mountBackendPanel } from './lensos/panel'
import { paintLens } from './lens-canvas'
import type { ActionGroup, BackendState } from './lensos/session'
import type { DisplayState, Preferences } from './lensos/types'
import { mountRecommendations, type RecommendationEvents } from './recommendations'
import { recommendActions } from './api'
import { shoppingActions, type RecommendationBatch, type RecommendationContext } from '../shared/recommendations'
import { toContext } from './lensos/page'
import { RECOMMENDATION_LAYOUTS, type RecommendationLayout } from './glasses-recommendations'
import './recommendations.css'
import './companion.css'

// Phone-side UI inside the Even app WebView. The front (Home, Library,
// Settings) is for the wearer; the developer console behind Settings holds
// the live mirror, remote input, voice feed, typed commands, account and condense log.

interface CompanionState {
  doc: CondensedPage | null
  status: string | null
  recent: { url: string; title: string }[]
  bookmarks: { url: string; title: string; position: number; where?: string }[]
  /** The page on the glasses is bookmarked. */
  bookmarked: boolean
  /** Exactly what the glasses are showing, drawn as a scaled preview. */
  spec: PageSpec
  position: string
  kind?: SiteKind
  /** What the glasses are showing (deck, menu, voice...), for the console. */
  view: string
  voice: VoiceUi
  handsFree: boolean
  /** Focal suggests next steps (its real capabilities) after each page loads. */
  recommendations: boolean
  recommendationLayout: RecommendationLayout
  /** Item labels when the glasses show a list (menu, contents, links...). */
  list: string[] | null
  /** The item the mirror's highlight is on in that list. */
  listIndex: number
  /** Hands-free is waiting for the wake phrase right now. */
  listening: boolean
  wakePhrase: string
  /** Everything heard while hands-free, newest first. */
  heard: HeardLine[]
}

export interface HeardLine {
  text: string
  /** A wake-phrase command that was sent to the server. */
  command?: boolean
  /** The page a command opened. */
  opened?: boolean
  /** Only the start of a sentence still being spoken. */
  pending?: boolean
}

export interface VoiceUi {
  phase: 'idle' | 'listening' | 'sending' | 'searching' | 'done' | 'error'
  /** Transcript (live while listening, final when done), or the message when error. */
  text: string
  /** A live-preview failure shown while still listening. */
  note?: string
  /** The page the command opened. */
  url?: string
}

interface CompanionOptions {
  getState: () => CompanionState
  onLoad: (url: string) => void
  /** Bookmark the page on the glasses, or remove its bookmark. */
  onBookmark: () => void
  onOpenBookmark: (url: string) => void
  onRemoveBookmark: (url: string) => void
  onStep: (delta: 1 | -1) => void
  onSelect: () => void
  onBack: () => void
  /** Open the glasses menu, or close it when already open. */
  onMenu: () => void
  /** Run item `index` of the list on the glasses. */
  onPick: (index: number) => void
  /** Start listening, or stop and send when already listening. */
  onVoice: () => void
  onHandsFree: (on: boolean) => void
  onRecommendations: (on: boolean) => void
  onRecommendationLayout: (layout: RecommendationLayout) => void
  /** Send typed text to Focal as if the wearer had said it. */
  onInject: (text: string) => void
  recommendations?: RecommendationEvents
  backend: {
    state(): BackendState
    connect(token: string, g2Token: string): void
    disconnect(): void
    save(p: Omit<Preferences, 'notes'>): void
    review(group: ActionGroup): void
    sync(): void
    back(): void
  }
}

const KIND_LABEL: Record<SiteKind, string> = {
  news: 'News',
  shop: 'Shopping',
  software: 'Software',
  forum: 'Forum',
  live: 'Live info',
  reference: 'Reference',
}

export interface Companion {
  update(): void
  setBusy(busy: boolean): void
  setError(message: string): void
  resetRecommendations(): void
  recommend(page: CondensedPage): void
  /** The watcher's session actions for the page on the glasses (null: none / no page). */
  sessionActions(page: CondensedPage | null, display: DisplayState | null): void
  selectRecommendation(index: number): void
  closeRecommendations(): void
  reopenRecommendations(): boolean
  /** Live mic loudness (0..1), plus elapsed time while capturing a command. */
  voiceLevel(level: number, ms?: number): void
  /** A page load starts: begin a fresh condense log in the console. */
  condenseStart(url: string): void
  condenseEvent(event: CondenseEvent): void
}

type Tab = 'home' | 'library' | 'settings' | 'console'
type ConsoleTab = 'glasses' | 'voice' | 'backend'
const TAB_KEY = 'webreader.companion.tab'
const PRIORITIES = [['balanced', 'Balanced'], ['price', 'Price'], ['quality', 'Quality']] as const

const icon = (paths: string, size = 20) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`
const ICON_HOME = '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>'
const ICON_BOOKMARK = '<path d="M6 3h12v18l-6-4-6 4z"/>'
const ICON_SLIDERS = '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>'
const ICON_MIC = '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>'
const ICON_CHEVRON = '<path d="M9 6l6 6-6 6"/>'
const ICON_BACK = '<path d="M15 6l-6 6 6 6"/>'

/** Focal's mark: a four-corner viewfinder around the lens-green focus point. */
const focalMark = (size: number) =>
  `<svg class="focal-mark" width="${size}" height="${size}" viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="5" aria-hidden="true"><path d="M8 20 V8 H20 M44 8 H56 V20 M56 44 V56 H44 M20 56 H8 V44"/><circle cx="32" cy="32" r="8" fill="var(--lens)" stroke="none"/></svg>`
const focalLockup = (size: number) => `<span class="focal" role="img" aria-label="Focal">${focalMark(size)}<span aria-hidden="true">focal</span></span>`

/** A scaled copy of the 576x288 glasses display inside `glass`. */
function mountMirror(glass: HTMLElement) {
  const canvas = document.createElement('canvas')
  canvas.className = 'glass-inner'
  canvas.width = 576
  canvas.height = 288
  glass.append(canvas)
  return {
    draw(spec: PageSpec) {
      paintLens(canvas, spec)
      glass.setAttribute('aria-label', spec.texts.map(t => t.text.trim()).filter(Boolean).join(' / ') || 'Glasses display preview')
    },
  }
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, props: Partial<HTMLElementTagNameMap[K]> = {}, ...children: (Node | string)[]) {
  const node = Object.assign(document.createElement(tag), props)
  node.append(...children)
  return node
}

export function mountCompanion(root: HTMLElement, opts: CompanionOptions): Companion {
  root.style.display = 'block'
  root.innerHTML = `
    <div class="debug-strip" id="strip" hidden><span>DEBUG MODE</span><span class="strip-brand">${focalMark(13)}focal · web reader</span></div>
    <main class="wr" id="wr">
      <div class="page" data-tab="home">
        <header>
          <div class="brand-bar">${focalLockup(26)}</div>
          <h1>Web Reader</h1>
        </header>
        <form id="f">
          <label class="label" for="url">01 · Open a page</label>
          <div class="url-field">
            <input id="url" type="text" inputmode="url" autocapitalize="off" spellcheck="false" autocomplete="url" placeholder="Paste a link or type a site" required />
            <button class="clear" id="clear" type="button" aria-label="Clear link" hidden>×</button>
          </div>
          <div class="pair">
            <button class="btn dark" id="go" type="submit">Send to glasses</button>
            <button class="btn" id="askVoice" type="button">${icon(ICON_MIC, 17)}<span>Ask by voice</span></button>
          </div>
          <p class="msg" id="msg" role="status"></p>
        </form>
        <section>
          <h2 class="label">02 · On the lens</h2>
          <div class="card">
            <div class="glass" id="glassHome" role="img" aria-label="Glasses display preview"></div>
            <div class="now">
              <div><strong id="title">Nothing on the lens</strong><small id="where">Send a page to start</small></div>
              <button class="btn icon-btn" id="bm" type="button" aria-pressed="false" aria-label="Bookmark this page" hidden>${icon(ICON_BOOKMARK, 18)}</button>
            </div>
          </div>
        </section>
        <aside id="recommendations" hidden></aside>
      </div>

      <div class="page" data-tab="library" hidden>
        <header>
          <h1>Library</h1>
        </header>
        <div>
          <label class="label" for="find">Find a saved page</label>
          <input id="find" type="search" placeholder="Search bookmarks and history" />
        </div>
        <section>
          <div class="row-head"><h2 class="label">Bookmarks</h2><span class="label" id="bmCount"></span></div>
          <ul class="links" id="bookmarks"></ul>
          <p class="empty" id="bmEmpty">No bookmarks yet. Bookmark a page from Home or the glasses menu.</p>
        </section>
        <section>
          <h2 class="label">Recent</h2>
          <ul class="links" id="recent"></ul>
          <p class="empty" id="recentEmpty">Pages you open show up here.</p>
        </section>
      </div>

      <div class="page" data-tab="settings" hidden>
        <header><h1>Settings</h1></header>
        <section>
          <h2 class="label">On the lens</h2>
          <div class="card rows">
            <label class="switch">Suggest next steps after each page<input type="checkbox" id="recs" /></label>
            <div class="setting">
              <span>Suggestion style</span>
              <div class="seg" id="layouts" role="group" aria-label="Suggestion style"></div>
            </div>
          </div>
        </section>
        <section>
          <h2 class="label">Voice</h2>
          <div class="card rows">
            <label class="switch">Hands-free listening<input type="checkbox" id="hf" /></label>
            <div class="switch">Wake phrase<span class="value" id="wake"></span></div>
          </div>
        </section>
        <section id="shopping">
          <h2 class="label">Shopping</h2>
          <div class="seg" id="priorities" role="group" aria-label="Shopping priority"></div>
        </section>
        <section>
          <h2 class="label">Account</h2>
          <div class="card rows">
            <div class="switch"><span id="acct">Not signed in</span><button class="btn" id="signOut" type="button" style="min-height:36px" hidden>Sign out</button></div>
          </div>
        </section>
        <button class="dev-link" id="openConsole" type="button">
          <span>Developer console</span>${icon(ICON_CHEVRON, 18)}
        </button>
      </div>

      <div class="page" data-tab="console" hidden>
        <div class="console-top">
          <button class="exit" id="exitConsole" type="button">${icon(ICON_BACK, 18)}Exit console</button>
          <span class="label" id="consoleState" style="margin:0"></span>
        </div>
        <h1 class="mono-h1">console/<span id="consoleName">glasses</span></h1>
        <nav class="subnav" aria-label="Console sections">
          <button type="button" data-sub="glasses">GLASSES</button>
          <button type="button" data-sub="voice">VOICE</button>
          <button type="button" data-sub="backend">BACKEND</button>
        </nav>

        <div class="page" data-sub-panel="glasses">
          <section>
            <div class="row-head"><h2 class="label">Live mirror <span id="count"></span></h2><button class="text-btn" id="openFull" type="button">fullscreen ⤢</button></div>
            <div class="glass" id="glassConsole" role="img" aria-label="Glasses display preview"></div>
          </section>
          <section>
            <h2 class="label">Remote input</h2>
            <div class="remote">
              <button class="btn" type="button" id="prev">▲ up</button>
              <button class="btn dark" type="button" id="sel">● tap</button>
              <button class="btn" type="button" id="next">▼ down</button>
              <button class="btn" type="button" id="menu">≡ menu</button>
              <button class="btn wide" type="button" id="back-control">●● double-tap · back</button>
            </div>
          </section>
          <section id="picksWrap" hidden>
            <h2 class="label">List on the glasses</h2>
            <ul class="picks" id="picks" aria-label="Glasses list"></ul>
          </section>
          <form id="rawForm">
            <label class="label" for="raw">Load URL</label>
            <div class="inline">
              <input id="raw" type="url" inputmode="url" autocomplete="off" placeholder="https://" required />
              <button class="btn warn console-btn" type="submit">reload</button>
            </div>
          </form>
          <pre class="log" id="info"></pre>
        </div>

        <div class="page" data-sub-panel="voice" hidden>
          <form id="injectForm">
            <label class="label" for="inject">Inject a message to Focal · handled as if spoken</label>
            <div class="inline">
              <input id="inject" type="text" autocomplete="off" autocapitalize="sentences" enterkeyhint="send" placeholder="Text legend. I'm running late" required />
              <button class="btn dark console-btn" id="injectGo" type="submit">send</button>
            </div>
          </form>
          <section class="card voice-card">
            <div class="phase" id="phase"><span id="phaseText">phase=idle</span><span id="phaseTime"></span></div>
            <div class="meter" aria-hidden="true"><div id="meter"></div></div>
            <p class="msg" id="vmsg" role="status"></p>
            <p class="heard" id="heard"></p>
            <div class="pair">
              <button class="btn dark console-btn" id="mic" type="button">start listening</button>
              <button class="btn console-btn" id="voiceCancel" type="button">cancel</button>
            </div>
          </section>
          <label class="switch card" style="padding:0 12px;min-height:46px">hands-free · <span id="hfName"></span><input type="checkbox" id="hf2" /></label>
          <section>
            <div class="row-head"><h2 class="label">Heard · newest first</h2></div>
            <ul class="feed" id="feed" aria-label="Everything heard" aria-live="polite"></ul>
            <p class="empty" id="feedEmpty">Turn on hands-free to see everything the glasses hear.</p>
          </section>
        </div>

        <div class="page" data-sub-panel="backend" hidden>
          <section class="card" id="backend"></section>
          <section>
            <div class="row-head"><h2 class="label">Last condense stream</h2><button class="text-btn" id="copyLog" type="button">copy</button></div>
            <pre class="log" id="log">No page loaded yet.</pre>
          </section>
        </div>
      </div>

      <div class="mirror-full" id="mirrorFull" role="dialog" aria-modal="true" aria-label="Live mirror, fullscreen" hidden>
        <div class="glass" id="glassFull" role="img" aria-label="Glasses display preview"></div>
        <div class="full-bar">
          <button class="btn" type="button" data-remote="prev">▲ up</button>
          <button class="btn dark" type="button" data-remote="sel">● tap</button>
          <button class="btn" type="button" data-remote="next">▼ down</button>
          <button class="btn" type="button" data-remote="menu">≡ menu</button>
          <button class="btn" type="button" data-remote="back">●● back</button>
          <button class="btn warn" type="button" id="closeFull">close ✕</button>
        </div>
      </div>
    </main>
    <nav class="tabs" id="tabs" aria-label="Main"><div class="tabs-inner" id="tabsInner">
      <button type="button" data-go="home">${icon(ICON_HOME)}Home</button>
      <button type="button" data-go="library">${icon(ICON_BOOKMARK)}Library</button>
      <button type="button" data-go="settings">${icon(ICON_SLIDERS)}Settings</button>
      <span class="tab-ind" aria-hidden="true"></span></div>
    </nav>
  `

  const $ = <T extends HTMLElement>(id: string) => root.querySelector<T>(`#${id}`)!
  const all = <T extends HTMLElement>(selector: string) => [...root.querySelectorAll<T>(selector)]

  // ── Tabs ──────────────────────────────────────────────────────────────────
  let tab: Tab = 'home'
  let sub: ConsoleTab = 'glasses'
  try {
    const saved = localStorage.getItem(TAB_KEY)
    if (saved === 'library' || saved === 'settings' || saved === 'console') tab = saved
  } catch {}
  /** Replays the short entrance on a page or panel that just came into view. */
  const enter = (node: HTMLElement) => {
    node.classList.remove('enter')
    void node.offsetWidth
    node.classList.add('enter')
  }
  const TAB_ORDER: Tab[] = ['home', 'library', 'settings']
  function showTab(next: Tab) {
    const changed = next !== tab
    tab = next
    try { localStorage.setItem(TAB_KEY, next) } catch {}
    const inConsole = next === 'console'
    for (const page of all('[data-tab]')) {
      page.hidden = page.dataset.tab !== next
      if (changed && !page.hidden) enter(page)
    }
    if (next !== 'console') $('tabsInner').style.setProperty('--i', String(TAB_ORDER.indexOf(next)))
    for (const b of all('[data-go]')) {
      if (b.dataset.go === next) b.setAttribute('aria-current', 'page')
      else b.removeAttribute('aria-current')
    }
    document.documentElement.classList.toggle('console', inConsole)
    $('wr').classList.toggle('console', inConsole)
    $('strip').hidden = !inConsole
    $('tabs').hidden = inConsole
    window.scrollTo(0, 0)
  }
  function showSub(next: ConsoleTab) {
    const changed = next !== sub
    sub = next
    for (const panel of all('[data-sub-panel]')) {
      panel.hidden = panel.dataset.subPanel !== next
      if (changed && !panel.hidden) enter(panel)
    }
    for (const b of all('[data-sub]')) {
      if (b.dataset.sub === next) b.setAttribute('aria-current', 'page')
      else b.removeAttribute('aria-current')
    }
    $('consoleName').textContent = next
  }
  $('tabs').addEventListener('click', e => {
    const b = (e.target as HTMLElement).closest<HTMLButtonElement>('[data-go]')
    if (b) showTab(b.dataset.go as Tab)
  })
  root.querySelector('.subnav')!.addEventListener('click', e => {
    const b = (e.target as HTMLElement).closest<HTMLButtonElement>('[data-sub]')
    if (b) showSub(b.dataset.sub as ConsoleTab)
  })
  // A short tick on Android for taps on controls (iOS WebViews ignore it).
  root.addEventListener('click', e => {
    const target = (e.target as HTMLElement).closest('button:not(:disabled), input[type=checkbox]')
    if (target) try { navigator.vibrate?.(8) } catch {}
  })
  $('openConsole').addEventListener('click', () => showTab('console'))
  $('exitConsole').addEventListener('click', () => showTab('settings'))

  // ── Home ──────────────────────────────────────────────────────────────────
  const input = $<HTMLInputElement>('url')
  const go = $<HTMLButtonElement>('go')
  const msg = $<HTMLParagraphElement>('msg')
  const clear = $<HTMLButtonElement>('clear')
  const syncClear = () => { clear.hidden = !input.value }
  input.addEventListener('input', syncClear)
  clear.addEventListener('click', () => {
    input.value = ''
    syncClear()
    input.focus()
  })
  $('f').addEventListener('submit', e => {
    e.preventDefault()
    const url = input.value.trim()
    if (!url) return
    msg.textContent = ''
    msg.classList.remove('err')
    opts.onLoad(url)
  })
  $('askVoice').addEventListener('click', () => opts.onVoice())
  $('bm').addEventListener('click', () => opts.onBookmark())
  const homeMirror = mountMirror($('glassHome'))

  // ── Library ───────────────────────────────────────────────────────────────
  const find = $<HTMLInputElement>('find')
  const bookmarkList = $<HTMLUListElement>('bookmarks')
  const recentList = $<HTMLUListElement>('recent')
  find.addEventListener('input', () => renderLibrary(opts.getState()))
  bookmarkList.addEventListener('click', e => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-url]')
    if (!btn) return
    if (btn.dataset.remove) opts.onRemoveBookmark(btn.dataset.url!)
    else {
      opts.onOpenBookmark(btn.dataset.url!)
      showTab('home')
    }
  })
  recentList.addEventListener('click', e => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-url]')
    if (!btn) return
    input.value = btn.dataset.url!
    syncClear()
    opts.onLoad(btn.dataset.url!)
    showTab('home')
  })
  const matches = (q: string, ...fields: string[]) => !q || fields.some(f => f.toLowerCase().includes(q))
  const hostOf = (url: string) => { try { return new URL(url).host.replace(/^www\./, '') } catch { return url } }
  function linkRow(url: string, title: string, meta: string, removable: boolean) {
    const open = el('button', { type: 'button', className: 'open' }, el('span', {}, title), el('small', {}, meta))
    open.dataset.url = url
    const li = el('li', {}, open)
    if (removable) {
      const remove = el('button', { type: 'button', className: 'remove', textContent: '×' })
      remove.dataset.url = url
      remove.dataset.remove = '1'
      remove.setAttribute('aria-label', `Remove bookmark: ${title}`)
      li.append(remove)
    }
    return li
  }
  function renderLibrary(s: CompanionState) {
    const q = find.value.trim().toLowerCase()
    const marks = s.bookmarks.filter(b => matches(q, b.title, b.url))
    const recent = s.recent.filter(r => matches(q, r.title, r.url))
    $('bmCount').textContent = s.bookmarks.length ? String(s.bookmarks.length).padStart(2, '0') : ''
    bookmarkList.replaceChildren(...marks.map(b =>
      linkRow(b.url, b.title, b.position && b.where ? `${hostOf(b.url)} · ${b.where}` : hostOf(b.url), true)))
    recentList.replaceChildren(...recent.map(r => linkRow(r.url, r.title, hostOf(r.url), false)))
    bookmarkList.hidden = !marks.length
    recentList.hidden = !recent.length
    $('bmEmpty').hidden = !!marks.length
    $('recentEmpty').hidden = !!recent.length
    $('bmEmpty').textContent = q && s.bookmarks.length ? 'No bookmarks match.' : 'No bookmarks yet. Bookmark a page from Home or the glasses menu.'
    $('recentEmpty').textContent = q && s.recent.length ? 'No recent pages match.' : 'Pages you open show up here.'
  }

  // ── Settings ──────────────────────────────────────────────────────────────
  const recs = $<HTMLInputElement>('recs')
  recs.addEventListener('change', () => opts.onRecommendations(recs.checked))
  const hf = $<HTMLInputElement>('hf')
  const hf2 = $<HTMLInputElement>('hf2')
  hf.addEventListener('change', () => opts.onHandsFree(hf.checked))
  hf2.addEventListener('change', () => opts.onHandsFree(hf2.checked))
  const layouts = $('layouts')
  layouts.append(...RECOMMENDATION_LAYOUTS.map(l => {
    const b = el('button', { type: 'button', textContent: l.label.replace(/ (popup|strip|list)$/, '') })
    b.dataset.layout = l.id
    return b
  }))
  layouts.addEventListener('click', e => {
    const b = (e.target as HTMLElement).closest<HTMLButtonElement>('[data-layout]')
    if (b) opts.onRecommendationLayout(b.dataset.layout as RecommendationLayout)
  })
  const priorities = $('priorities')
  priorities.append(...PRIORITIES.map(([id, label]) => {
    const b = el('button', { type: 'button', textContent: label })
    b.dataset.priority = id
    return b
  }))
  priorities.addEventListener('click', e => {
    const b = (e.target as HTMLElement).closest<HTMLButtonElement>('[data-priority]')
    const p = opts.backend.state().preferences
    if (!b || !p) return
    opts.backend.save({ proactive_enabled: p.proactive_enabled, display_density: p.display_density,
      shopping_priority: b.dataset.priority as Preferences['shopping_priority'] })
  })
  $('signOut').addEventListener('click', () => opts.backend.disconnect())

  // ── Console ───────────────────────────────────────────────────────────────
  const consoleMirror = mountMirror($('glassConsole'))
  $('prev').addEventListener('click', () => opts.onStep(-1))
  $('next').addEventListener('click', () => opts.onStep(1))
  $('sel').addEventListener('click', () => opts.onSelect())
  $('back-control').addEventListener('click', () => opts.onBack())
  $('menu').addEventListener('click', () => opts.onMenu())
  // Fullscreen mirror: an overlay everywhere, plus real fullscreen where the
  // WebView allows it (iOS only lets video go fullscreen).
  const full = $<HTMLDivElement>('mirrorFull')
  const fullMirror = mountMirror($('glassFull'))
  const closeFullOnEscape = (e: KeyboardEvent) => { if (e.key === 'Escape') closeFull() }
  function openFull() {
    full.hidden = false
    fullMirror.draw(opts.getState().spec)
    full.requestFullscreen?.().catch(() => {})
    document.addEventListener('keydown', closeFullOnEscape)
    $('closeFull').focus()
  }
  function closeFull() {
    if (full.hidden) return
    full.hidden = true
    document.removeEventListener('keydown', closeFullOnEscape)
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => {})
    $('openFull').focus()
  }
  document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement) closeFull() })
  $('openFull').addEventListener('click', openFull)
  $('closeFull').addEventListener('click', closeFull)
  const remote: Record<string, () => void> = {
    prev: () => opts.onStep(-1), next: () => opts.onStep(1), sel: () => opts.onSelect(),
    menu: () => opts.onMenu(), back: () => opts.onBack(),
  }
  full.addEventListener('click', e => {
    const b = (e.target as HTMLElement).closest<HTMLButtonElement>('[data-remote]')
    if (b) remote[b.dataset.remote!]()
  })
  const picks = $<HTMLUListElement>('picks')
  picks.addEventListener('click', e => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-pick]')
    if (btn) opts.onPick(Number(btn.dataset.pick))
  })
  const raw = $<HTMLInputElement>('raw')
  $('rawForm').addEventListener('submit', e => {
    e.preventDefault()
    const url = raw.value.trim()
    if (url) opts.onLoad(url)
  })
  $('mic').addEventListener('click', () => opts.onVoice())
  $('voiceCancel').addEventListener('click', () => opts.onBack())
  const meter = $<HTMLDivElement>('meter')
  const vmsg = $<HTMLParagraphElement>('vmsg')
  let listenNote = ''
  const backendPanel = mountBackendPanel($('backend'), opts.backend)
  const inject = $<HTMLInputElement>('inject')
  $('injectForm').addEventListener('submit', e => {
    e.preventDefault()
    const text = inject.value.trim()
    if (!text) return
    opts.onInject(text)
    inject.value = ''
  })
  // Product/purchase pages get the shopping capabilities without a model call.
  const options = (context: RecommendationContext, signal: AbortSignal): Promise<RecommendationBatch> => {
    const shopping = shoppingActions(context)
    return shopping ? Promise.resolve({ source: 'catalog', page: { url: context.url, title: context.title }, actions: shopping })
      : recommendActions(context, signal)
  }
  const recommendations = mountRecommendations($('recommendations'), options, opts.recommendations)
  const contextOf = (page: CondensedPage): RecommendationContext => {
    const { url, title, text, product } = toContext(page)
    return { url, title: title || url, text, ...(page.deck?.kind ? { kind: page.deck.kind } : {}), ...(product ? { product } : {}) }
  }

  // The condense stream of the last page load, with time since it started.
  const logEl = $<HTMLPreElement>('log')
  let logStart = 0
  const logLine = (text: string, bad = false) => {
    const t = el('span', { className: 't', textContent: `${((performance.now() - logStart) / 1000).toFixed(1)}s`.padEnd(6) })
    logEl.append(t, el('span', { className: bad ? 'bad' : '', textContent: ` ${text}\n` }))
    logEl.scrollTop = logEl.scrollHeight
  }
  $('copyLog').addEventListener('click', async () => {
    const btn = $('copyLog')
    try {
      await navigator.clipboard.writeText(logEl.textContent ?? '')
      btn.textContent = 'copied ✓'
    } catch { btn.textContent = 'copy failed' }
    setTimeout(() => { btn.textContent = 'copy' }, 1400)
  })

  showTab(tab)
  showSub(sub)

  return {
    resetRecommendations() { recommendations.reset() },
    selectRecommendation(index) { recommendations.select(index) },
    closeRecommendations() { recommendations.close() },
    reopenRecommendations() { return recommendations.reopen() },
    recommend(page) {
      if (opts.backend.state().preferences?.proactive_enabled === false) return
      const context = contextOf(page)
      if (context.text.trim() || shoppingActions(context)) void recommendations.show(context)
    },
    sessionActions(page, display) {
      if (!page || !display?.page) return recommendations.session(null, [], false)
      recommendations.session(contextOf(page), display.actions, display.page.kind === 'product')
    },
    update() {
      backendPanel.update()
      const s = opts.getState()
      const showing = !!s.doc && !s.status

      // Home
      homeMirror.draw(s.spec)
      $('title').textContent = s.doc?.title ?? 'Nothing on the lens'
      $('where').textContent = s.doc
        ? [s.doc.site, s.kind ? KIND_LABEL[s.kind].toLowerCase() : s.doc.manga ? 'manga' : '', showing ? s.position : '']
          .filter(Boolean).join(' · ')
        : 'Send a page to start'
      const bm = $<HTMLButtonElement>('bm')
      bm.hidden = !showing
      if (bm.getAttribute('aria-pressed') !== String(s.bookmarked) && s.bookmarked) enter(bm)
      bm.setAttribute('aria-pressed', String(s.bookmarked))
      bm.setAttribute('aria-label', s.bookmarked ? 'Remove bookmark' : 'Bookmark this page')
      if (showing && !msg.classList.contains('err')) {
        msg.textContent = s.doc!.truncated ? 'Long page; only the first part was used.' : ''
      }
      const v = s.voice
      const listening = v.phase === 'listening'
      const busyVoice = v.phase === 'sending' || v.phase === 'searching'
      const ask = $<HTMLButtonElement>('askVoice')
      ask.classList.toggle('on', listening)
      ask.querySelector('span')!.textContent = listening ? 'Stop and send' : busyVoice ? 'Searching…' : 'Ask by voice'
      ask.disabled = busyVoice

      // Library and settings
      renderLibrary(s)
      recs.checked = s.recommendations
      for (const b of all<HTMLButtonElement>('[data-layout]')) {
        b.setAttribute('aria-pressed', String(b.dataset.layout === s.recommendationLayout))
        b.disabled = !s.recommendations
      }
      hf.checked = hf2.checked = s.handsFree
      $('wake').textContent = `"${s.wakePhrase}"`
      $('hfName').textContent = `"${s.wakePhrase}"`
      const b = opts.backend.state()
      $('shopping').hidden = !b.connected || !b.preferences
      for (const p of all<HTMLButtonElement>('[data-priority]')) {
        p.setAttribute('aria-pressed', String(p.dataset.priority === b.preferences?.shopping_priority))
      }
      $('acct').textContent = b.connected ? 'Signed in' : 'Not signed in · connect in the developer console'
      $('signOut').hidden = !b.connected

      // Console: glasses
      consoleMirror.draw(s.spec)
      if (!full.hidden) fullMirror.draw(s.spec)
      $('count').textContent = showing && s.position ? `· ${s.position}` : ''
      $('consoleState').textContent = `view=${s.view}${showing && s.position ? ` · ${s.position}` : ''}`
      $('picksWrap').hidden = !s.list
      picks.replaceChildren(...(s.list ?? []).map((label, i) => {
        const btn = el('button', { type: 'button', textContent: label })
        btn.dataset.pick = String(i)
        if (i === s.listIndex) btn.setAttribute('aria-current', 'true')
        return el('li', {}, btn)
      }))
      if (s.doc && document.activeElement !== raw) raw.value = s.doc.url
      $('info').textContent = s.doc
        ? [`site=${s.doc.site}  kind=${s.kind ?? (s.doc.manga ? 'manga' : '-')}`,
          `words=${s.doc.wordCount.toLocaleString()}${s.doc.truncated ? '  truncated' : ''}  view=${s.view}`,
          s.status ? `status=${s.status.split('\n')[0]}` : ''].filter(Boolean).join('\n')
        : 'Nothing loaded.'

      // Console: voice
      const mic = $<HTMLButtonElement>('mic')
      mic.textContent = listening ? 'stop + send' : 'start listening'
      mic.disabled = busyVoice
      $<HTMLButtonElement>('injectGo').disabled = busyVoice
      $('phase').classList.toggle('live', listening || s.listening)
      $('phaseText').textContent = `${listening || s.listening ? '● ' : ''}phase=${v.phase}${s.listening && !listening ? ' · waiting for wake phrase' : ''}`
      if (!listening) $('phaseTime').textContent = ''
      if (!listening && !s.listening) meter.style.width = '0'
      listenNote = (listening || v.phase === 'idle') && v.note ? ` · ${v.note}` : ''
      vmsg.classList.toggle('err', v.phase === 'error' || !!listenNote)
      vmsg.textContent = {
        idle: s.listening ? `Waiting for "${s.wakePhrase}"...${listenNote}` : 'Uses the glasses microphone.',
        listening: `Listening...${listenNote}`,
        sending: 'Sending to the server...',
        searching: 'Searching the web for:',
        done: !v.text ? "Server couldn't make that out." : v.url ? `Opening ${v.url}` : 'Server heard:',
        error: v.text,
      }[v.phase]
      $('heard').textContent = v.phase !== 'error' && v.phase !== 'idle' && v.phase !== 'sending' && v.text ? `"${v.text}"` : ''
      const feed = $<HTMLUListElement>('feed')
      feed.replaceChildren(...s.heard.map(h =>
        el('li', { textContent: h.text, className: h.command ? 'cmd' : h.opened ? 'open' : h.pending ? 'pending' : '' })))
      $('feedEmpty').hidden = !!s.heard.length
    },
    setBusy(busy) {
      go.disabled = busy
      go.textContent = busy ? 'Condensing…' : 'Send to glasses'
      go.classList.toggle('busy', busy)
      if (busy) {
        msg.classList.remove('err')
        msg.textContent = ''
      }
    },
    setError(message) {
      msg.classList.add('err')
      msg.textContent = message
      if (logStart) logLine(`error    ${message}`, true)
    },
    voiceLevel(level, ms) {
      meter.style.width = `${Math.round(level * 100)}%`
      if (ms !== undefined) $('phaseTime').textContent = `${(ms / 1000).toFixed(1)}s`
    },
    condenseStart(url) {
      logStart = performance.now()
      logEl.replaceChildren(`GET /api/condense?url=${url}\n`)
    },
    condenseEvent(e) {
      if ('page' in e) logLine(`page     ${e.page.title}${e.page.manga ? ' (manga)' : ''}`)
      else if ('kind' in e) logLine(`kind     ${e.kind}`)
      else if ('screens' in e) logLine(`screens  [${e.screens.map(s => s.layout).join(', ')}]`)
      else if ('replace' in e) logLine(`replace  #${e.replace} ${e.screen.layout}`)
      else if ('done' in e) logLine(`done${e.truncated ? '     truncated' : ''}`)
    },
  }
}
