import type { CondensedPage, SiteKind } from '../shared/types'
import type { PageSpec } from './screens'
import { mountBackendPanel } from './lensos/panel'
import type { ActionGroup, BackendState } from './lensos/session'
import type { DisplayState, Preferences } from './lensos/types'
import { mountRecommendations, type RecommendationEvents } from './recommendations'
import { recommendActions } from './api'
import { shoppingActions, type RecommendationBatch, type RecommendationContext } from '../shared/recommendations'
import { toContext } from './lensos/page'
import './recommendations.css'

// Phone-side UI inside the Even app WebView: URL entry, recent
// pages, and a live mirror of what the glasses are showing.

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
  voice: VoiceUi
  handsFree: boolean
  /** Focal suggests next steps (its real capabilities) after each page loads. */
  recommendations: boolean
  /** Item labels when the glasses show a list (menu, contents, links...). */
  list: string[] | null
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
}

const STYLE = `
  /* Focal blueprint palette and type (apps/dashboard/prototype/blueprint-tokens.css):
     graphite sheet, off-white ink, near-square corners, no decoration-only marks. Lens green marks only live things:
     the mirror, the mic meter and active states. */
  :root {
    color-scheme: dark;
    --sheet: #23272b; --grid: #2c3136; --grid-major: #353b41; --panel: #272b30; --well: #1d2024;
    --hair: #41484f; --ink: #e8ebe4; --muted: #8d949a; --lens: #8ee6a2; --lens-soft: rgba(142,230,162,.12);
    --lens-bg: #121417; --warn: #f2a477; --r: 3px;
    --serif: "Instrument Serif", Georgia, serif; --sans: "Instrument Sans", system-ui, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, monospace;
    /* names the recommendation sheet and backend panel already use */
    --text: var(--ink); --text-dim: var(--muted); --bg: var(--sheet); --surface: var(--panel);
    --input: var(--well); --accent: var(--ink); --on-accent: var(--sheet); --danger: var(--warn);
  }
  html, body { background: var(--sheet) !important; color: var(--ink) !important; -webkit-font-smoothing: antialiased; }
  ::selection { background: var(--lens); color: var(--sheet); }
  .wr :focus-visible { outline: 2px solid var(--lens); outline-offset: 2px; }
  .wr { box-sizing: border-box; width: 100%; max-width: 640px; margin: 0 auto; padding: 24px 16px 40px;
    font: 16px/1.55 var(--sans); display: flex; flex-direction: column; gap: 28px; }
  .wr h1 { font: 400 40px/1 var(--serif); letter-spacing: -.015em; margin: 0; }
  .wr .sub { color: var(--muted); font-size: 13px; margin: 6px 0 0; }
  .wr label.lbl, .wr .feed-head { display: block; font: 12px/1.4 var(--mono); letter-spacing: .02em;
    text-transform: none; color: var(--muted); margin: 0 0 8px; }
  .wr form { display: flex; flex-direction: column; gap: 12px; }
  .wr input[type=url] { width: 100%; box-sizing: border-box; border: 1px solid var(--hair); border-radius: var(--r);
    background: var(--well); color: var(--ink); font: 15px/1.4 var(--mono); padding: 13px 14px; min-height: 48px; }
  .wr input[type=url]:focus { outline: none; border-color: var(--lens); }
  .wr .url-field { position: relative; }
  .wr .url-field input[type=url] { padding-right: 48px; }
  .wr .clear { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); width: 36px; height: 36px;
    border: 0; border-radius: var(--r); background: transparent; color: var(--muted); font: inherit; font-size: 20px;
    line-height: 1; cursor: pointer; }
  /* primary: solid ink, like the blueprint's Open Studio */
  .wr .go { border: 1px solid var(--ink); border-radius: var(--r); background: var(--ink); color: var(--sheet);
    font: 500 15px/1 var(--sans); min-height: 48px; cursor: pointer; }
  .wr .go:hover { background: transparent; color: var(--ink); }
  .wr .go:disabled { opacity: .45; cursor: progress; }
  .wr .msg { font: 12px/1.5 var(--mono); color: var(--muted); min-height: 18px; margin: 0; }
  .wr .msg.err { color: var(--warn); }
  .wr .card { background: var(--panel); border: 1px solid var(--hair); border-radius: var(--r); padding: 16px; }
  .wr .mirror-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 12px; }
  .wr .mirror-head strong { font-size: 14px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wr .mirror-head span { font: 12px var(--mono); color: var(--muted); flex: none; }
  /* what lands on the lens: dark glass, green type */
  .wr .glass { position: relative; width: 100%; aspect-ratio: 2 / 1; background: var(--lens-bg);
    border: 1px solid var(--hair); border-radius: var(--r); overflow: hidden; }
  .wr .glass-inner { position: absolute; left: 0; top: 0; width: 576px; height: 288px; transform-origin: 0 0;
    font: 20px/27px 'FK Grotesk Neue', -apple-system, system-ui, sans-serif; color: var(--lens); letter-spacing: 0; }
  .wr .glass-inner > div { position: absolute; box-sizing: border-box; white-space: pre-wrap; overflow: hidden; border-style: solid; }
  .wr .glass-inner > img { position: absolute; image-rendering: pixelated; mix-blend-mode: screen;
    filter: sepia(1) saturate(6) hue-rotate(62deg) brightness(1.05); }
  .wr .pager { display: grid; grid-template-columns: 1fr 1.2fr 1fr; gap: 8px; margin-top: 12px; }
  .wr .pager button, .wr .recent button { border: 1px solid var(--hair); border-radius: var(--r); background: transparent;
    color: var(--ink); font: 14px/1.2 var(--sans); min-height: 44px; cursor: pointer; }
  .wr .pager .sel { background: var(--ink); color: var(--sheet); border-color: var(--ink); font-weight: 500; }
  @media (hover: hover) { .wr .pager button:hover, .wr .recent button:hover, .wr .back-button:hover,
    .wr .backend-panel button:hover { border-color: var(--ink); } }
  .wr .recent { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  .wr .recent button { width: 100%; text-align: left; padding: 10px 14px; display: flex; flex-direction: column; gap: 2px; background: var(--panel); }
  .wr .voice-row { display: flex; align-items: center; gap: 12px; }
  .wr .voice-row .go { flex: none; padding: 0 18px; }
  .wr .voice-row .go.on { background: var(--lens); border-color: var(--lens); color: var(--sheet); }
  .wr .meter { flex: 1; height: 6px; border-radius: 1px; background: var(--well); border: 1px solid var(--hair); overflow: hidden; }
  .wr .meter > div { height: 100%; width: 0; background: var(--lens); transition: width 80ms linear; }
  .wr .heard { margin: 12px 0 0; font: 400 24px/1.2 var(--serif); }
  .wr .heard:empty { display: none; }
  .wr .hf { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 48px;
    font-size: 15px; font-weight: 500; margin-bottom: 8px; }
  .wr input[type=checkbox] { flex: none; width: 22px; height: 22px; margin: 0; accent-color: var(--lens); cursor: pointer; }
  .wr .feed { list-style: none; margin: 12px 0 0; padding: 12px 0 0; border-top: 1px solid var(--hair);
    display: flex; flex-direction: column; gap: 8px; max-height: 320px; overflow-y: auto; }
  .wr .feed:empty { display: none; }
  .wr .feed li { font-size: 14px; color: var(--muted); line-height: 1.45; }
  .wr .feed li.cmd { color: var(--ink); font-weight: 500; }
  .wr .feed li.cmd::before { content: 'sent · '; font: 12px var(--mono); color: var(--lens); }
  .wr .feed li.open { color: var(--ink); word-break: break-all; }
  .wr .feed li.open::before { content: 'opened · '; font: 12px var(--mono); color: var(--muted); }
  .wr .feed li.pending { font-style: italic; }
  .wr .feed li.pending::after { content: ' …'; }
  .wr .feed-head { margin: 12px 0 0; }
  .wr .bm-toggle { margin-top: 8px; width: 100%; }
  .wr .bm-toggle[aria-pressed=true] { border-color: var(--lens); color: var(--lens); background: var(--lens-soft); }
  .wr .bookmarks li { display: flex; gap: 8px; align-items: stretch; }
  .wr .bookmarks li > button:first-child { flex: 1; min-width: 0; }
  .wr .bookmarks .remove { flex: none; width: 44px; padding: 0; align-items: center; justify-content: center;
    font-size: 20px; color: var(--muted); }
  .wr .recent small { color: var(--muted); font: 12px var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
  .wr .recent span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
  .wr [hidden] { display: none !important; }
  .wr .backend-panel { display: grid; gap: 12px; }
  .wr .backend-panel label { display: grid; gap: 6px; font-size: 13px; }
  .wr .backend-panel .check { display: flex; align-items: center; gap: 10px; }
  .wr .backend-panel input[type=password], .wr .backend-panel select { width: 100%; box-sizing: border-box;
    border: 1px solid var(--hair); border-radius: var(--r); min-height: 44px; padding: 8px 12px; background: var(--well);
    color: var(--ink); font: 14px var(--mono); }
  .wr .backend-panel input[type=password]:focus, .wr .backend-panel select:focus { outline: none; border-color: var(--lens); }
  .wr .backend-panel button, .wr .back-button { min-height: 44px; border: 1px solid var(--hair); border-radius: var(--r);
    padding: 10px 12px; background: transparent; color: var(--ink); cursor: pointer; font: 14px/1.2 var(--sans); }
  .wr .backend-panel button.go { background: var(--ink); color: var(--sheet); border-color: var(--ink); font-weight: 500; }
  .wr .backend-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 14px; }
  .wr .backend-panel summary { cursor: pointer; font-size: 13px; }
  .wr .compact .backend-panel { gap: 6px; padding: 12px; }
  .wr .back-button { width: 100%; margin-top: 8px; }
  .wr .picks { margin-top: 12px; max-height: 360px; overflow-y: auto; }
  @media (prefers-reduced-motion: reduce) { .wr *, .wr *::before, .wr *::after { animation: none !important; transition: none !important; } }
`

export function mountCompanion(root: HTMLElement, opts: CompanionOptions): Companion {
  const style = document.createElement('style')
  style.textContent = STYLE
  document.head.appendChild(style)

  root.style.display = 'block'
  root.innerHTML = `
    <main class="wr">
      <header>
        <h1>Web Reader</h1>
        <p class="sub">Condense any web page into text pages for your G2.</p>
      </header>
      <form id="f">
        <div>
          <label class="lbl" for="url">Page URL</label>
          <div class="url-field">
            <input id="url" type="text" inputmode="url" autocapitalize="off" spellcheck="false" autocomplete="url" placeholder="https://example.com/article" required />
            <button class="clear" id="clear" type="button" aria-label="Clear URL" hidden>×</button>
          </div>
        </div>
        <button class="go" id="go" type="submit">Send to glasses</button>
        <p class="msg" id="msg" role="status"></p>
      </form>
      <section>
        <label class="lbl">On your glasses</label>
        <div class="card">
          <div class="mirror-head"><strong id="title">Nothing loaded</strong><span id="count"></span></div>
          <div class="glass" id="glass" role="img" aria-label="Glasses display preview"><div class="glass-inner" id="mirror"></div></div>
          <div class="pager">
            <button type="button" id="prev">Previous</button>
            <button type="button" id="sel" class="sel">Select</button>
            <button type="button" id="next">Next</button>
          </div>
          <ul class="recent picks" id="picks" aria-label="Glasses list" hidden></ul>
          <div class="pager">
            <button type="button" id="menu">Menu</button>
            <button type="button" id="back-control" style="grid-column: span 2">Back / cancel</button>
          </div>
          <button type="button" id="bm" class="back-button bm-toggle" aria-pressed="false" hidden>☆ Bookmark this page</button>
        </div>
      </section>
      <section>
        <label class="lbl">Voice search</label>
        <div class="card">
          <label class="hf"><span id="hfLabel">Hands-free</span><input type="checkbox" id="hf" /></label>
          <div class="voice-row">
            <button class="go" id="mic" type="button">Start listening</button>
            <div class="meter" aria-hidden="true"><div id="meter"></div></div>
          </div>
          <p class="msg" id="vmsg" role="status" style="margin-top:12px">Uses the glasses microphone.</p>
          <p class="heard" id="heard"></p>
          <p class="feed-head" id="feedHead" hidden>Always listening · what it hears</p>
          <ul class="feed" id="feed" aria-label="Everything heard" aria-live="polite"></ul>
        </div>
      </section>
      <section>
        <div class="card">
          <label class="hf" style="margin-bottom:0"><span>Focal can help: suggest next steps after each page</span><input type="checkbox" id="recs" /></label>
        </div>
      </section>
      <section id="backend"></section>
      <section id="bookmarksWrap" hidden>
        <label class="lbl">Bookmarks</label>
        <ul class="recent bookmarks" id="bookmarks"></ul>
      </section>
      <section id="recentWrap" hidden>
        <label class="lbl">Recent</label>
        <ul class="recent" id="recent"></ul>
      </section>
      <aside id="recommendations" hidden></aside>
    </main>
  `

  const $ = <T extends HTMLElement>(id: string) => root.querySelector<T>(`#${id}`)!
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
  $('prev').addEventListener('click', () => opts.onStep(-1))
  $('next').addEventListener('click', () => opts.onStep(1))
  $('sel').addEventListener('click', () => opts.onSelect())
  $('back-control').addEventListener('click', () => opts.onBack())
  $('menu').addEventListener('click', () => opts.onMenu())
  const picks = $<HTMLUListElement>('picks')
  picks.addEventListener('click', e => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-pick]')
    if (btn) opts.onPick(Number(btn.dataset.pick))
  })
  $('mic').addEventListener('click', () => opts.onVoice())
  const hf = $<HTMLInputElement>('hf')
  hf.addEventListener('change', () => opts.onHandsFree(hf.checked))
  const recs = $<HTMLInputElement>('recs')
  recs.addEventListener('change', () => opts.onRecommendations(recs.checked))
  const meter = $<HTMLDivElement>('meter')
  const vmsg = $<HTMLParagraphElement>('vmsg')
  let listenNote = ''
  const backendPanel = mountBackendPanel($('backend'), opts.backend)
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

  const glass = $<HTMLDivElement>('glass')
  const mirror = $<HTMLDivElement>('mirror')
  const fitMirror = () => {
    mirror.style.transform = `scale(${glass.clientWidth / 576})`
  }
  new ResizeObserver(fitMirror).observe(glass)

  const BORDER: Record<number, string> = { 13: '#5CFF5C', 5: '#2A7A2A' }
  function drawMirror(spec: PageSpec) {
    mirror.replaceChildren()
    for (const t of spec.texts) {
      const el = document.createElement('div')
      const bw = t.border ?? 0
      Object.assign(el.style, {
        left: `${t.x}px`, top: `${t.y}px`, width: `${t.w}px`, height: `${t.h}px`,
        padding: `${t.pad ?? 4}px`, borderWidth: `${bw}px`,
        borderColor: BORDER[t.color ?? 13] ?? '#3f9f3f', borderRadius: `${t.radius ?? 0}px`,
        ...(t.scroll ? { overflowY: 'auto' } : {}),
      })
      el.textContent = t.text
      mirror.append(el)
    }
    for (const im of spec.images) {
      const img = document.createElement('img')
      img.src = `data:image/png;base64,${im.image.png}`
      img.alt = ''
      Object.assign(img.style, { left: `${im.x}px`, top: `${im.y}px`, width: `${im.image.w}px`, height: `${im.image.h}px` })
      mirror.append(img)
    }
    glass.setAttribute('aria-label', spec.texts.map(t => t.text.trim()).filter(Boolean).join(' / ') || 'Glasses display preview')
  }

  $('bm').addEventListener('click', () => opts.onBookmark())
  const bookmarkList = $<HTMLUListElement>('bookmarks')
  bookmarkList.addEventListener('click', e => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-url]')
    if (!btn) return
    if (btn.dataset.remove) opts.onRemoveBookmark(btn.dataset.url!)
    else opts.onOpenBookmark(btn.dataset.url!)
  })

  const recentList = $<HTMLUListElement>('recent')
  recentList.addEventListener('click', e => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-url]')
    if (!btn) return
    input.value = btn.dataset.url!
    syncClear()
    opts.onLoad(btn.dataset.url!)
  })

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
      $('title').textContent = s.doc?.title ?? 'Nothing loaded'
      $('count').textContent = s.doc && !s.status ? s.position : ''
      drawMirror(s.spec)
      picks.hidden = !s.list
      picks.replaceChildren(
        ...(s.list ?? []).map((label, i) => {
          const li = document.createElement('li')
          const btn = document.createElement('button')
          btn.type = 'button'
          btn.dataset.pick = String(i)
          btn.textContent = label
          li.append(btn)
          return li
        }),
      )
      fitMirror()
      if (s.doc && !s.status) {
        const kind = s.kind ? `${KIND_LABEL[s.kind]} layout` : 'Manga'
        const note = s.doc.truncated ? ' Long page; only the first part was used.' : ''
        msg.classList.remove('err')
        msg.textContent = `${s.doc.site} · ${kind} · ${s.doc.wordCount.toLocaleString()} words.${note}`
      }
      const v = s.voice
      const mic = $<HTMLButtonElement>('mic')
      mic.textContent = v.phase === 'listening' ? 'Stop & send' : 'Start listening'
      mic.classList.toggle('on', v.phase === 'listening')
      mic.disabled = v.phase === 'sending' || v.phase === 'searching'
      hf.checked = s.handsFree
      recs.checked = s.recommendations
      $('hfLabel').textContent = `Hands-free: say "${s.wakePhrase}", then your question`
      if (v.phase !== 'listening' && !s.listening) meter.style.width = '0'
      listenNote = (v.phase === 'listening' || v.phase === 'idle') && v.note ? ` · ${v.note}` : ''
      vmsg.classList.toggle('err', v.phase === 'error' || !!listenNote)
      vmsg.textContent = {
        idle: s.listening ? `Waiting for "${s.wakePhrase}"...` : 'Uses the glasses microphone.',
        listening: `Listening...${listenNote}`,
        sending: 'Sending to the server...',
        searching: 'Searching the web for:',
        done: !v.text ? "Server couldn't make that out." : v.url ? `Opening ${v.url}` : 'Server heard:',
        error: v.text,
      }[v.phase]
      $('heard').textContent = v.phase !== 'error' && v.phase !== 'idle' && v.phase !== 'sending' && v.text ? `"${v.text}"` : ''
      $('feedHead').hidden = !s.handsFree
      const feed = $<HTMLUListElement>('feed')
      feed.replaceChildren(
        ...s.heard.map(h => {
          const li = document.createElement('li')
          li.textContent = h.text
          if (h.command) li.className = 'cmd'
          else if (h.opened) li.className = 'open'
          else if (h.pending) li.className = 'pending'
          return li
        }),
      )
      const bm = $<HTMLButtonElement>('bm')
      bm.hidden = !s.doc || !!s.status
      bm.setAttribute('aria-pressed', String(s.bookmarked))
      bm.textContent = s.bookmarked ? '★ Bookmarked' : '☆ Bookmark this page'
      $('bookmarksWrap').hidden = !s.bookmarks.length
      bookmarkList.replaceChildren(
        ...s.bookmarks.map(b => {
          const li = document.createElement('li')
          const open = document.createElement('button')
          open.type = 'button'
          open.dataset.url = b.url
          const t = document.createElement('span')
          t.textContent = b.title
          const u = document.createElement('small')
          u.textContent = b.position && b.where ? `${b.where} · ${b.url}` : b.url
          open.append(t, u)
          const remove = document.createElement('button')
          remove.type = 'button'
          remove.className = 'remove'
          remove.dataset.url = b.url
          remove.dataset.remove = '1'
          remove.setAttribute('aria-label', `Remove bookmark: ${b.title}`)
          remove.textContent = '×'
          li.append(open, remove)
          return li
        }),
      )
      $('recentWrap').hidden = !s.recent.length
      recentList.innerHTML = ''
      for (const r of s.recent) {
        const li = document.createElement('li')
        const btn = document.createElement('button')
        btn.type = 'button'
        btn.dataset.url = r.url
        const t = document.createElement('span')
        t.textContent = r.title
        const u = document.createElement('small')
        u.textContent = r.url
        btn.append(t, u)
        li.append(btn)
        recentList.append(li)
      }
    },
    setBusy(busy) {
      go.disabled = busy
      go.textContent = busy ? 'Condensing...' : 'Send to glasses'
      if (busy) {
        msg.classList.remove('err')
        msg.textContent = ''
      }
    },
    setError(message) {
      msg.classList.add('err')
      msg.textContent = message
    },
    voiceLevel(level, ms) {
      meter.style.width = `${Math.round(level * 100)}%`
      if (ms !== undefined) vmsg.textContent = `Listening... ${(ms / 1000).toFixed(1)}s${listenNote}`
    },
  }
}
