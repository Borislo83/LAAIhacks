import {
  waitForEvenAppBridge,
  TextContainerProperty,
  ListContainerProperty,
  ListItemContainerProperty,
  ImageContainerProperty,
  CreateStartUpPageContainer,
  RebuildPageContainer,
  TextContainerUpgrade,
  ImageRawDataUpdate,
  OsEventTypeList,
  AudioInputSource,
} from '@evenrealities/even_hub_sdk'
import { pxTruncate } from '@evenrealities/pretext'
import type { CondensedPage, MangaStrip, Screen } from '../shared/types'
import { condense, fetchStrip, report, searchPage, sendVoice, type MangaStyle } from './api'
import type { VoiceCommand } from '../shared/types'
import {
  FLOW_LINES, FLOW_STEP, flowLines, flowSpec, isFlowable, progressBar, renderScreen, screenTitle,
  type PageSpec, type TextBox,
} from './screens'
import { mountCompanion, type Companion, type HeardLine, type VoiceUi } from './companion'
import { clockFrame, loadingSpec, spinnerFrame, SPIN_MS } from './loading'
import { VoiceRecorder, WakeListener, concat } from './voice'
import { BackendClient } from './lensos/client'
import { BackendSession, type ActionGroup } from './lensos/session'
import { apiActionCards } from './lensos/action-cards'
import { glassesRecommendationSpec, recommendationCursor, RECOMMENDATION_LAYOUTS, type RecommendationLayout } from './glasses-recommendations'
import type { RecommendationBatch } from '../shared/recommendations'

// ── Layout (576x288) ────────────────────────────────────────────────────────
// Status:  body 0..252 (9 lines at 27px)  +  footer 252..288 (1 line)
// Menus:   header 0..36                   +  native list 36..288
// Smart:   per-layout containers from screens.ts over an invisible event layer
const BODY_H = 252
const BODY_PAD = 4
const INNER_W = 576 - 2 * BODY_PAD
const FOOTER_PAD = 4
const LIST_ITEM_PX = 520
const MAX_LIST_ITEMS = 20
const IMAGE_TIMEOUT_MS = 10_000

const STATE_KEY = 'webreader.v2.state'
const RECENT_KEY = 'webreader.v1.recent'
const BOOKMARKS_KEY = 'webreader.v1.bookmarks'
const MANGA_STYLE_KEY = 'webreader.v1.mangaStyle'
// Start with the chosen bottom strip, replacing preferences from the layout trial.
const RECOMMENDATION_LAYOUT_KEY = 'webreader.v2.recommendationLayout'
const SESSION_KEY = 'webreader.lensos.session'
const HANDS_FREE_KEY = 'webreader.v1.handsFree'
const RECOMMENDATIONS_KEY = 'webreader.v1.recommendations'
/** Must match the server's WAKE_NAME. Not "Hey Even": that wakes the system assistant. */
const WAKE_PHRASE = 'Hey Search'
/** The assistant's name on the glasses card and spinner. */
const ASSISTANT = 'Search'
const MAX_RECENT = 15
const MAX_BOOKMARKS = 50
const MAX_HISTORY = 10

type View =
  | 'reader' | 'deck' | 'manga' | 'menu' | 'contents' | 'links' | 'recent' | 'bookmarks'
  | 'actions' | 'recommendations' | 'recommendation-layout' | 'voice'
interface Recent {
  url: string
  title: string
}
/** A saved page and the place in it; the place follows along as the page is read. */
interface Bookmark {
  url: string
  title: string
  position: number
  /** The place in words: "Screen 3", "Page 12". */
  where?: string
}
interface MenuItem {
  label: string
  run: () => void
}
interface HistoryEntry {
  url: string
  title: string
  position: number
}

const WELCOME = `Web Reader

Type or paste a URL in the Even app on your phone. Each site gets screens built for it: headlines, product lists, shortcut cards and more.

Swipe: next / previous
Tap: open item or menu
Say "${WAKE_PHRASE}", then your question
Double-tap: exit`

// ── State ───────────────────────────────────────────────────────────────────
let view: View = 'reader'
let onScreen: 'reader' | 'manga' | 'loading' | 'assistant' | 'other' = 'reader'
/** Set while a page is opening; shown as the spinner screen. */
let loading: { title: string; host: string; step: string; frame: number } | null = null
let spinTimer: ReturnType<typeof setInterval> | undefined
let spinBusy = false
let doc: CondensedPage | null = null
let screens: Screen[] = []
let screenIdx = 0
/** First stream line shown while `screenIdx` is in a run of flowing text screens. */
let flowOffset = 0
/** Where the stream is rolling to; equals flowOffset when it's still. */
let flowTarget = 0
let flowRolling = false
/** How long one swipe's roll takes, however fast BLE is. */
const FLOW_ROLL_MS = 260
let rollFrom = 0
let rollStart = 0
/** More screens are still streaming in from the server. */
let deckLoading = false
let loadAbort: AbortController | null = null
const selected = new Map<number, number>()
// Manga: position within the chapter. `mangaStrips` is 0 while the current
// page's strip count is still loading.
let mangaPage = 0
let mangaStrip = 0
let mangaStrips = 0
/** Open the next chapter on its last page (swiping up past a chapter start). */
let mangaFromEnd = false
const strips = new Map<string, Promise<MangaStrip>>()
// What each of the 4 tile boxes currently holds on the glasses (the PNG it was
// sent), so a turn only re-sends tiles that changed: blank gutters, page ends
// and repeated panels cost nothing.
let shownTiles: (string | undefined)[] = []
let mangaStyle: MangaStyle = 'natural'
let status: string | null = null
let recent: Recent[] = []
let bookmarks: Bookmark[] = []
let history: HistoryEntry[] = []
let listItems: MenuItem[] = []
let loadSeq = 0
let renderSeq = 0
let mirror: PageSpec = { texts: [], images: [] }
/** Container ID of the shown deck screen's first text box (1 when it has no capture layer). */
let deckTextId = 2
let savedSession = ''
let savedSessionUrl = ''
/** Set while the glasses mic is capturing a voice command. */
let recorder: VoiceRecorder | null = null
/** Set while hands-free is waiting for the wake phrase (and no command is being captured). */
let wake: WakeListener | null = null
let handsFree = true
let micOn = false
let voiceSeq = 0
let voiceUi: VoiceUi = { phase: 'idle', text: '' }
/** The capture in progress came from the wake phrase: the glasses show only the words. */
let wakeCapture = false
/** Everything heard while hands-free, newest first (the phone shows it all). */
let heard: HeardLine[] = []
/** Each ambient utterance's wake check, how much of it that check covered, and its line on the phone. */
const wakeChecks = new WeakMap<
  Uint8Array[],
  { result: Promise<VoiceCommand | null>; bytes: number; line?: HeardLine }
>()
let glassRecommendations: RecommendationBatch | undefined
let recommendationsOpen = false
/** Whether Claude suggests next steps after each page loads. */
let recommendationsOn = true
let recommendationLayout: RecommendationLayout = 'bottom'
let recommendationIndex = 0
let recommendationSelected = false
let recommendationBase: PageSpec | undefined
let actionGroup: ActionGroup = 'tasks'
const actionCards = new Map<ActionGroup, ReturnType<typeof apiActionCards>>()
const backend = new BackendSession({
  saveSession(id, pageUrl) {
    savedSessionUrl = pageUrl ?? (id === savedSession ? savedSessionUrl : '')
    savedSession = id
    const value = JSON.stringify({ id, url: savedSessionUrl })
    void enqueue(() => bridge.setLocalStorage(SESSION_KEY, value)).catch(console.error)
  },
  changed() {
    for (const [group, cards] of actionCards) {
      cards.show(backend.actionDisplay(group))
      cards.setEnabled(backend.state.connected && backend.state.online)
    }
    companion.update()
  },
})

const bridge = await waitForEvenAppBridge()

// Every bridge call goes through one queue: concurrent render + storage calls
// can drop the BLE link, and a single flaky hop can hang for ~30s.
let chain: Promise<unknown> = Promise.resolve()
function enqueue<T>(fn: () => Promise<T>, timeoutMs = 6000): Promise<T> {
  const run = chain.then(() =>
    Promise.race([
      fn(),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('bridge timeout')), timeoutMs)),
    ]),
  )
  chain = run.catch(err => console.error('[bridge]', err))
  return run
}

const isManga = () => !!doc?.manga
const isDeck = () => !isManga() && screens.length > 0
const position = () => (isManga() ? mangaPage : screenIdx)
const isContentView = () => view === 'reader' || view === 'deck' || view === 'manga'

// ── Containers ──────────────────────────────────────────────────────────────
function readerContainers(body: string, footer: string) {
  return [
    new TextContainerProperty({
      xPosition: 0, yPosition: 0, width: 576, height: BODY_H,
      borderWidth: 0, borderColor: 5, paddingLength: BODY_PAD,
      containerID: 1, containerName: 'body',
      content: body || ' ',
      isEventCapture: 1,
    }),
    new TextContainerProperty({
      xPosition: 0, yPosition: BODY_H, width: 576, height: 288 - BODY_H,
      borderWidth: 0, borderColor: 5, paddingLength: FOOTER_PAD,
      containerID: 2, containerName: 'footer',
      content: footer || ' ',
      isEventCapture: 0,
    }),
  ]
}

const readerSpec = (body: string, footer: string): PageSpec => ({
  texts: [
    { x: 0, y: 0, w: 576, h: BODY_H, text: body, pad: BODY_PAD },
    { x: 0, y: BODY_H, w: 576, h: 288 - BODY_H, text: footer, pad: FOOTER_PAD },
  ],
  images: [],
})

function bodyText() {
  return status ?? (doc ? ' ' : WELCOME)
}

function footerText() {
  const mic = wake ? '◉ ' : ''
  if (doc) return pxTruncate(mic + doc.title, INNER_W)
  return mic + (status ? 'Web Reader' : 'Web Reader · waiting for a URL')
}

const created = await bridge.createStartUpPageContainer(
  new CreateStartUpPageContainer({ containerTotalNum: 2, textObject: readerContainers(WELCOME, footerText()) }),
)
if (created !== 0) console.error('createStartUpPageContainer failed:', created)

// ── Rendering ───────────────────────────────────────────────────────────────
/** Shows the current page in whichever form it has: status text, deck or manga. */
function showContent() {
  if (loading && !status) return renderLoading()
  if (glassRecommendations && recommendationsOpen && !status) return renderGlassesRecommendations()
  if (!status && doc && isManga()) return showStrip(mangaPage, mangaStrip)
  if (!status && doc && isDeck()) return renderDeck()
  return renderReader()
}

async function renderReader() {
  view = 'reader'
  await showText(bodyText(), footerText())
}

/** Body + footer text on the reader layout, shared by the reader and voice screens. */
async function showText(body: string, footer: string) {
  renderSeq++
  mirror = readerSpec(body, footer)
  companion.update()
  if (onScreen !== 'reader') {
    onScreen = 'reader'
    await enqueue(() =>
      bridge.rebuildPageContainer(
        new RebuildPageContainer({ containerTotalNum: 2, textObject: readerContainers(body, footer) }),
      ),
    )
    return
  }
  // In-place upgrades are flicker-free; the page doesn't redraw.
  await enqueue(() =>
    bridge.textContainerUpgrade(new TextContainerUpgrade({ containerID: 1, containerName: 'body', content: body })),
  )
  await enqueue(() =>
    bridge.textContainerUpgrade(new TextContainerUpgrade({ containerID: 2, containerName: 'footer', content: footer })),
  )
}

// Loading screen: capture layer (1), title (2), step (3), spinner image (4).
// Built once, then the spinner frame and the two text lines are swapped in place.
const SPINNER_ID = 4
let spinTimes: number[] = []

async function renderLoading() {
  if (!loading) return
  view = 'reader'
  onScreen = 'loading'
  renderSeq++
  const spec = loadingSpec(loading.title, loading.host, loading.step, loading.frame)
  mirror = spec
  companion.update()
  const capture = new TextContainerProperty({
    xPosition: 0, yPosition: 0, width: 576, height: 288,
    borderWidth: 0, borderColor: 0, paddingLength: 0,
    containerID: 1, containerName: 'capture',
    content: ' ',
    isEventCapture: 1,
  })
  const texts = [capture, ...spec.texts.map((t, i) => textContainer(t, i + 2))]
  const [im] = spec.images
  const image = new ImageContainerProperty({
    xPosition: im.x, yPosition: im.y, width: im.image.w, height: im.image.h,
    containerID: SPINNER_ID, containerName: `i${SPINNER_ID}`,
  })
  clearInterval(spinTimer)
  spinBusy = false
  spinTimes = []
  await enqueue(() =>
    bridge.rebuildPageContainer(
      new RebuildPageContainer({ containerTotalNum: texts.length + 1, textObject: texts, imageObject: [image] }),
    ),
  )
  spinTimer = setInterval(spin, SPIN_MS)
  spin()
}

function spin() {
  if (!loading || onScreen !== 'loading') return void clearInterval(spinTimer)
  // Skip a frame rather than queue up behind a slow BLE hop.
  if (spinBusy) return
  spinBusy = true
  // Frame from the clock, so the ring turns at the same speed however slow
  // the link is; a slow link just jumps further each frame.
  const frame = clockFrame()
  if (frame === loading.frame) {
    spinBusy = false
    return
  }
  loading.frame = frame
  const image = spinnerFrame(loading.frame)
  mirror.images = [{ ...mirror.images[0], image }]
  companion.update()
  const t0 = performance.now()
  enqueue(() =>
    bridge.updateImageRawData(
      new ImageRawDataUpdate({ containerID: SPINNER_ID, containerName: `i${SPINNER_ID}`, imageData: base64ToBytes(image.png) }),
    ),
  )
    .then(() => {
      // Real-device frame time, printed by the dev server as [telemetry].
      spinTimes.push(Math.round(performance.now() - t0))
      if (spinTimes.length === 20) {
        const sorted = [...spinTimes].sort((a, b) => a - b)
        report({ ev: 'spinner', medianMs: sorted[10], maxMs: sorted[19] })
        spinTimes = []
      }
    })
    .catch(() => {})
    .finally(() => (spinBusy = false))
}

/** Updates the title and step lines in place as the server reports progress. */
function loadingProgress(step: string, title?: string) {
  if (!loading) return
  loading.step = step
  if (title) loading.title = title
  if (onScreen !== 'loading') return
  const spec = loadingSpec(loading.title, loading.host, loading.step, loading.frame)
  mirror = spec
  companion.update()
  spec.texts.forEach((t, i) => {
    const id = i + 2
    void enqueue(() => bridge.textContainerUpgrade(new TextContainerUpgrade({ containerID: id, containerName: `t${id}`, content: t.text })))
  })
}

function stopLoading() {
  loading = null
  clearInterval(spinTimer)
}

function textContainer(t: TextBox, id: number) {
  return new TextContainerProperty({
    xPosition: t.x, yPosition: t.y, width: t.w, height: t.h,
    borderWidth: t.border ?? 0, borderColor: t.color ?? 13, borderRadius: t.radius ?? 0,
    paddingLength: t.pad ?? 4,
    containerID: id, containerName: `t${id}`,
    content: t.text || ' ',
    isEventCapture: t.scroll ? 1 : 0,
  })
}

function base64ToBytes(b64: string) {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

function deckContext() {
  return {
    position: progressBar(screenIdx, screens.length),
    selected: selected.get(screenIdx) ?? 0,
    upcoming: screens.slice(screenIdx + 1).map(s => screenTitle(s)),
  }
}

/** The run of flowing text screens around `i`, as [start, end), or null for a slide. */
function flowRun(i: number): [number, number] | null {
  if (!screens[i] || !isFlowable(screens[i])) return null
  let a = i
  let b = i + 1
  while (a > 0 && isFlowable(screens[a - 1])) a--
  while (b < screens.length && isFlowable(screens[b])) b++
  return [a, b]
}

const flowMax = (lines: string[]) => Math.max(0, lines.length - FLOW_LINES)

/** Index within the run of the section at the top of the window. */
function flowTop(starts: number[], offset: number) {
  let k = 0
  while (k + 1 < starts.length && starts[k + 1] <= offset) k++
  return k
}

/** Points the deck at a screen; a flowing one opens at its section (or the run's end). */
function enterScreen(index: number, fromEnd = false) {
  screenIdx = index
  const run = flowRun(index)
  if (!run) return
  const { lines, starts } = flowLines(screens.slice(...run))
  flowOffset = flowTarget = fromEnd ? flowMax(lines) : Math.min(starts[index - run[0]], flowMax(lines))
  if (fromEnd) screenIdx = run[0] + flowTop(starts, flowOffset)
}

function currentSpec(): PageSpec {
  const run = flowRun(screenIdx)
  if (!run) return renderScreen(screens[screenIdx], deckContext())
  const { lines } = flowLines(screens.slice(...run))
  flowOffset = Math.min(flowOffset, flowMax(lines))
  if (!flowRolling) flowTarget = flowOffset
  // The hint names what comes after the stream, not sections already in view.
  return flowSpec(lines, flowOffset, { ...deckContext(), upcoming: screens.slice(run[1]).map(s => screenTitle(s)) })
}

/**
 * Swipe inside a run: move the stream FLOW_STEP lines, leaving the run at its
 * ends. A swipe during the roll extends it instead of leaving.
 */
function flowStep(delta: 1 | -1): boolean {
  const run = isDeck() && !status ? flowRun(screenIdx) : null
  if (!run) return false
  const { lines } = flowLines(screens.slice(...run))
  const next = Math.min(flowMax(lines), Math.max(0, flowTarget + delta * FLOW_STEP))
  if (next === flowTarget) {
    if (flowRolling) return true
    if (delta > 0) goToScreen(run[1])
    else goToScreen(run[0] - 1, true)
    return true
  }
  flowTarget = next
  scheduleSave()
  void flowRoll()
  return true
}

/**
 * Fakes a scroll animation with text upgrades (same two boxes, so no redraw).
 * Each roll is timed, not counted: it eases out over FLOW_ROLL_MS, and when
 * BLE is slower than one line per frame it skips lines to finish on time.
 * A swipe mid-roll restarts the clock from where the stream is now.
 */
async function flowRoll() {
  if (flowRolling) {
    rollFrom = flowOffset
    rollStart = performance.now()
    return
  }
  flowRolling = true
  rollFrom = flowOffset
  rollStart = performance.now()
  try {
    while (flowOffset !== flowTarget && view === 'deck' && !status) {
      const run = flowRun(screenIdx)
      if (!run) break
      const t = Math.min(1, (performance.now() - rollStart) / FLOW_ROLL_MS)
      const eased = 1 - (1 - t) ** 3
      const want = t >= 1 ? flowTarget : rollFrom + Math.round((flowTarget - rollFrom) * eased)
      if (want === flowOffset) {
        await new Promise(r => setTimeout(r, 16))
        continue
      }
      flowOffset = want
      const { starts } = flowLines(screens.slice(...run))
      screenIdx = run[0] + flowTop(starts, flowOffset)
      await refreshDeck()
    }
  } finally {
    flowTarget = flowOffset
    flowRolling = false
  }
}

async function renderDeck() {
  view = 'deck'
  return renderPageSpec(currentSpec())
}

async function renderPageSpec(spec: PageSpec) {
  onScreen = 'other'
  const seq = ++renderSeq
  mirror = spec
  companion.update()

  // A scrolling box takes the swipes itself. Otherwise an invisible
  // full-screen layer, declared first so it sits behind everything,
  // receives all taps and swipes for the page.
  const scrolls = spec.texts.some(t => t.scroll)
  const capture = new TextContainerProperty({
    xPosition: 0, yPosition: 0, width: 576, height: 288,
    borderWidth: 0, borderColor: 0, paddingLength: 0,
    containerID: 1, containerName: 'capture',
    content: ' ',
    isEventCapture: 1,
  })
  deckTextId = scrolls ? 1 : 2
  const texts = [...(scrolls ? [] : [capture]), ...spec.texts.map((t, i) => textContainer(t, i + deckTextId))]
  const firstImageId = texts.length + 1
  const images = spec.images.map(
    (im, i) =>
      new ImageContainerProperty({
        xPosition: im.x, yPosition: im.y, width: im.image.w, height: im.image.h,
        containerID: firstImageId + i, containerName: `i${firstImageId + i}`,
      }),
  )
  await enqueue(() =>
    bridge.rebuildPageContainer(
      new RebuildPageContainer({
        containerTotalNum: texts.length + images.length,
        textObject: texts,
        ...(images.length ? { imageObject: images } : {}),
      }),
    ),
  )

  // Text first, then images one at a time (each costs ~0.5-2s over BLE).
  // A newer render cancels the rest.
  for (const [i, im] of spec.images.entries()) {
    if (seq !== renderSeq) return
    await enqueue(
      () =>
        bridge.updateImageRawData(
          new ImageRawDataUpdate({
            containerID: firstImageId + i,
            containerName: `i${firstImageId + i}`,
            imageData: base64ToBytes(im.image.png),
          }),
        ),
      IMAGE_TIMEOUT_MS,
    ).catch(err => console.warn('[image]', err))
  }
}

/**
 * Redraws the shown screen's text in place after more screens stream in (the
 * position and "next" hint change). Upgrades only the boxes whose text changed,
 * so there's no flicker and no image re-send; skipped if the layout moved.
 */
async function refreshDeck() {
  if (view !== 'deck' || status) return
  const before = mirror
  const spec = currentSpec()
  const sameBoxes =
    spec.texts.length === before.texts.length &&
    spec.images.length === before.images.length &&
    spec.texts.every((t, i) => t.x === before.texts[i].x && t.y === before.texts[i].y && t.w === before.texts[i].w && t.h === before.texts[i].h)
  if (!sameBoxes) return
  mirror = spec
  companion.update()
  for (const [i, t] of spec.texts.entries()) {
    // Upgrading a scrolling box jumps it back to the top, so only changed text is sent.
    if (t.text === before.texts[i].text) continue
    const id = i + deckTextId
    await enqueue(() =>
      bridge.textContainerUpgrade(new TextContainerUpgrade({ containerID: id, containerName: `t${id}`, content: t.text || ' ' })),
    )
  }
}

/**
 * Shop/feed lists are ordinary swipe screens: each swipe moves the highlight
 * one item. Swiping past the last item opens the next screen; past the first
 * goes back. Native firmware lists can't do this — they swallow swipes and
 * can't show product photos — so they are only used for menus.
 */
function stepList(delta: 1 | -1): boolean {
  const s = screens[screenIdx]
  if (!isDeck() || status || s?.layout !== 'list') return false
  const sel = selected.get(screenIdx) ?? 0
  const next = sel + delta
  if (next < 0) {
    goToScreen(screenIdx - 1, true)
    return true
  }
  if (next >= s.items.length) {
    goToScreen(screenIdx + 1)
    return true
  }
  selected.set(screenIdx, next)
  scheduleSave()
  void renderDeck()
  return true
}

// ── Manga ───────────────────────────────────────────────────────────────────
// A chapter is a run of full-screen strips: each page image scaled to the
// display width and cut top to bottom. Swipes move one strip; the 2x2 image
// tiles are updated in place so the page doesn't rebuild between strips.
const TILE_POS = [[0, 0], [288, 0], [0, 144], [288, 144]] as const

function getStrip(pageIndex: number, strip: number): Promise<MangaStrip> {
  const chapter = doc!.manga!.chapterId
  const style = mangaStyle
  const key = `${style}:${chapter}:${pageIndex}:${strip}`
  let hit = strips.get(key)
  if (!hit) {
    hit = fetchStrip(chapter, pageIndex, strip, style)
    strips.set(key, hit)
    hit.then(
      r => strips.set(`${style}:${chapter}:${pageIndex}:${r.strip}`, hit!),
      () => strips.delete(key),
    )
    if (strips.size > 40) strips.delete(strips.keys().next().value!)
  }
  return hit
}

/** `strip` -1 = the page's last strip. */
async function showStrip(pageIndex: number, strip: number) {
  const m = doc!.manga!
  view = 'manga'
  const seq = ++renderSeq
  const t0 = performance.now()
  const timing: { rebuild?: number; fetch?: number; tiles: (number | string)[] } = { tiles: [] }
  if (pageIndex !== mangaPage || mangaStrips === 0) mangaStrips = 0
  mangaPage = pageIndex
  mangaStrip = Math.max(0, strip)
  scheduleSave()

  if (onScreen !== 'manga') {
    onScreen = 'manga'
    const capture = new TextContainerProperty({
      xPosition: 0, yPosition: 0, width: 576, height: 288,
      borderWidth: 0, borderColor: 0, paddingLength: 0,
      containerID: 1, containerName: 'capture',
      content: ' ',
      isEventCapture: 1,
    })
    const images = TILE_POS.map(
      ([x, y], i) =>
        new ImageContainerProperty({
          xPosition: x, yPosition: y, width: 288, height: 144,
          containerID: i + 2, containerName: `m${i + 2}`,
        }),
    )
    shownTiles = []
    await enqueue(() =>
      bridge.rebuildPageContainer(
        new RebuildPageContainer({ containerTotalNum: 5, textObject: [capture], imageObject: images }),
      ),
    )
    timing.rebuild = Math.round(performance.now() - t0)
  }

  let r: MangaStrip
  try {
    const tf = performance.now()
    r = await getStrip(pageIndex, strip)
    timing.fetch = Math.round(performance.now() - tf)
  } catch (err) {
    if (seq !== renderSeq) return
    status = `Could not load page ${pageIndex + 1} of ${m.chapter}\n\n${err instanceof Error ? err.message : err}\n\nTap for menu.`
    return void renderReader()
  }
  if (seq !== renderSeq) return
  mangaStrip = r.strip
  mangaStrips = r.strips
  mirror = {
    texts: [],
    images: r.tiles.map((image, i) => ({ x: TILE_POS[i][0], y: TILE_POS[i][1], image })),
  }
  companion.update()

  // Read ahead while the tiles cross BLE.
  // Two screens ahead, crossing into the next page when this one ends.
  const ahead: [number, number][] = []
  for (let k = 1; k <= 2; k++) {
    const s = r.strip + k
    if (s < r.strips) ahead.push([pageIndex, s])
    else if (pageIndex + 1 < m.pages) ahead.push([pageIndex + 1, s - r.strips])
  }
  for (const [p, s] of ahead) void getStrip(p, s).catch(() => {})

  const sendTiles = async () => {
  for (const [i, tile] of r.tiles.entries()) {
    if (seq !== renderSeq) return
    if (shownTiles[i] === tile.png) {
      timing.tiles.push('skip')
      continue
    }
    const ts = performance.now()
    // Unknown until the send succeeds: a timeout may leave the old or new tile up.
    shownTiles[i] = undefined
    const ok = await enqueue(
      () =>
        bridge.updateImageRawData(
          new ImageRawDataUpdate({ containerID: i + 2, containerName: `m${i + 2}`, imageData: base64ToBytes(tile.png) }),
        ),
      IMAGE_TIMEOUT_MS,
    ).then(
      res => (String(res) === 'success' ? true : String(res)),
      err => (console.warn('[manga]', err), `error: ${err instanceof Error ? err.message : err}`),
    )
    timing.tiles.push(ok === true ? Math.round(performance.now() - ts) : `fail ${ok}`)
    if (ok === true) shownTiles[i] = tile.png
  }
  }
  await sendTiles()
  report({
    ev: 'strip', page: pageIndex, strip: r.strip, style: mangaStyle, cancelled: seq !== renderSeq,
    ...timing, total: Math.round(performance.now() - t0),
    pngKB: r.tiles.map(t => Math.round((t.png.length * 3) / 4 / 1024)),
  })
}

function mangaStep(delta: 1 | -1) {
  const m = doc!.manga!
  // Strip count of a newly opened page isn't known yet; wait for it.
  if (!mangaStrips) return
  if (delta > 0) {
    if (mangaStrip + 1 < mangaStrips) return void showStrip(mangaPage, mangaStrip + 1)
    if (mangaPage + 1 < m.pages) return void showStrip(mangaPage + 1, 0)
    if (m.next) void load(m.next)
    return
  }
  if (mangaStrip > 0) return void showStrip(mangaPage, mangaStrip - 1)
  if (mangaPage > 0) return void showStrip(mangaPage - 1, -1)
  if (m.prev) {
    mangaFromEnd = true
    void load(m.prev, Number.MAX_SAFE_INTEGER)
  }
}

function mangaMenuItems(): MenuItem[] {
  const m = doc!.manga!
  const items: MenuItem[] = []
  if (m.next) items.push({ label: 'Next chapter', run: () => void load(m.next!) })
  if (m.prev) items.push({ label: 'Previous chapter', run: () => void load(m.prev!) })
  if (m.pages > 1) items.push({ label: `Go to page (${m.pages})`, run: openPages })
  items.push({ label: 'All chapters', run: () => void load(m.seriesUrl, 0, true) })
  items.push(
    mangaStyle === 'natural'
      ? { label: 'Style: linework (less glare)', run: () => setMangaStyle('lines') }
      : { label: 'Style: natural tones', run: () => setMangaStyle('natural') },
  )
  return items
}

function setMangaStyle(style: MangaStyle) {
  mangaStyle = style
  scheduleSave()
  void showStrip(mangaPage, mangaStrip)
}

/** Native lists hold 20 rows, so long chapters offer evenly spaced pages. */
function openPages() {
  const m = doc!.manga!
  const count = Math.min(m.pages, MAX_LIST_ITEMS - 1)
  const targets = Array.from({ length: count }, (_, i) => Math.round((i * (m.pages - 1)) / Math.max(1, count - 1)))
  const items = [...new Set(targets)].map(p => ({
    label: `Page ${p + 1}${p === mangaPage ? '  (here)' : ''}`,
    run: () => void showStrip(p, 0),
  }))
  renderList('contents', `${m.chapter} · page ${mangaPage + 1}/${m.pages}`, [back, ...items]).catch(console.error)
}

async function renderList(next: View, title: string, items: MenuItem[]) {
  view = next
  onScreen = 'other'
  renderSeq++
  listItems = items.slice(0, MAX_LIST_ITEMS)
  mirror = {
    texts: [
      { x: 0, y: 0, w: 576, h: 36, text: title },
      { x: 0, y: 36, w: 576, h: 252, text: listItems.map(i => `  ${i.label}`).join('\n') },
    ],
    images: [],
  }
  companion.update()
  const header = new TextContainerProperty({
    xPosition: 0, yPosition: 0, width: 576, height: 36,
    borderWidth: 0, borderColor: 5, paddingLength: 4,
    containerID: 1, containerName: 'header',
    content: pxTruncate(title, INNER_W),
    isEventCapture: 0,
  })
  const list = new ListContainerProperty({
    xPosition: 0, yPosition: 36, width: 576, height: 252,
    borderWidth: 0, borderColor: 5, paddingLength: 4,
    containerID: 2, containerName: 'list',
    isEventCapture: 1,
    itemContainer: new ListItemContainerProperty({
      itemCount: listItems.length,
      itemWidth: 0,
      isItemSelectBorderEn: 1,
      itemName: listItems.map(i => pxTruncate(i.label.slice(0, 61), LIST_ITEM_PX)),
    }),
  })
  await enqueue(() =>
    bridge.rebuildPageContainer(
      new RebuildPageContainer({ containerTotalNum: 2, textObject: [header], listObject: [list] }),
    ),
  )
}

// ── Navigation ──────────────────────────────────────────────────────────────
function goToScreen(index: number, fromEnd = false) {
  if (!doc || status || index < 0 || index >= screens.length) return
  enterScreen(index, fromEnd)
  scheduleSave()
  renderDeck().catch(console.error)
}

function goTo(index: number) {
  if (isManga()) void showStrip(index, 0)
  else goToScreen(index)
}

/** Swipe: through list items, then onto the next screen once you pass the last one. */
function step(delta: 1 | -1) {
  if (view === 'recommendations') {
    recommendationIndex = recommendationCursor(recommendationIndex, delta)
    void renderGlassesRecommendations()
    return
  }
  if (view === 'actions') { actionCards.get(actionGroup)?.dispatch(delta > 0 ? 'down' : 'up'); return }
  if (loading && !status) return
  if (isManga() && !status) return mangaStep(delta)
  if (stepList(delta) || flowStep(delta)) return
  goToScreen(screenIdx + delta, delta < 0)
}

/** Tap: open the selected list item, otherwise the menu. */
function select() {
  if (view === 'recommendations') {
    if (recommendationIndex === 3) companion.closeRecommendations()
    else companion.selectRecommendation(recommendationIndex)
    return
  }
  if (view === 'actions') { actionCards.get(actionGroup)?.dispatch('tap'); return }
  const s = isDeck() && !status && !loading ? screens[screenIdx] : undefined
  if (s?.layout === 'list') {
    const url = s.items[selected.get(screenIdx) ?? 0]?.url
    if (url) void load(url, 0, true)
    else openMenu()
    return
  }
  openMenu()
}

const back: MenuItem = { label: '< Back', run: () => void showContent() }

function openMenu() {
  const items: MenuItem[] = []
  // While a page opens, the old page's screens are about to be replaced.
  const ready = doc && !status && !loading
  if (loading && !status) {
    items.push({ label: `Keep loading ${loading.host}`, run: () => void showContent() })
  } else if (ready) {
    if (glassRecommendations) items.push({ label: 'Recommendations (3)', run: () => { companion.reopenRecommendations() } })
    items.push({ label: 'Resume', run: () => void showContent() })
    if (recommendationsOn) {
      items.push({ label: `Recommendation layout: ${RECOMMENDATION_LAYOUTS.find(layout => layout.id === recommendationLayout)!.label}`,
        run: openRecommendationLayouts })
    }
    if (isManga()) items.push(...mangaMenuItems())
  } else {
    items.push({
      label: doc ? `Back to ${doc.title}` : 'Back',
      run: () => {
        status = null
        void showContent()
      },
    })
  }
  items.push({ label: 'Voice search', run: startVoice })
  items.push({
    label: `Recommendations: ${recommendationsOn ? 'on' : 'off'}`,
    run: () => {
      setRecommendations(!recommendationsOn)
      openMenu()
    },
  })
  items.push({
    label: handsFree ? `Hands-free "${WAKE_PHRASE}": on` : `Hands-free "${WAKE_PHRASE}": off`,
    run: () => {
      setHandsFree(!handsFree)
      openMenu()
    },
  })
  const prev = history[history.length - 1]
  if (prev) {
    items.push({
      label: `◀ ${prev.title}`,
      run: () => {
        history.pop()
        void load(prev.url, prev.position)
      },
    })
  }
  if (ready && doc && !isManga()) {
    if (screens.length > 1) items.push({ label: `Contents (${screens.length})`, run: openContents })
    if (doc.links.length) items.push({ label: `Links on page (${doc.links.length})`, run: openLinks })
    if (position() > 0) items.push({ label: 'Back to start', run: () => goTo(0) })
  }
  if (ready && doc) {
    items.push({
      label: bookmarkOf(doc.url) ? 'Remove bookmark' : 'Bookmark this page',
      run: () => {
        toggleBookmark()
        openMenu()
      },
    })
  }
  if (bookmarks.length) items.push({ label: `Bookmarks (${bookmarks.length})`, run: openBookmarks })
  if (ready && isManga() && position() > 0) items.push({ label: 'Back to first page', run: () => goTo(0) })
  if (recent.length) items.push({ label: `Recent pages (${recent.length})`, run: openRecent })
  if (backend.state.connected) {
    items.push({ label: `Saved suggestions (${backend.state.tasks.length})`, run: () => openActions('tasks') })
    items.push({ label: `Session actions (${backend.state.display?.actions.length ?? 0})`, run: () => openActions('session') })
    items.push({ label: 'Saved task history', run: () => openActions('history') })
  }
  items.push({ label: 'Exit', run: () => void bridge.shutDownPageContainer(1) })
  renderList('menu', doc ? `Menu · ${doc.site}` : 'Menu', items).catch(console.error)
}

function openContents() {
  if (!doc) return
  const items = screens.map((s, i) => ({ label: `${i + 1}. ${screenTitle(s)}`, run: () => goToScreen(i) }))
  renderList('contents', 'Contents', [back, ...items]).catch(console.error)
}

function openRecommendationLayouts() {
  const items: MenuItem[] = RECOMMENDATION_LAYOUTS.map(layout => ({
    label: `${layout.id === recommendationLayout ? '* ' : ''}${layout.label}`,
    run: () => {
      recommendationLayout = layout.id
      void enqueue(() => bridge.setLocalStorage(RECOMMENDATION_LAYOUT_KEY, layout.id)).catch(console.error)
      if (!companion.reopenRecommendations()) openMenu()
    },
  }))
  items.push({ label: '< Back to menu', run: openMenu })
  void renderList('recommendation-layout', 'Choose recommendation layout', items).catch(console.error)
}

function openLinks() {
  if (!doc) return
  const items = doc.links.map(l => ({ label: l.text, run: () => void load(l.url, 0, true) }))
  renderList('links', 'Links · tap to open', [back, ...items]).catch(console.error)
}

// ── Bookmarks ───────────────────────────────────────────────────────────────
const bookmarkOf = (url: string) => bookmarks.find(b => b.url === url)
const where = () => `${isManga() ? 'Page' : 'Screen'} ${position() + 1}`

function toggleBookmark() {
  if (!doc) return
  const { url, title } = doc
  bookmarks = bookmarkOf(url)
    ? bookmarks.filter(b => b.url !== url)
    : [{ url, title, position: position(), where: where() }, ...bookmarks].slice(0, MAX_BOOKMARKS)
  companion.update()
  void flushSave()
}

function removeBookmark(url: string) {
  bookmarks = bookmarks.filter(b => b.url !== url)
  companion.update()
  void flushSave()
  if (view === 'bookmarks') openBookmarks()
}

/** Reopens a bookmark where it was left. */
function openBookmark(url: string) {
  const b = bookmarkOf(url)
  if (b) void load(b.url, b.position, true)
}

function openBookmarks() {
  const items = bookmarks.map(b => ({
    label: b.position && b.where ? `${b.title} · ${b.where}` : b.title,
    run: () => openBookmark(b.url),
  }))
  renderList('bookmarks', 'Bookmarks', [back, ...items]).catch(console.error)
}

function openRecent() {
  const items = recent.map(r => ({ label: r.title, run: () => void load(r.url) }))
  renderList('recent', 'Recent pages', [back, ...items]).catch(console.error)
}

// ── Voice ───────────────────────────────────────────────────────────────────
// Hands-free (like "Hey Even"): the mic stays on and the start of each utterance
// is checked for the wake phrase. "Hey Search" pops up the card, then the next
// thing said is the question (two steps, like "Hey Even").
// Voice search in the menu does the same without the wake phrase.
// While capturing: tap sends early, swipe cancels, a pause ends it. The server
// transcribes the command and owns what happens next.
// Both ways in use the same card; voice search by hand also shows the gestures,
// since the user started it with a tap and may want to send early.
const VOICE_HINT = 'Tap: try again   Swipe: back'
const LISTEN_HINT = 'Listening...   Tap: send   Swipe: cancel'
/** How often the audio so far is re-sent for a live transcript. */
const PARTIAL_MS = 1200
/** A hands-free result stays on the glasses this long, then the page comes back. */
const WAKE_RESULT_MS = 6000
const MAX_HEARD = 40

// Voice card, like a system assistant popping up: a name line, a bordered
// card with the greeting or your words, and a status line. Built once, then the
// card and status are swapped in place as the words come in.
const GREETING = 'Hey! What can I find for you?'

function assistantSpec(card: string, status: string): PageSpec {
  return {
    texts: [
      { x: 32, y: 14, w: 512, h: 36, text: `◉ ${ASSISTANT}`, pad: 4 },
      { x: 32, y: 56, w: 512, h: 160, text: card, pad: 16, border: 2, color: 13, radius: 12 },
      { x: 32, y: 226, w: 512, h: 36, text: status, pad: 4 },
    ],
    images: [],
  }
}

async function renderAssistant(card: string, status: string) {
  view = 'voice'
  renderSeq++
  const spec = assistantSpec(card, status)
  mirror = spec
  companion.update()
  if (onScreen !== 'assistant') {
    onScreen = 'assistant'
    stopLoading()
    // A full-screen capture layer under the card takes the taps and swipes.
    const capture = new TextContainerProperty({
      xPosition: 0, yPosition: 0, width: 576, height: 288,
      borderWidth: 0, borderColor: 0, paddingLength: 0,
      containerID: 1, containerName: 'capture',
      content: ' ',
      isEventCapture: 1,
    })
    const texts = [capture, ...spec.texts.map((t, i) => textContainer(t, i + 2))]
    await enqueue(() =>
      bridge.rebuildPageContainer(new RebuildPageContainer({ containerTotalNum: texts.length, textObject: texts })),
    )
    return
  }
  for (const id of [3, 4]) {
    const content = spec.texts[id - 2].text || ' '
    await enqueue(() => bridge.textContainerUpgrade(new TextContainerUpgrade({ containerID: id, containerName: `t${id}`, content })))
  }
}

/** Processing: the page-loading spinner, titled with the command, until the page opens. */
function renderThinking(command: string, step: string) {
  if (loading?.host === ASSISTANT) return loadingProgress(step, command)
  loading = { title: command, host: ASSISTANT, step, frame: 0 }
  return renderLoading()
}

function logHeard(line: HeardLine) {
  if (!line.text) return
  heard = [line, ...heard].slice(0, MAX_HEARD)
  companion.update()
}

/** Turns the glasses mic on or off, skipping calls that wouldn't change anything. */
async function setMic(on: boolean): Promise<boolean> {
  if (on === micOn) return true
  micOn = on
  const ok = await enqueue(() => bridge.audioControl(on, AudioInputSource.Glasses)).catch(() => false)
  if (!ok && on) micOn = false
  return ok
}

/** Starts waiting for the wake phrase when hands-free is on and nothing else is listening. */
async function armWake() {
  if (!handsFree || recorder || wake) return
  wake = new WakeListener(
    utterance => void checkWake(utterance),
    utterance => void transcribeAmbient(utterance),
  )
  if (view === 'reader') void renderReader()
  companion.update()
  if (!(await setMic(true))) {
    wake = null
    voiceUi = { phase: 'error', text: "Couldn't turn on the glasses microphone." }
    companion.update()
  }
}

function disarmWake() {
  if (!wake) return
  wake = null
  if (view === 'reader') void renderReader()
}

function setHandsFree(on: boolean) {
  handsFree = on
  void enqueue(() => bridge.setLocalStorage(HANDS_FREE_KEY, on ? '1' : '0')).catch(() => {})
  if (on) void armWake()
  else {
    disarmWake()
    if (!recorder) void setMic(false)
    companion.update()
  }
}

function setRecommendations(on: boolean) {
  recommendationsOn = on
  void enqueue(() => bridge.setLocalStorage(RECOMMENDATIONS_KEY, on ? '1' : '0')).catch(() => {})
  if (on) {
    if (doc && !status && !loading) companion.recommend(doc)
  } else {
    companion.resetRecommendations()
    if (view === 'recommendations') void showContent()
  }
  companion.update()
}

/** Sends the start of an ambient utterance to see whether it opens with the wake phrase. */
async function checkWake(utterance: Uint8Array[]) {
  const listener = wake
  const pcm = concat(utterance)
  const result = sendVoice(pcm, 'check').catch(err => {
    if (wake === listener) {
      voiceUi = { phase: 'idle', text: '', note: err instanceof Error ? err.message : String(err) }
      companion.update()
    }
    return null
  })
  const check: { result: typeof result; bytes: number; line?: HeardLine } = { result, bytes: pcm.length }
  wakeChecks.set(utterance, check)
  const res = await result
  if (res && !res.wake && res.transcript) {
    // Show the start right away; transcribeAmbient fills in the rest when the sentence ends.
    check.line = { text: res.transcript, pending: true }
    logHeard(check.line)
  }
  if (!listener || !res?.wake || wake !== listener || recorder) return
  wake = null
  void capture(wakeSeed(listener, utterance), true)
}

/**
 * Audio the question capture starts from. Words in the wake sentence itself are
 * not the question; but the check takes a moment, so anything said since it
 * ended is kept. If the user is still talking in that same breath, the whole
 * sentence is kept and the server strips everything up to "Hey Search".
 */
function wakeSeed(listener: WakeListener, utterance: Uint8Array[]): Uint8Array[] {
  const live = listener.current
  return live === utterance ? utterance.slice() : live.slice()
}

/**
 * Puts a finished ambient utterance on the phone's transcript (wake commands log
 * themselves). The wake check only hears the start, so a slow "Hey... Search",
 * or one later in a sentence, is caught here from the whole sentence instead.
 */
async function transcribeAmbient(utterance: Uint8Array[]) {
  const listener = wake
  const check = wakeChecks.get(utterance)
  const res = await check?.result
  if (!check || !res || res.wake) return
  const pcm = concat(utterance)
  // The check only covered the start; transcribe the whole thing for the phone.
  const full = pcm.length > check.bytes ? await sendVoice(pcm, 'check').catch(() => null) : null
  if (full?.wake && listener && wake === listener && !recorder) {
    wake = null
    void capture(wakeSeed(listener, utterance), true)
  }
  const text = full?.transcript || res.transcript
  if (check.line) {
    check.line.text = text
    check.line.pending = false
    companion.update()
  } else logHeard({ text })
}

/** Voice search from the menu, phone or a tap on the voice screen: capture without the wake phrase. */
function startVoice() {
  if (recorder) return
  wake = null
  void capture([], false)
}

/** Tap on the voice card (or the phone's mic / Select): send what's been said, or listen again. */
function toggleVoice() {
  if (recorder) void stopVoice(true)
  else startVoice()
}

/** Swipe on the voice card (or the phone's Back): cancel the capture, or leave the card. */
function voiceBack() {
  if (recorder) return void stopVoice(false)
  voiceSeq++ // drops a search still in flight
  void showContent()
}

async function capture(seed: Uint8Array[], fromWake: boolean) {
  const seq = ++voiceSeq
  const rec = (recorder = new VoiceRecorder(reason => void stopVoice(reason !== 'no-speech'), seed))
  wakeCapture = fromWake
  voiceUi = { phase: 'listening', text: '' }
  await renderAssistant(GREETING, fromWake ? 'Listening...' : LISTEN_HINT)
  if (await setMic(true)) {
    if (seq === voiceSeq) void livePreview(rec, seq)
  } else if (seq === voiceSeq) {
    recorder = null
    voiceUi = { phase: 'error', text: "Couldn't turn on the glasses microphone." }
    await renderAssistant("Couldn't turn on the microphone.", VOICE_HINT)
  }
}

/** While listening, re-transcribes the audio so far so the words show up as they're spoken. */
async function livePreview(rec: VoiceRecorder, seq: number) {
  let sentMs = 0
  while (seq === voiceSeq) {
    await new Promise(r => setTimeout(r, PARTIAL_MS))
    if (seq !== voiceSeq) return
    if (!rec.hasSpeech || rec.durationMs === sentMs) continue
    sentMs = rec.durationMs
    try {
      const { command } = await sendVoice(rec.pcm(), 'partial')
      if (seq !== voiceSeq || !command) continue
      voiceUi = { phase: 'listening', text: command }
      await renderAssistant(`"${command}"`, wakeCapture ? 'Listening...' : LISTEN_HINT)
    } catch (err) {
      if (seq !== voiceSeq) return
      voiceUi = { ...voiceUi, note: err instanceof Error ? err.message : String(err) }
      companion.update()
    }
  }
}

async function stopVoice(send: boolean) {
  const rec = recorder
  if (!rec) return
  recorder = null
  const seq = ++voiceSeq
  // Hands-free keeps the mic on and goes back to waiting for the wake phrase.
  if (handsFree) void armWake()
  else void setMic(false)
  if (!send) {
    voiceUi = { phase: 'idle', text: '' }
    return void showContent()
  }
  const quiet = wakeCapture
  // Hands-free results fade back to the page, like a notification.
  const fadeLater = () =>
    quiet && setTimeout(() => seq === voiceSeq && view === 'voice' && !recorder && void showContent(), WAKE_RESULT_MS)
  const fail = async (message: string, detail = message) => {
    voiceUi = { phase: 'error', text: detail }
    if (loading?.host === ASSISTANT) stopLoading()
    await renderAssistant(message, quiet ? `Say "${WAKE_PHRASE}" to try again` : VOICE_HINT)
    fadeLater()
  }
  if (!rec.hasSpeech) {
    voiceUi = { phase: 'error', text: "Didn't hear anything. Is the mic on?" }
    if (quiet) return void showContent()
    return void renderAssistant("Sorry, I didn't hear anything.", VOICE_HINT)
  }
  voiceUi = { phase: 'sending', text: voiceUi.text }
  void renderThinking(voiceUi.text || '...', 'Got it')
  try {
    const { command } = await sendVoice(rec.pcm())
    if (seq !== voiceSeq) return
    report({ voice: rec.durationMs, chars: command.length })
    if (!command) return void fail("Sorry, I didn't catch that.", "Couldn't make that out.")
    logHeard({ text: command, command: true })
    voiceUi = { phase: 'searching', text: command }
    void renderThinking(command, 'Searching the web')
    const searching = loading
    const page = await searchPage(command)
    // Dropped if the user opened something else meanwhile.
    if (seq !== voiceSeq || loading !== searching) return
    voiceUi = { phase: 'done', text: command, url: page.url }
    logHeard({ text: page.url, opened: true })
    // Opens like a typed URL: the spinner carries on, then the page shows.
    // Recommendations don't pop up over a page the user just asked for by voice.
    void load(page.url, 0, true, true)
  } catch (err) {
    if (seq !== voiceSeq) return
    const detail = err instanceof Error ? err.message : String(err)
    void fail("Sorry, I couldn't find that.", detail)
  }
}

// ── Loading ─────────────────────────────────────────────────────────────────
async function load(url: string, restore = 0, pushHistory = false, fromVoice = false) {
  const seq = ++loadSeq
  companion.resetRecommendations()
  backend.beginPage()
  loadAbort?.abort()
  const abort = (loadAbort = new AbortController())
  let host = url
  try {
    host = new URL(/^[a-z]+:\/\//i.test(url) ? url : `https://${url}`).hostname
  } catch {}
  if (pushHistory && doc) {
    history = [...history, { url: doc.url, title: doc.title, position: position() }].slice(-MAX_HISTORY)
  }
  status = null
  loading = { title: host, host, step: 'Fetching the page', frame: 0 }
  companion.setBusy(true)
  renderLoading().catch(console.error)

  // Screens stream in; show the deck as soon as the one to open on arrives.
  let shown = false
  const reveal = () => {
    shown = true
    stopLoading()
    status = null
    companion.setBusy(false)
    // Don't pull the page over a voice command in progress; it shows when that ends.
    if (view !== 'voice') void showContent()
  }

  let failed: string | null = null
  try {
    await condense(url, abort.signal, event => {
      if (seq !== loadSeq) return
      if ('page' in event) {
        doc = event.page
        screens = []
        deckLoading = !doc.manga
        selected.clear()
        strips.clear()
        remember({ url: doc.url, title: doc.title })
        const p = event.page
        loadingProgress(p.manga ? 'Opening the chapter' : `Read ${p.wordCount.toLocaleString()} words · picking a layout`, p.title)
      } else if ('kind' in event) {
        doc!.deck = { kind: event.kind, screens }
        loadingProgress(`${event.kind.toUpperCase()} · writing the first screen`)
      } else if ('screens' in event) {
        doc!.deck ??= { kind: 'reference', screens }
        screens.push(...event.screens)
        if (shown) void refreshDeck()
        else if (screens.length > restore) {
          enterScreen(restore)
          reveal()
        }
      } else if ('replace' in event) {
        // A long list sent early with its first items, now complete.
        screens[event.replace] = event.screen
        if (shown) void refreshDeck()
      } else if ('done' in event) {
        deckLoading = false
        if (event.truncated) doc!.truncated = true
      }
    })
    if (seq !== loadSeq) return
    if (isManga()) {
      mangaPage = Math.min(Math.max(0, restore), doc!.manga!.pages - 1)
      mangaStrip = mangaFromEnd ? -1 : 0
      mangaStrips = 0
    } else if (!shown) {
      enterScreen(Math.max(0, Math.min(restore, screens.length - 1)))
    }
    scheduleSave()
    if (doc) {
      void backend.publish(doc)
      if (!fromVoice && recommendationsOn) companion.recommend(doc)
    }
  } catch (err) {
    if (seq !== loadSeq) return
    deckLoading = false
    failed = err instanceof Error ? err.message : String(err)
    companion.setError(failed)
  } finally {
    if (seq === loadSeq) {
      companion.setBusy(false)
      stopLoading()
    }
    mangaFromEnd = false
  }
  if (seq !== loadSeq) return
  // Screens already on the glasses stay after a failure; the rest just never arrive.
  if (shown) return refreshDeck()
  status = failed ? `Could not load ${host}\n\n${failed}\n\nTap for menu.` : null
  await showContent()
}

// ── Persistence (SDK storage; browser storage doesn't survive restarts) ─────
function remember(entry: Recent) {
  recent = [entry, ...recent.filter(r => r.url !== entry.url)].slice(0, MAX_RECENT)
}

let saveTimer: ReturnType<typeof setTimeout> | undefined
function scheduleSave() {
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => void flushSave(), 1000)
}
async function flushSave() {
  clearTimeout(saveTimer)
  const state = doc ? { url: doc.url, position: position() } : null
  // A bookmark keeps up with reading, so it reopens where you stopped.
  const mark = doc && !status && !loading ? bookmarkOf(doc.url) : undefined
  if (mark) Object.assign(mark, { position: position(), where: where() })
  await enqueue(() => bridge.setLocalStorage(STATE_KEY, JSON.stringify(state))).catch(() => {})
  await enqueue(() => bridge.setLocalStorage(BOOKMARKS_KEY, JSON.stringify(bookmarks))).catch(() => {})
  await enqueue(() => bridge.setLocalStorage(RECENT_KEY, JSON.stringify(recent))).catch(() => {})
  await enqueue(() => bridge.setLocalStorage(MANGA_STYLE_KEY, mangaStyle)).catch(() => {})
}

async function restore() {
  try {
    const connection = (await enqueue(() => bridge.getLocalStorage(SESSION_KEY)).catch(() => '')) || ''
    try {
      const parsed = JSON.parse(connection)
      if (typeof parsed?.id === 'string') savedSession = parsed.id
      if (typeof parsed?.url === 'string') savedSessionUrl = parsed.url
    } catch { savedSession = connection }
    recent = JSON.parse((await enqueue(() => bridge.getLocalStorage(RECENT_KEY))) || '[]')
    try {
      const saved = JSON.parse((await enqueue(() => bridge.getLocalStorage(BOOKMARKS_KEY))) || '[]')
      if (Array.isArray(saved)) bookmarks = saved.filter(b => typeof b?.url === 'string' && typeof b?.title === 'string')
    } catch {}
    const saved = JSON.parse((await enqueue(() => bridge.getLocalStorage(STATE_KEY))) || 'null')
    if ((await enqueue(() => bridge.getLocalStorage(MANGA_STYLE_KEY)).catch(() => '')) === 'lines') mangaStyle = 'lines'
    const savedLayout = await enqueue(() => bridge.getLocalStorage(RECOMMENDATION_LAYOUT_KEY)).catch(() => '')
    if (RECOMMENDATION_LAYOUTS.some(layout => layout.id === savedLayout)) recommendationLayout = savedLayout as RecommendationLayout
    if ((await enqueue(() => bridge.getLocalStorage(HANDS_FREE_KEY)).catch(() => '')) === '0') handsFree = false
    if ((await enqueue(() => bridge.getLocalStorage(RECOMMENDATIONS_KEY)).catch(() => '')) === '0') recommendationsOn = false
    void armWake()
    companion.update()
    // Deep link: index.html?open=<page> opens straight into a page.
    // (Not ?url= — Vite reserves that query for asset imports.)
    const params = new URLSearchParams(location.search)
    const linked = params.get('open')
    if (linked) await load(linked)
    else if (saved?.url) await load(saved.url, saved.position ?? 0)
  } catch (err) {
    console.warn('restore failed', err)
  }
}

// ── Input ───────────────────────────────────────────────────────────────────
// CLICK_EVENT is 0 and protobuf omits zero values, so a tap arrives as an
// envelope with no eventType. Resolve the default inside the envelope check.
function eventTypeOf(envelope?: { eventType?: OsEventTypeList }): OsEventTypeList | null {
  if (!envelope) return null
  return envelope.eventType ?? OsEventTypeList.CLICK_EVENT
}

let cleanedUp = false
const unsubscribe = bridge.onEvenHubEvent(event => {
  if (event.audioEvent) {
    const pcm = event.audioEvent.audioPcm
    if (recorder) {
      recorder.push(pcm)
      if (recorder) companion.voiceLevel(recorder.level, recorder.durationMs)
    } else if (wake) {
      wake.push(pcm)
      companion.voiceLevel(wake.level)
    }
    return
  }
  const sysType = eventTypeOf(event.sysEvent)
  const textType = eventTypeOf(event.textEvent)
  const listType = eventTypeOf(event.listEvent)

  if ([sysType, textType, listType].includes(OsEventTypeList.DOUBLE_CLICK_EVENT)) {
    if (view === 'recommendations') { companion.closeRecommendations(); return }
    if (view === 'actions') { actionBack(); return }
    // Don't clean up here: the user may cancel the system dialog.
    bridge.shutDownPageContainer(1)
    return
  }
  if (sysType === OsEventTypeList.FOREGROUND_EXIT_EVENT) {
    backend.setSuspended(true)
    // The WebView may be suspended in the background; don't leave the mic on.
    void stopVoice(false)
    disarmWake()
    void setMic(false)
    void flushSave()
    return
  }
  if (sysType === OsEventTypeList.FOREGROUND_ENTER_EVENT) {
    backend.setSuspended(false)
    void armWake()
    return
  }
  if (sysType === OsEventTypeList.SYSTEM_EXIT_EVENT || sysType === OsEventTypeList.ABNORMAL_EXIT_EVENT) {
    cleanup()
    return
  }

  if (view === 'voice') {
    if (textType === OsEventTypeList.SCROLL_TOP_EVENT || textType === OsEventTypeList.SCROLL_BOTTOM_EVENT) voiceBack()
    else if (sysType === OsEventTypeList.CLICK_EVENT || textType === OsEventTypeList.CLICK_EVENT) toggleVoice()
    return
  }

  if (isListView()) {
    if (listType === OsEventTypeList.CLICK_EVENT) {
      const item = listItems[event.listEvent?.currentSelectItemIndex ?? 0]
      item?.run()
    }
    return
  }

  if (textType === OsEventTypeList.SCROLL_TOP_EVENT) return step(-1)
  if (textType === OsEventTypeList.SCROLL_BOTTOM_EVENT) return step(1)
  if (sysType === OsEventTypeList.CLICK_EVENT || textType === OsEventTypeList.CLICK_EVENT) select()
})

/** A native glasses list (menu, contents, links...) is on screen. */
function isListView() {
  return !isContentView() && view !== 'actions' && view !== 'recommendations' && view !== 'voice'
}

function cleanup() {
  if (cleanedUp) return
  cleanedUp = true
  companion.resetRecommendations()
  if (micOn) void bridge.audioControl(false)
  void flushSave()
  unsubscribe()
  backend.disconnect()
}
window.addEventListener('beforeunload', cleanup)

// ── Phone companion UI ──────────────────────────────────────────────────────
const companion: Companion = mountCompanion(document.querySelector<HTMLDivElement>('#app')!, {
  getState: () => ({
    doc,
    status,
    recent,
    bookmarks,
    bookmarked: !!doc && !!bookmarkOf(doc.url),
    spec: mirror,
    position: isManga()
      ? `Page ${mangaPage + 1} / ${doc!.manga!.pages}${mangaStrips > 1 ? ` · ${mangaStrip + 1}/${mangaStrips}` : ''}`
      : isDeck() ? `${screenIdx + 1} / ${screens.length}${deckLoading ? '+' : ''}` : '',
    kind: isDeck() ? doc?.deck?.kind : undefined,
    voice: voiceUi,
    handsFree,
    recommendations: recommendationsOn,
    list: isListView() ? listItems.map(i => i.label) : null,
    listening: !!wake,
    wakePhrase: WAKE_PHRASE,
    heard,
  }),
  onLoad: url => void load(url),
  onBookmark: toggleBookmark,
  onOpenBookmark: openBookmark,
  onRemoveBookmark: removeBookmark,
  onStep: delta => {
    if (view === 'voice') voiceBack()
    else if (view === 'actions' || view === 'recommendations' || isContentView()) step(delta)
    else void showContent()
  },
  onSelect: () => {
    if (view === 'voice') toggleVoice()
    else if (isContentView() || view === 'actions' || view === 'recommendations') select()
  },
  onBack: () => {
    if (view === 'voice') voiceBack()
    else if (view === 'recommendations') companion.closeRecommendations()
    else if (view === 'actions') actionBack()
    else void showContent()
  },
  onMenu: () => {
    if (view === 'menu') void showContent()
    else openMenu()
  },
  onPick: index => {
    if (isListView()) listItems[index]?.run()
  },
  onVoice: toggleVoice,
  onHandsFree: setHandsFree,
  onRecommendations: setRecommendations,
  recommendations: {
    ready(batch) {
      if (batch.page.url !== doc?.url || cleanedUp) return
      glassRecommendations = batch
      recommendationsOpen = true
      recommendationIndex = 0
      recommendationSelected = false
      recommendationBase = undefined
      if (isContentView() && !loading && !status) void renderGlassesRecommendations()
    },
    selected(selection) {
      if (!glassRecommendations) return
      recommendationIndex = glassRecommendations.actions.findIndex(action => action.id === selection.action.id)
      recommendationSelected = true
      if (view === 'recommendations') void renderGlassesRecommendations()
    },
    reset() { glassRecommendations = undefined; recommendationBase = undefined; recommendationsOpen = false },
    closed() {
      recommendationsOpen = false
      recommendationBase = undefined
      if (view === 'recommendations') void showContent()
    },
    opened() {
      if (!glassRecommendations || glassRecommendations.page.url !== doc?.url || cleanedUp) return
      recommendationsOpen = true
      recommendationIndex = 0
      recommendationBase = undefined
      void renderGlassesRecommendations()
    },
  },
  backend: {
    state: () => backend.state,
    connect: (token, g2Token) => { void connectBackend(token, g2Token) },
    disconnect: () => { backend.disconnect(); actionCards.clear(); void showContent() },
    save: p => { void backend.updatePreferences(p) },
    review: openActions,
    sync: () => { if (doc && !loading && !deckLoading) { backend.beginPage(); void backend.publish(doc) } },
    back: () => { void showContent() },
  },
})

async function renderGlassesRecommendations() {
  if (!glassRecommendations) return
  const alreadyVisible = view === 'recommendations'
  recommendationBase ??= isDeck() ? currentSpec() : mirror
  const spec = glassesRecommendationSpec(recommendationBase,
    glassRecommendations.actions.map(action => action.title), recommendationIndex, recommendationSelected, recommendationLayout)
  view = 'recommendations'
  if (!alreadyVisible) return renderPageSpec(spec).catch(console.error)
  // Swiping only updates the small text panel; do not resend images over BLE.
  const previous = mirror
  mirror = spec
  companion.update()
  for (const [index, box] of spec.texts.entries()) {
    if (previous.texts[index]?.text === box.text) continue
    const id = index + 2 // Full-screen capture is container 1.
    await enqueue(() => bridge.textContainerUpgrade(new TextContainerUpgrade({
      containerID: id, containerName: `t${id}`, content: box.text,
    }))).catch(console.error)
  }
}

async function connectBackend(token: string, g2Token: string) {
  try {
    actionCards.clear()
    const api = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')
    const resumeId = savedSession, resumeUrl = savedSessionUrl
    const restoreDisplay = !loading && !deckLoading && (!doc || doc.url === resumeUrl)
    await backend.connect(new BackendClient(`${api}/api/backend`, token, g2Token), resumeId, restoreDisplay)
    // A page PUT invalidates session actions, even for the same URL. Resume instead
    // of submitting again when the device is showing this session's saved page.
    if (backend.state.connected && doc && !loading && !deckLoading &&
        (backend.state.sessionId !== resumeId || doc.url !== resumeUrl)) {
      backend.beginPage(); void backend.publish(doc)
    }
  } catch (e) { backend.state.message = e instanceof Error ? e.message : 'Could not connect.'; companion.update() }
}

function openActions(group: ActionGroup) {
  actionGroup = group
  let cards = actionCards.get(group)
  if (!cards) {
    cards = apiActionCards(intent => {
      void backend.decide(group, intent).then(result => cards!.finish(intent.request_id, result.outcome, result.message))
    }, () => { if (view === 'actions' && actionGroup === group) void renderActions() })
    actionCards.set(group, cards)
  }
  cards.show(backend.actionDisplay(group))
  cards.setEnabled(backend.state.connected && backend.state.online)
  view = 'actions'
  cards.open()
  void renderActions()
}

function actionBack() {
  if (actionCards.get(actionGroup)?.dispatch('back') === 'exit') void showContent()
}

async function renderActions() {
  const card = actionCards.get(actionGroup)?.view()
  if (!card) return
  view = 'actions'; onScreen = 'other'; renderSeq++
  const body = [card.title, '', ...card.lines].join('\n')
  mirror = readerSpec(body, card.hint); companion.update()
  await enqueue(() => bridge.rebuildPageContainer(new RebuildPageContainer({
    containerTotalNum: 2, textObject: readerContainers(body, card.hint),
  }))).catch(console.error)
}

companion.update()
void restore()
