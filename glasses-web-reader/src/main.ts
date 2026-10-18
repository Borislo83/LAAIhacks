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
} from '@evenrealities/even_hub_sdk'
import { pxTruncate } from '@evenrealities/pretext'
import type { CondensedPage, MangaStrip, Screen } from '../shared/types'
import { condense, fetchStrip, report, type MangaStyle } from './api'
import { renderScreen, screenTitle, type PageSpec, type TextBox } from './screens'
import { mountCompanion, type Companion } from './companion'

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
const MANGA_STYLE_KEY = 'webreader.v1.mangaStyle'
const MAX_RECENT = 15
const MAX_HISTORY = 10

type View = 'reader' | 'deck' | 'manga' | 'menu' | 'contents' | 'links' | 'recent'
interface Recent {
  url: string
  title: string
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
Double-tap: exit`

// ── State ───────────────────────────────────────────────────────────────────
let view: View = 'reader'
let onScreen: 'reader' | 'manga' | 'other' = 'reader'
let doc: CondensedPage | null = null
let screens: Screen[] = []
let screenIdx = 0
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
let history: HistoryEntry[] = []
let listItems: MenuItem[] = []
let loadSeq = 0
let renderSeq = 0
let mirror: PageSpec = { texts: [], images: [] }

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
  if (doc) return pxTruncate(doc.title, INNER_W)
  return status ? 'Web Reader' : 'Web Reader · waiting for a URL'
}

const created = await bridge.createStartUpPageContainer(
  new CreateStartUpPageContainer({ containerTotalNum: 2, textObject: readerContainers(WELCOME, footerText()) }),
)
if (created !== 0) console.error('createStartUpPageContainer failed:', created)

// ── Rendering ───────────────────────────────────────────────────────────────
/** Shows the current page in whichever form it has: status text, deck or manga. */
function showContent() {
  if (!status && doc && isManga()) return showStrip(mangaPage, mangaStrip)
  if (!status && doc && isDeck()) return renderDeck()
  return renderReader()
}

async function renderReader() {
  view = 'reader'
  renderSeq++
  const body = bodyText()
  const footer = footerText()
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

function textContainer(t: TextBox, id: number) {
  return new TextContainerProperty({
    xPosition: t.x, yPosition: t.y, width: t.w, height: t.h,
    borderWidth: t.border ?? 0, borderColor: t.color ?? 13, borderRadius: t.radius ?? 0,
    paddingLength: t.pad ?? 4,
    containerID: id, containerName: `t${id}`,
    content: t.text || ' ',
    isEventCapture: 0,
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
    position: `${screenIdx + 1} / ${screens.length}${deckLoading ? '+' : ''}`,
    selected: selected.get(screenIdx) ?? 0,
    upcoming: screens.slice(screenIdx + 1).map(s => screenTitle(s)),
  }
}

async function renderDeck() {
  view = 'deck'
  onScreen = 'other'
  const seq = ++renderSeq
  const spec = renderScreen(screens[screenIdx], deckContext())
  mirror = spec
  companion.update()

  // An invisible full-screen layer, declared first so it sits behind
  // everything, receives all taps and swipes for the page.
  const capture = new TextContainerProperty({
    xPosition: 0, yPosition: 0, width: 576, height: 288,
    borderWidth: 0, borderColor: 0, paddingLength: 0,
    containerID: 1, containerName: 'capture',
    content: ' ',
    isEventCapture: 1,
  })
  const texts = [capture, ...spec.texts.map((t, i) => textContainer(t, i + 2))]
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
  const current = screens[screenIdx]
  const before = mirror
  const spec = renderScreen(current, deckContext())
  const sameBoxes =
    spec.texts.length === before.texts.length &&
    spec.images.length === before.images.length &&
    spec.texts.every((t, i) => t.x === before.texts[i].x && t.y === before.texts[i].y && t.w === before.texts[i].w && t.h === before.texts[i].h)
  if (!sameBoxes) return
  mirror = spec
  companion.update()
  for (const [i, t] of spec.texts.entries()) {
    if (t.text === before.texts[i].text) continue
    const id = i + 2
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
    goToScreen(screenIdx - 1)
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
function goToScreen(index: number) {
  if (!doc || status || index < 0 || index >= screens.length) return
  screenIdx = index
  scheduleSave()
  renderDeck().catch(console.error)
}

function goTo(index: number) {
  if (isManga()) void showStrip(index, 0)
  else goToScreen(index)
}

/** Swipe: through list items, then onto the next screen once you pass the last one. */
function step(delta: 1 | -1) {
  if (isManga() && !status) return mangaStep(delta)
  if (stepList(delta)) return
  goToScreen(screenIdx + delta)
}

/** Tap: open the selected list item, otherwise the menu. */
function select() {
  const s = isDeck() && !status ? screens[screenIdx] : undefined
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
  if (doc && !status) {
    items.push({ label: 'Resume', run: () => void showContent() })
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
  if (doc && !status && !isManga()) {
    if (screens.length > 1) items.push({ label: `Contents (${screens.length})`, run: openContents })
    if (doc.links.length) items.push({ label: `Links on page (${doc.links.length})`, run: openLinks })
    if (position() > 0) items.push({ label: 'Back to start', run: () => goTo(0) })
  }
  if (isManga() && !status && position() > 0) items.push({ label: 'Back to first page', run: () => goTo(0) })
  if (recent.length) items.push({ label: `Recent pages (${recent.length})`, run: openRecent })
  items.push({ label: 'Exit', run: () => void bridge.shutDownPageContainer(1) })
  renderList('menu', doc ? `Menu · ${doc.site}` : 'Menu', items).catch(console.error)
}

function openContents() {
  if (!doc) return
  const items = screens.map((s, i) => ({ label: `${i + 1}. ${screenTitle(s)}`, run: () => goToScreen(i) }))
  renderList('contents', 'Contents', [back, ...items]).catch(console.error)
}

function openLinks() {
  if (!doc) return
  const items = doc.links.map(l => ({ label: l.text, run: () => void load(l.url, 0, true) }))
  renderList('links', 'Links · tap to open', [back, ...items]).catch(console.error)
}

function openRecent() {
  const items = recent.map(r => ({ label: r.title, run: () => void load(r.url) }))
  renderList('recent', 'Recent pages', [back, ...items]).catch(console.error)
}

// ── Loading ─────────────────────────────────────────────────────────────────
async function load(url: string, restore = 0, pushHistory = false) {
  const seq = ++loadSeq
  loadAbort?.abort()
  const abort = (loadAbort = new AbortController())
  let host = url
  try {
    host = new URL(/^[a-z]+:\/\//i.test(url) ? url : `https://${url}`).hostname
  } catch {}
  if (pushHistory && doc) {
    history = [...history, { url: doc.url, title: doc.title, position: position() }].slice(-MAX_HISTORY)
  }
  status = `Building smart view of ${host}...\n\nReading the page and picking a layout. The first screen shows in a few seconds.`
  companion.setBusy(true)
  renderReader().catch(console.error)

  // Screens stream in; show the deck as soon as the one to open on arrives.
  let shown = false
  const reveal = () => {
    shown = true
    status = null
    companion.setBusy(false)
    void showContent()
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
      } else if ('kind' in event) {
        doc!.deck = { kind: event.kind, screens }
      } else if ('screens' in event) {
        doc!.deck ??= { kind: 'reference', screens }
        screens.push(...event.screens)
        if (shown) void refreshDeck()
        else if (screens.length > restore) {
          screenIdx = restore
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
      screenIdx = Math.max(0, Math.min(restore, screens.length - 1))
    }
    scheduleSave()
  } catch (err) {
    if (seq !== loadSeq) return
    deckLoading = false
    failed = err instanceof Error ? err.message : String(err)
    companion.setError(failed)
  } finally {
    if (seq === loadSeq) companion.setBusy(false)
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
  await enqueue(() => bridge.setLocalStorage(STATE_KEY, JSON.stringify(state))).catch(() => {})
  await enqueue(() => bridge.setLocalStorage(RECENT_KEY, JSON.stringify(recent))).catch(() => {})
  await enqueue(() => bridge.setLocalStorage(MANGA_STYLE_KEY, mangaStyle)).catch(() => {})
}

async function restore() {
  try {
    recent = JSON.parse((await enqueue(() => bridge.getLocalStorage(RECENT_KEY))) || '[]')
    const saved = JSON.parse((await enqueue(() => bridge.getLocalStorage(STATE_KEY))) || 'null')
    if ((await enqueue(() => bridge.getLocalStorage(MANGA_STYLE_KEY)).catch(() => '')) === 'lines') mangaStyle = 'lines'
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
  const sysType = eventTypeOf(event.sysEvent)
  const textType = eventTypeOf(event.textEvent)
  const listType = eventTypeOf(event.listEvent)

  if ([sysType, textType, listType].includes(OsEventTypeList.DOUBLE_CLICK_EVENT)) {
    // Don't clean up here: the user may cancel the system dialog.
    bridge.shutDownPageContainer(1)
    return
  }
  if (sysType === OsEventTypeList.FOREGROUND_EXIT_EVENT) {
    void flushSave()
    return
  }
  if (sysType === OsEventTypeList.FOREGROUND_ENTER_EVENT) {
    return
  }
  if (sysType === OsEventTypeList.SYSTEM_EXIT_EVENT || sysType === OsEventTypeList.ABNORMAL_EXIT_EVENT) {
    cleanup()
    return
  }

  if (!isContentView()) {
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

function cleanup() {
  if (cleanedUp) return
  cleanedUp = true
  void flushSave()
  unsubscribe()
}
window.addEventListener('beforeunload', cleanup)

// ── Phone companion UI ──────────────────────────────────────────────────────
const companion: Companion = mountCompanion(document.querySelector<HTMLDivElement>('#app')!, {
  getState: () => ({
    doc,
    status,
    recent,
    spec: mirror,
    position: isManga()
      ? `Page ${mangaPage + 1} / ${doc!.manga!.pages}${mangaStrips > 1 ? ` · ${mangaStrip + 1}/${mangaStrips}` : ''}`
      : isDeck() ? deckContext().position : '',
    kind: isDeck() ? doc?.deck?.kind : undefined,
  }),
  onLoad: url => void load(url),
  onStep: delta => {
    if (!isContentView()) void showContent()
    else step(delta)
  },
  onSelect: () => {
    if (isContentView()) select()
  },
})

companion.update()
void restore()
