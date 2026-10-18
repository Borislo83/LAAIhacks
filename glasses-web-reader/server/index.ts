import { serve } from '@hono/node-server'
import { Hono } from 'hono'
import { cors } from 'hono/cors'
import { stream } from 'hono/streaming'
import type { CondensedPage, CondenseEvent } from '../shared/types'
import { extract } from './extract'
import { FetchError, safeFetch } from './safe-fetch'
import { buildDeck } from './deck'
import { renderPage } from './render'
import { isMangaDex, mangaPage, mangaStrip, type MangaStyle } from './manga'
import type { Extracted } from './extract'

// Condense server: GET /api/condense?url=<page>
//
// The Even app WebView enforces CORS, so the glasses app cannot fetch
// arbitrary sites itself. This server does the fetch, strips the page down
// to text, and asks Claude to lay it out as screens suited to the kind of
// site (news, shop, software, forum, live info, reference). The reply is
// NDJSON (CondenseEvent per line) so screens reach the glasses as they're built.

const PORT = Number(process.env.PORT ?? 8787)
const CACHE_TTL_MS = 10 * 60_000
const CACHE_MAX = 100
// Below this the fast path probably hit a bot check or a JS-only shell.
const THIN_WORDS = 150
// Interstitials that render as a short "page" of their own.
const BOT_WALL =
  /prove your humanity|human verification|verify (that )?you are (a )?human|are you a robot|just a moment|attention required|access denied|captcha|unusual traffic/i

/**
 * One condense in progress or finished. Events are kept so a later request
 * for the same page (or one arriving mid-build) replays them, then follows live.
 */
class Run {
  private events: (CondenseEvent & { status?: number })[] = []
  private wake = new Set<() => void>()
  readonly ok: Promise<boolean>

  constructor(work: (emit: (e: CondenseEvent) => void) => Promise<{ truncated?: boolean } | void>) {
    this.ok = work(e => this.push(e)).then(
      r => (this.push({ done: true, ...r }), true),
      err => {
        const status = err instanceof FetchError ? err.status : 500
        this.push({ error: err instanceof Error ? err.message : String(err), status })
        return false
      },
    )
  }

  private push(e: CondenseEvent & { status?: number }) {
    this.events.push(e)
    for (const w of this.wake) w()
  }

  async *read() {
    for (let i = 0; ; ) {
      while (i < this.events.length) {
        const e = this.events[i++]
        yield e
        if ('done' in e || 'error' in e) return
      }
      await new Promise<void>(resolve => {
        const w = () => (this.wake.delete(w), resolve())
        this.wake.add(w)
      })
    }
  }
}

const cache = new Map<string, { at: number; run: Run }>()

function cached(key: string, make: () => Run): Run {
  const hit = cache.get(key)
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.run
  const run = make()
  cache.set(key, { at: Date.now(), run })
  void run.ok.then(ok => ok || cache.delete(key))
  if (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value!)
  return run
}

const extracted = new Map<string, { at: number; value: Promise<Extracted> }>()

function fetchAndExtract(url: string): Promise<Extracted> {
  const hit = extracted.get(url)
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.value
  const value = readPage(url)
  extracted.set(url, { at: Date.now(), value })
  value.catch(() => extracted.delete(url))
  if (extracted.size > CACHE_MAX) extracted.delete(extracted.keys().next().value!)
  return value
}

/** Plain fetch first; headless Chromium when the site refused it or sent an empty shell. */
async function readPage(url: string): Promise<Extracted> {
  let fast: Extracted | null = null
  try {
    const doc = await safeFetch(url)
    fast = extract(doc.body, doc.finalUrl, doc.contentType)
    if (fast.wordCount >= THIN_WORDS) return fast
  } catch (err) {
    // 502 = the site refused or failed us; anything else (bad URL, private
    // address, not HTML) a browser will not fix.
    if (!(err instanceof FetchError) || err.status !== 502) throw err
    console.warn(`[condense] fast fetch failed for ${url}: ${err.message}`)
  }
  try {
    const doc = await renderPage(fast?.url ?? url)
    const rendered = extract(doc.body, doc.finalUrl, doc.contentType)
    if (BOT_WALL.test(rendered.title) && rendered.wordCount < THIN_WORDS) {
      throw new Error(`bot check: ${rendered.title}`)
    }
    return fast && fast.wordCount >= rendered.wordCount ? fast : rendered
  } catch (err) {
    console.warn(`[condense] browser render failed for ${url}: ${err instanceof Error ? err.message : err}`)
    if (fast && !BOT_WALL.test(fast.title)) return fast
    throw new FetchError(`Couldn't load ${hostOf(url)} - it blocks automated readers`, 502)
  }
}

const hostOf = (url: string) => {
  try {
    return new URL(/^[a-z]+:\/\//i.test(url) ? url : `https://${url}`).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}

/** The wire format never carries image/product candidates (they are server-side input). */
function forWire({ images: _images, allLinks: _allLinks, products: _products, ...page }: Extracted): CondensedPage {
  return page
}

export const app = new Hono()

app.use('/api/*', cors({ origin: process.env.CORS_ORIGIN ?? '*' }))

app.get('/api/health', c => c.json({ ok: true }))

// Timing reports from the glasses app (page-turn latency on real hardware).
app.post('/api/telemetry', async c => {
  const body = await c.req.text()
  if (body.length < 4000) console.log(`[telemetry] ${body}`)
  return c.body(null, 204)
})

app.get('/api/condense', async c => {
  const url = c.req.query('url')?.trim()
  if (!url) return c.json({ error: 'Missing ?url=' }, 400)

  const run = isMangaDex(url)
    ? cached(`manga:${url}`, () => new Run(async emit => emit({ page: await mangaPage(url) })))
    : cached(`smart:${url}`, () =>
        new Run(async emit => {
          const ex = await fetchAndExtract(url)
          const page = forWire(ex)
          if (!page.sections.length) {
            throw new FetchError(`Couldn't read ${hostOf(url)} - it blocks automated readers or needs a login`, 422)
          }
          emit({ page })
          const { truncated } = await buildDeck(page, ex.images, ex.allLinks, emit, ex.products)
          return truncated ? { truncated } : {}
        }),
      )

  // Failures before the page is read get a real status; later ones arrive in the stream.
  const events = run.read()
  const first = (await events.next()).value!
  if ('error' in first) {
    console.error(`[condense] ${url}: ${first.error}`)
    return c.json({ error: first.error }, (first.status ?? 500) as 400)
  }
  c.header('content-type', 'application/x-ndjson; charset=utf-8')
  c.header('cache-control', 'no-cache')
  return stream(c, async s => {
    await s.write(JSON.stringify(first) + '\n')
    for await (const { status: _status, ...e } of events) {
      if ('error' in e) console.error(`[condense] ${url}: ${e.error}`)
      await s.write(JSON.stringify(e) + '\n')
    }
  })
})

// GET /api/manga/strip?chapter=<id>&page=<0-based>&strip=<n, or -1 for the last>&style=natural|lines
app.get('/api/manga/strip', async c => {
  const chapter = c.req.query('chapter') ?? ''
  const page = Number(c.req.query('page') ?? 0)
  const strip = Number(c.req.query('strip') ?? 0)
  const style: MangaStyle = c.req.query('style') === 'lines' ? 'lines' : 'natural'
  if (!Number.isInteger(page) || page < 0 || !Number.isInteger(strip)) return c.json({ error: 'Bad page or strip' }, 400)
  try {
    return c.json(await mangaStrip(chapter, page, strip, style))
  } catch (err) {
    const status = err instanceof FetchError ? err.status : 500
    const message = err instanceof Error ? err.message : String(err)
    console.error(`[manga] ${chapter} p${page} s${strip}: ${message}`)
    return c.json({ error: message }, status as 400)
  }
})

serve({ fetch: app.fetch, port: PORT }, info => {
  console.log(`condense server listening on http://localhost:${info.port}`)
})
