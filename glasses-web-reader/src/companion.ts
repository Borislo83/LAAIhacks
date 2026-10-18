import type { CondensedPage, SiteKind } from '../shared/types'
import type { PageSpec } from './screens'

// Phone-side UI inside the Even app WebView: URL entry, recent
// pages, and a live mirror of what the glasses are showing.

interface CompanionState {
  doc: CondensedPage | null
  status: string | null
  recent: { url: string; title: string }[]
  /** Exactly what the glasses are showing, drawn as a scaled preview. */
  spec: PageSpec
  position: string
  kind?: SiteKind
  voice: VoiceUi
  handsFree: boolean
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
  onStep: (delta: 1 | -1) => void
  onSelect: () => void
  /** Start listening, or stop and send when already listening. */
  onVoice: () => void
  onHandsFree: (on: boolean) => void
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
  /** Live mic loudness (0..1), plus elapsed time while capturing a command. */
  voiceLevel(level: number, ms?: number): void
}

const STYLE = `
  :root {
    --text: #232323; --text-dim: #7B7B7B; --bg: #FFFFFF; --surface: #EEEEEE;
    --input: rgba(35,35,35,0.08); --accent: #FEF991; --on-accent: #232323; --danger: #C0392B;
  }
  @media (prefers-color-scheme: dark) {
    :root { --text: #FFFFFF; --text-dim: #8A8A8A; --bg: #111111; --surface: #1A1A1A;
      --input: rgba(255,255,255,0.08); --danger: #FF7B6B; }
  }
  html, body { background: var(--bg) !important; color: var(--text) !important; }
  .wr { box-sizing: border-box; width: 100%; max-width: 640px; margin: 0 auto; padding: 20px;
    font: 16px/1.45 "FK Grotesk Neue", -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    letter-spacing: -0.01em; display: flex; flex-direction: column; gap: 24px; }
  .wr h1 { font-size: 24px; font-weight: 600; letter-spacing: -0.02em; margin: 0; }
  .wr .sub { color: var(--text-dim); font-size: 13px; margin: 4px 0 0; }
  .wr label.lbl { display: block; font-size: 11px; font-weight: 500; letter-spacing: 0.04em;
    text-transform: uppercase; color: var(--text-dim); margin-bottom: 8px; }
  .wr form { display: flex; flex-direction: column; gap: 12px; }
  .wr input[type=url] { width: 100%; box-sizing: border-box; border: 0; border-radius: 12px;
    background: var(--input); color: var(--text); font: inherit; padding: 14px 16px; min-height: 48px; }
  .wr input[type=url]:focus { outline: 2px solid var(--text-dim); }
  .wr .go { border: 0; border-radius: 12px; background: var(--accent); color: var(--on-accent); font: inherit;
    font-weight: 600; min-height: 48px; cursor: pointer; }
  .wr .go:disabled { opacity: 0.6; cursor: progress; }
  .wr .msg { font-size: 13px; color: var(--text-dim); min-height: 18px; margin: 0; }
  .wr .msg.err { color: var(--danger); }
  .wr .card { background: var(--surface); border-radius: 16px; padding: 16px; }
  .wr .mirror-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 12px; }
  .wr .mirror-head strong { font-size: 14px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wr .mirror-head span { font-size: 13px; color: var(--text-dim); flex: none; }
  .wr .glass { position: relative; width: 100%; aspect-ratio: 2 / 1; background: #000; border-radius: 10px; overflow: hidden; }
  .wr .glass-inner { position: absolute; left: 0; top: 0; width: 576px; height: 288px; transform-origin: 0 0;
    font: 20px/27px 'FK Grotesk Neue', -apple-system, system-ui, sans-serif; color: #5CFF5C; letter-spacing: 0; }
  .wr .glass-inner > div { position: absolute; box-sizing: border-box; white-space: pre-wrap; overflow: hidden; border-style: solid; }
  .wr .glass-inner > img { position: absolute; image-rendering: pixelated; mix-blend-mode: screen;
    filter: sepia(1) saturate(6) hue-rotate(62deg) brightness(1.05); }
  .wr .pager { display: grid; grid-template-columns: 1fr 1.2fr 1fr; gap: 8px; margin-top: 12px; }
  .wr .pager .sel { background: var(--accent); color: var(--on-accent); font-weight: 600; }
  .wr .pager button, .wr .recent button { border: 0; border-radius: 10px; background: var(--input); color: var(--text);
    font: inherit; font-size: 14px; min-height: 44px; cursor: pointer; }
  .wr .recent { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  .wr .recent button { width: 100%; text-align: left; padding: 10px 14px; display: flex; flex-direction: column; gap: 2px; }
  .wr .voice-row { display: flex; align-items: center; gap: 12px; }
  .wr .voice-row .go { flex: none; padding: 0 18px; }
  .wr .voice-row .go.on { background: var(--danger); color: #fff; }
  .wr .meter { flex: 1; height: 8px; border-radius: 4px; background: var(--input); overflow: hidden; }
  .wr .meter > div { height: 100%; width: 0; background: #5CFF5C; transition: width 80ms linear; }
  .wr .heard { margin: 12px 0 0; font-size: 18px; font-weight: 500; }
  .wr .heard:empty { display: none; }
  .wr .hf { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 48px;
    font-size: 16px; font-weight: 500; margin-bottom: 8px; }
  .wr .hf input { width: 24px; height: 24px; accent-color: var(--text); }
  .wr .feed { list-style: none; margin: 12px 0 0; padding: 12px 0 0; border-top: 1px solid var(--input);
    display: flex; flex-direction: column; gap: 8px; max-height: 320px; overflow-y: auto; }
  .wr .feed:empty { display: none; }
  .wr .feed li { font-size: 14px; color: var(--text-dim); line-height: 1.4; }
  .wr .feed li.cmd { color: var(--text); font-weight: 600; }
  .wr .feed li.cmd::before { content: 'Sent · '; font-weight: 500; color: var(--text-dim); }
  .wr .feed li.open { color: var(--text); word-break: break-all; }
  .wr .feed li.pending { font-style: italic; }
  .wr .feed li.pending::after { content: ' …'; }
  .wr .feed-head { margin: 12px 0 0; font-size: 11px; font-weight: 500; letter-spacing: 0.04em;
    text-transform: uppercase; color: var(--text-dim); }
  .wr .feed-head[hidden] { display: none; }
  .wr .feed li.open::before { content: 'Opened · '; color: var(--text-dim); }
  .wr .recent small { color: var(--text-dim); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
  .wr .recent span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
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
          <input id="url" type="url" inputmode="url" autocomplete="url" placeholder="https://example.com/article" required />
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
      <section id="recentWrap" hidden>
        <label class="lbl">Recent</label>
        <ul class="recent" id="recent"></ul>
      </section>
    </main>
  `

  const $ = <T extends HTMLElement>(id: string) => root.querySelector<T>(`#${id}`)!
  const input = $<HTMLInputElement>('url')
  const go = $<HTMLButtonElement>('go')
  const msg = $<HTMLParagraphElement>('msg')

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
  $('mic').addEventListener('click', () => opts.onVoice())
  const hf = $<HTMLInputElement>('hf')
  hf.addEventListener('change', () => opts.onHandsFree(hf.checked))
  const meter = $<HTMLDivElement>('meter')
  const vmsg = $<HTMLParagraphElement>('vmsg')
  let listenNote = ''

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

  const recentList = $<HTMLUListElement>('recent')
  recentList.addEventListener('click', e => {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-url]')
    if (!btn) return
    input.value = btn.dataset.url!
    opts.onLoad(btn.dataset.url!)
  })

  return {
    update() {
      const s = opts.getState()
      $('title').textContent = s.doc?.title ?? 'Nothing loaded'
      $('count').textContent = s.doc && !s.status ? s.position : ''
      drawMirror(s.spec)
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
