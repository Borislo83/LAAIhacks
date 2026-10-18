import { chromium, type Browser } from 'playwright'
import { assertPublic, type FetchedDoc } from './safe-fetch'

// Headless Chromium fallback for pages a plain fetch cannot read: bot checks
// that need JavaScript to pass (AWS WAF on Goodreads/IMDb) and apps that
// build their content client-side (Reddit, store pages). Only used after the
// fast path comes back blocked or nearly empty.

const NAV_TIMEOUT_MS = 20_000
const SETTLE_MS = 6_000
const IDLE_CLOSE_MS = 5 * 60_000
const MAX_HTML = 4 * 1024 * 1024
const MAX_PAGES = 2

const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) ' +
  'Chrome/130.0 Safari/537.36'

let browser: Promise<Browser> | null = null
let idleTimer: NodeJS.Timeout | undefined
let active = 0
const waiting: (() => void)[] = []

function getBrowser(): Promise<Browser> {
  clearTimeout(idleTimer)
  browser ??= chromium.launch({ headless: true }).catch(err => {
    browser = null
    throw err
  })
  return browser
}

function scheduleIdleClose() {
  clearTimeout(idleTimer)
  idleTimer = setTimeout(() => {
    if (active) return
    browser?.then(b => b.close()).catch(() => {})
    browser = null
  }, IDLE_CLOSE_MS)
}

async function slot<T>(fn: () => Promise<T>): Promise<T> {
  if (active >= MAX_PAGES) await new Promise<void>(r => waiting.push(r))
  active++
  try {
    return await fn()
  } finally {
    active--
    waiting.shift()?.()
    if (!active) scheduleIdleClose()
  }
}

export function renderPage(input: string): Promise<FetchedDoc> {
  return slot(async () => {
    const context = await (await getBrowser()).newContext({
      userAgent: UA,
      locale: 'en-US',
      viewport: { width: 1280, height: 1600 },
    })
    try {
      // Same SSRF rule as the fast path, applied to every request the page makes.
      await context.route('**/*', async route => {
        const req = route.request()
        if (['image', 'media', 'font'].includes(req.resourceType())) return route.abort()
        try {
          await assertPublic(new URL(req.url()))
          await route.continue()
        } catch {
          await route.abort()
        }
      })
      const page = await context.newPage()
      await page.goto(input, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS })
      // Bot checks reload the page once solved; client apps fetch after load.
      await page.waitForLoadState('networkidle', { timeout: SETTLE_MS }).catch(() => {})
      // Lazy grids (store pages, feeds) only render rows that scroll into view.
      await page
        .evaluate(async () => {
          for (let i = 0; i < 6; i++) {
            window.scrollBy(0, window.innerHeight)
            await new Promise(r => setTimeout(r, 250))
          }
        })
        .catch(() => {})
      await page.waitForLoadState('networkidle', { timeout: 2_000 }).catch(() => {})
      const html = (await page.content()).slice(0, MAX_HTML)
      return { finalUrl: page.url(), contentType: 'text/html', body: html }
    } finally {
      await context.close().catch(() => {})
    }
  })
}
