import sharp from 'sharp'
import type { CondensedPage, GlassImage, ListItem, MangaStrip } from '../shared/types'
import { normalizeGlyphs } from './extract'
import { FetchError, safeFetchImage } from './safe-fetch'

// MangaDex chapters are image-only pages built client-side, so they skip the
// HTML pipeline. Chapter data comes from the public API; each page image is
// scaled to the display width and cut into full-screen strips the wearer
// swipes through. A series page becomes a chapter list.

const API = 'https://api.mangadex.org'
const SITE = 'https://mangadex.org'
const UA = 'GlassesWebReader/0.1'
// at-home base URLs expire after ~15 minutes.
const AT_HOME_TTL_MS = 10 * 60_000
const PAGE_CACHE_MAX = 12
const MAX_CHAPTERS = 20

// The display is 576x288; image containers top out at 288x144, so a strip
// ships as a 2x2 grid of tiles.
const SCREEN_W = 576
const SCREEN_H = 288
const TILE_W = 288
const TILE_H = 144
const LEVELS = 16
// Mean brightness above which a screen is mostly paper and gets inverted.
const BRIGHT_MEAN = 110
const GAMMA = 1.6
const STEP = 255 / (LEVELS - 1)

// Source images for a whole chapter are fetched in the background when it is
// opened, so page turns and "go to page" never wait on MangaDex. Data-saver
// JPEGs are ~100-300 KB, so a few chapters fit comfortably.
const SOURCE_CACHE_MAX = 150
const PREFETCH_CONCURRENCY = 2

const CHAPTER_URL = /^https?:\/\/(?:www\.)?mangadex\.org\/chapter\/([0-9a-f-]{36})/i
const TITLE_URL = /^https?:\/\/(?:www\.)?mangadex\.org\/title\/([0-9a-f-]{36})/i

export const isMangaDex = (url: string) => CHAPTER_URL.test(url) || TITLE_URL.test(url)

async function api<T = any>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { headers: { 'user-agent': UA }, signal: AbortSignal.timeout(12_000) }).catch(
    err => {
      throw new FetchError(`MangaDex unreachable: ${err?.cause?.code ?? err}`, 502)
    },
  )
  if (res.status === 404) throw new FetchError('MangaDex has no such chapter or title', 404)
  if (!res.ok) throw new FetchError(`MangaDex responded ${res.status}`, 502)
  return res.json() as Promise<T>
}

const clean = (s: string) => normalizeGlyphs(s).replace(/\s+/g, ' ').trim()

const pickTitle = (titles: Record<string, string> | undefined) =>
  clean(titles?.en ?? Object.values(titles ?? {})[0] ?? 'Untitled')

const chapterLabel = (a: { chapter?: string | null; title?: string | null; volume?: string | null }) =>
  [a.chapter ? `Ch. ${a.chapter}` : 'Oneshot', a.title ? clean(a.title) : ''].filter(Boolean).join(' · ')

const chapterUrl = (id: string) => `${SITE}/chapter/${id}`

// ── Chapter / title pages ───────────────────────────────────────────────────

export async function mangaPage(url: string): Promise<CondensedPage> {
  const chapter = CHAPTER_URL.exec(url)?.[1]
  if (chapter) return chapterPage(chapter)
  // ?tl= keeps "All chapters" in the translation the wearer was reading.
  const lang = /[?&]tl=([a-z-]{2,8})/i.exec(url)?.[1]
  return titlePage(TITLE_URL.exec(url)![1], lang)
}

async function chapterPage(id: string): Promise<CondensedPage> {
  const { data } = await api(`/chapter/${id}?includes[]=manga&includes[]=scanlation_group`)
  const a = data.attributes
  if (a.externalUrl) throw new FetchError('This chapter is hosted off MangaDex and cannot be read here', 422)
  const manga = data.relationships.find((r: any) => r.type === 'manga')
  const group = data.relationships.find((r: any) => r.type === 'scanlation_group')?.attributes?.name
  const series = pickTitle(manga?.attributes?.title)
  const home = await atHome(id)
  const pages = home.files.length
  if (!pages) throw new FetchError('This chapter has no pages on MangaDex', 422)
  void prefetchChapter(id, pages)

  const { prev, next } = await neighbours(manga.id, id, a.translatedLanguage)
  const label = chapterLabel(a)
  const info = [series, label, group ? `Translated by ${clean(group)}` : '', `${pages} pages · ${a.translatedLanguage}`]
    .filter(Boolean)
    .map(text => ({ type: 'p' as const, text }))

  return {
    url: chapterUrl(id),
    title: `${series} · ${label}`,
    site: 'mangadex.org',
    sections: [{ heading: label, blocks: info }],
    links: [],
    wordCount: 0,
    manga: {
      chapterId: id,
      series,
      chapter: label,
      pages,
      prev: prev && chapterUrl(prev),
      next: next && chapterUrl(next),
      seriesUrl: `${SITE}/title/${manga.id}?tl=${a.translatedLanguage}`,
    },
  }
}

/** Neighbouring chapters in the same translation, by chapter number. */
async function neighbours(mangaId: string, id: string, lang: string) {
  const agg = await api(`/manga/${mangaId}/aggregate?translatedLanguage[]=${encodeURIComponent(lang)}`).catch(() => null)
  const chapters = Object.values<any>(agg?.volumes ?? {})
    .flatMap(v => Object.values<any>(v.chapters))
    .sort((x, y) => (parseFloat(x.chapter) || 0) - (parseFloat(y.chapter) || 0))
  const i = chapters.findIndex(c => c.id === id || c.others?.includes(id))
  return { prev: i > 0 ? (chapters[i - 1].id as string) : undefined, next: i >= 0 ? (chapters[i + 1]?.id as string | undefined) : undefined }
}

async function titlePage(id: string, lang = 'en'): Promise<CondensedPage> {
  const { data } = await api(`/manga/${id}`)
  const series = pickTitle(data.attributes.title)
  const feed = (lang: string) =>
    api(
      `/manga/${id}/feed?limit=${MAX_CHAPTERS}&order[chapter]=desc&includes[]=scanlation_group` +
        `&contentRating[]=safe&contentRating[]=suggestive&contentRating[]=erotica${lang}`,
    )
  // The requested language (English by default); otherwise whatever translations exist.
  let chapters: any[] = (await feed(`&translatedLanguage[]=${encodeURIComponent(lang)}`)).data
  if (!chapters.length) chapters = (await feed('')).data
  chapters = chapters.filter(c => !c.attributes.externalUrl)
  if (!chapters.length) throw new FetchError(`${series} has no chapters readable on MangaDex`, 422)

  const items: ListItem[] = chapters.map(c => {
    const group = c.relationships.find((r: any) => r.type === 'scanlation_group')?.attributes?.name
    return {
      title: chapterLabel(c.attributes),
      meta: [c.attributes.translatedLanguage, group && clean(group)].filter(Boolean).join(' · '),
      url: chapterUrl(c.id),
    }
  })
  const description = clean(data.attributes.description?.en ?? '')

  return {
    url: `${SITE}/title/${id}?tl=${lang}`,
    title: series,
    site: 'mangadex.org',
    sections: [
      {
        heading: series,
        blocks: [
          ...(description ? [{ type: 'p' as const, text: description }] : []),
          ...items.map(it => ({ type: 'li' as const, text: `${it.title} (${it.meta})` })),
        ],
      },
    ],
    links: items.map(it => ({ text: it.title, url: it.url! })),
    wordCount: description.split(/\s+/).length,
    deck: { kind: 'reference', screens: [{ layout: 'list', heading: `Chapters · ${series}`, items }] },
  }
}

// ── Strips ──────────────────────────────────────────────────────────────────

const atHomeCache = new Map<string, { at: number; value: Promise<{ files: string[]; base: string }> }>()

function atHome(chapterId: string) {
  const hit = atHomeCache.get(chapterId)
  if (hit && Date.now() - hit.at < AT_HOME_TTL_MS) return hit.value
  // data-saver pages are ~800px wide JPEGs: plenty for a 576px display.
  const value = api(`/at-home/server/${chapterId}`).then(r => ({
    files: r.chapter.dataSaver as string[],
    base: `${r.baseUrl}/data-saver/${r.chapter.hash}`,
  }))
  atHomeCache.set(chapterId, { at: Date.now(), value })
  value.catch(() => atHomeCache.delete(chapterId))
  return value
}

/** A page scaled to the display width, kept as raw greyscale. */
interface PreparedPage {
  px: Uint8Array
  h: number
}

const pageCache = new Map<string, Promise<PreparedPage>>()
const sourceCache = new Map<string, Promise<Buffer>>()

function sourceBytes(chapterId: string, page: number): Promise<Buffer> {
  const key = `${chapterId}:${page}`
  const hit = sourceCache.get(key)
  if (hit) return hit
  const value = pageBytes(chapterId, page)
  sourceCache.set(key, value)
  value.catch(() => sourceCache.delete(key))
  if (sourceCache.size > SOURCE_CACHE_MAX) sourceCache.delete(sourceCache.keys().next().value!)
  return value
}

let prefetching: string | null = null

/** Downloads every page of the chapter, two at a time; a newer chapter takes over. */
async function prefetchChapter(chapterId: string, pages: number) {
  if (prefetching === chapterId) return
  prefetching = chapterId
  let next = 0
  const worker = async () => {
    while (prefetching === chapterId && next < pages) {
      const page = next++
      await sourceBytes(chapterId, page).catch(() => {})
    }
  }
  await Promise.all(Array.from({ length: PREFETCH_CONCURRENCY }, worker))
  if (prefetching === chapterId) prefetching = null
}

function preparedPage(chapterId: string, page: number): Promise<PreparedPage> {
  const key = `${chapterId}:${page}`
  const hit = pageCache.get(key)
  if (hit) return hit
  const value = (async () => {
    const bytes = await sourceBytes(chapterId, page)
    // Descreen before shrinking: printed screentone is a fine dot pattern that
    // turns into blotchy moire at display width. A blur sized to the shrink
    // factor melts the dots into the grey they stand for; lines survive.
    const { width = SCREEN_W } = await sharp(bytes).metadata()
    const { data, info } = await sharp(bytes)
      .flatten({ background: '#ffffff' })
      .blur(Math.max(0.3, (width / SCREEN_W) * DESCREEN))
      .resize({ width: SCREEN_W })
      .toColourspace('b-w')
      .raw()
      .toBuffer({ resolveWithObject: true })
    return { px: new Uint8Array(data), h: info.height }
  })()
  pageCache.set(key, value)
  value.catch(() => pageCache.delete(key))
  if (pageCache.size > PAGE_CACHE_MAX) pageCache.delete(pageCache.keys().next().value!)
  return value
}

/**
 * MangaDex@Home cache nodes sometimes 404 a page (often on fresh uploads),
 * so fall back to the origin, then to a newly assigned node.
 */
async function pageBytes(chapterId: string, page: number): Promise<Buffer> {
  let home = await atHome(chapterId)
  const file = home.files[page]
  if (!file) throw new FetchError(`No page ${page + 1} in this chapter`, 404)
  try {
    return await safeFetchImage(`${home.base}/${file}`)
  } catch {
    const origin = home.base.replace(/^https:\/\/[^/]+/, 'https://uploads.mangadex.org')
    try {
      return await safeFetchImage(`${origin}/${file}`)
    } catch {
      atHomeCache.delete(chapterId)
      home = await atHome(chapterId)
      return safeFetchImage(`${home.base}/${file}`)
    }
  }
}

/** Nearest of the panel's 16 levels; line art stays crisp without dithering noise. */
function quantise(px: Uint8Array): Uint8Array {
  for (let i = 0; i < px.length; i++) px[i] = Math.round(Math.round(px[i] / STEP) * STEP)
  return px
}

// Bluetooth time is ~110 ms per compressed KB on the glasses, so tones are
// traded against bytes. With the page descreened first, 8 tones keep shading
// smooth: on test pages it cost 13-27 KB of LZ4 per screen against 10-21 KB
// for the old 6 tones + median filter, which turned screentone into blotches
// (16 tones was barely smoother at 21-35 KB). Dropping the dimmest tones
// keeps faint haze off the see-through view.
const TONES = 8
const DIMMEST_KEPT = 2
/** Descreen blur per unit of shrink (source width / display width). */
const DESCREEN = 0.55

/** Fewer, flatter tones that compress well. */
function simplify(px: Uint8Array): Uint8Array {
  const out = px.slice()
  for (let i = 0; i < out.length; i++) {
    const level = Math.round(out[i] / STEP)
    if (level < DIMMEST_KEPT) {
      out[i] = 0
      continue
    }
    const tone = Math.round((level / (LEVELS - 1)) * (TONES - 1))
    out[i] = Math.round(Math.round((tone / (TONES - 1)) * (LEVELS - 1)) * STEP)
  }
  return out
}

// Each swipe moves the page by two thirds of a screen, so reading feels like
// scrolling (the last third you read stays on top for context) while a page
// needs fewer swipes: every swipe re-sends the whole screen over Bluetooth.
const SCROLL_STEP = Math.round((SCREEN_H * 2) / 3)

/** Strip tops for a page: one scroll step apart, the last one flush with the page bottom. */
function stripTops(h: number): number[] {
  if (h <= SCREEN_H) return [0]
  const end = h - SCREEN_H
  const tops: number[] = []
  for (let t = 0; t < end; t += SCROLL_STEP) tops.push(t)
  // A final step shorter than a third of a step is merged into the previous one.
  if (tops.length > 1 && end - tops[tops.length - 1] < SCROLL_STEP / 3) tops.pop()
  tops.push(end)
  return tops
}

/** Contrast and paper/ink decision for every row of a page. */
interface RowTones {
  lo: Float32Array
  hi: Float32Array
  invert: Uint8Array
}

const toneCache = new WeakMap<Uint8Array, RowTones>()

/**
 * Tone mapping is decided per half-screen band of the page (judged over the
 * band and its neighbours) and blended between bands row by row. A row then
 * looks the same whichever scroll step shows it, so half-screen scrolling
 * doesn't make the area you just read change brightness. White paper would
 * glow as a solid slab on the see-through display, so bright bands are
 * inverted (paper goes dark, ink and art light up).
 */
function rowTones(px: Uint8Array, h: number): RowTones {
  const hit = toneCache.get(px)
  if (hit) return hit
  const band = SCROLL_STEP
  const bands = Math.max(1, Math.ceil(h / band))
  const bLo = new Float32Array(bands)
  const bHi = new Float32Array(bands)
  const bInv = new Uint8Array(bands)
  for (let k = 0; k < bands; k++) {
    const from = Math.max(0, (k - 1) * band)
    const to = Math.min(h, (k + 2) * band)
    const hist = new Array(256).fill(0)
    for (let i = from * SCREEN_W; i < to * SCREEN_W; i++) hist[px[i]]++
    const count = (to - from) * SCREEN_W
    const cut = count * 0.01
    let lo = 0
    let hi = 255
    for (let acc = 0; lo < 255 && acc + hist[lo] <= cut; lo++) acc += hist[lo]
    for (let acc = 0; hi > 0 && acc + hist[hi] <= cut; hi--) acc += hist[hi]
    const range = Math.max(1, hi - lo)
    // Judge brightness after stretching: a flat light-grey panel is "paper"
    // too, even though its raw mean is low.
    let sum = 0
    for (let v = 0; v < 256; v++) sum += hist[v] * Math.max(0, Math.min(255, ((v - lo) * 255) / range))
    bLo[k] = lo
    bHi[k] = lo + range
    bInv[k] = sum / count > BRIGHT_MEAN ? 1 : 0
  }
  const tones: RowTones = { lo: new Float32Array(h), hi: new Float32Array(h), invert: new Uint8Array(h) }
  for (let r = 0; r < h; r++) {
    const own = Math.min(bands - 1, Math.floor(r / band))
    const k0 = Math.max(0, Math.min(bands - 1, Math.floor((r - band / 2) / band)))
    const k1 = Math.min(bands - 1, k0 + 1)
    const t = Math.max(0, Math.min(1, (r - (k0 * band + band / 2)) / band))
    tones.invert[r] = bInv[own]
    if (bInv[k0] === bInv[k1]) {
      tones.lo[r] = bLo[k0] + (bLo[k1] - bLo[k0]) * t
      tones.hi[r] = bHi[k0] + (bHi[k1] - bHi[k0]) * t
    } else {
      tones.lo[r] = bLo[own]
      tones.hi[r] = bHi[own]
    }
  }
  toneCache.set(px, tones)
  return tones
}

/** One screen of page pixels using the page's row tones. Rows outside the page stay black. */
function screenPixels(px: Uint8Array, h: number, y: number): Uint8Array {
  const out = new Uint8Array(SCREEN_W * SCREEN_H)
  const tones = rowTones(px, h)
  const first = Math.max(0, -y)
  const last = Math.min(SCREEN_H, h - y)
  for (let row = first; row < last; row++) {
    const pr = y + row
    const lo = tones.lo[pr]
    const range = Math.max(1, tones.hi[pr] - lo)
    const inv = tones.invert[pr]
    for (let x = 0; x < SCREEN_W; x++) {
      const s = Math.max(0, Math.min(255, ((px[pr * SCREEN_W + x] - lo) * 255) / range))
      // Gamma > 1 sinks midtones so broad fills glow less while lines stay bright.
      const v = (inv ? 255 - s : s) / 255
      out[row * SCREEN_W + x] = Math.round(255 * Math.pow(v, GAMMA))
    }
  }
  return quantise(out)
}

async function tile(screen: Uint8Array, x: number, y: number): Promise<GlassImage> {
  const out = new Uint8Array(TILE_W * TILE_H)
  for (let row = 0; row < TILE_H; row++) {
    out.set(screen.subarray((y + row) * SCREEN_W + x, (y + row) * SCREEN_W + x + TILE_W), row * TILE_W)
  }
  const png = await sharp(Buffer.from(out), { raw: { width: TILE_W, height: TILE_H, channels: 1 } })
    .toColourspace('b-w')
    .png({ compressionLevel: 9 })
    .toBuffer()
  return { png: png.toString('base64'), w: TILE_W, h: TILE_H }
}

/** One screen of a page. `strip` -1 means the page's last strip. */
export async function mangaStrip(chapterId: string, page: number, strip: number): Promise<MangaStrip> {
  if (!/^[0-9a-f-]{36}$/i.test(chapterId)) throw new FetchError('Bad chapter id')
  const { px, h } = await preparedPage(chapterId, page)
  const tops = stripTops(h)
  const index = strip < 0 ? tops.length - 1 : Math.min(strip, tops.length - 1)
  const top = tops[index]
  // Short pages are centred vertically instead of pinned to the top.
  const y = h < SCREEN_H ? -Math.floor((SCREEN_H - h) / 2) : top
  const screen = simplify(screenPixels(px, h, y))
  const tiles = await Promise.all([
    tile(screen, 0, 0),
    tile(screen, TILE_W, 0),
    tile(screen, 0, TILE_H),
    tile(screen, TILE_W, TILE_H),
  ])
  return { page, strip: index, strips: tops.length, tiles }
}
