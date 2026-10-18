import Anthropic from '@anthropic-ai/sdk'
import type { CondensedPage, CondenseEvent, GlassImage, Link, Screen, SiteKind } from '../shared/types'
import { normalizeGlyphs, toPlainText, type ImageCandidate, type ProductCandidate } from './extract'
import { bigText, fetchGlassImage, type Treatment } from './images'
import { MODEL, refusalFallback } from './model'

// Smart mode: Claude decides what kind of site this is and lays the page out
// as a short deck of screens built for that kind of site (a headline card
// for news, boxed product rows for shopping, key cards for software docs…).
// Every length limit below matches what the glasses renderer can show; fields
// marked "scrolls" sit in a box the firmware scrolls, so they can run longer.
// The reply is streamed and each screen is sent on as soon as it is complete,
// so the wearer sees the first screen long before Claude finishes the deck.

const MAX_INPUT_CHARS = 120_000
const MAX_LINKS = 200
/** Items in one list screen; the glasses scroll it as a sliding 20-row window. */
const MAX_LIST_ITEMS = 60
/** A list is sent early once this many items are written, then replaced when complete. */
const EARLY_LIST_ITEMS = 8


// The screen union is too large for a structured-output grammar, so the
// format is spelled out here and the reply is validated in resolveScreens.
const FORMAT = `Reply with only a JSON object, no prose and no code fence:
{"kind": "news"|"shop"|"software"|"forum"|"live"|"reference", "screens": [screen, ...]}

Each screen is one of these (character limits in parentheses):
{"layout":"headline","headline":(80),"source":"outlet · site · date"(40),"summary":"what happened", scrolls (400),"image":lead photo number or -1}
{"layout":"points","heading":(28),"points":[3-8 facts (90 each)], scrolls,"quote_text":direct quote (100) or "","quote_by":who (40) or ""}
{"layout":"list","heading":(36),"items":[3-60 of {"title":(36),"meta":"price · rating" or "site · points · comments"(40),"link":link number or -1,"image":thumbnail number or -1}]}
{"layout":"product","title":(40),"subtitle":"category · brand"(40),"price":(12),"rating":as a number out of 5 with review count if shown, e.g. "4.5/5 (210)"(14) or "","stock":(16) or "","description":scrolls (400),"image":product image number or -1}
{"layout":"tiles","heading":(36),"intro":what it is, scrolls (200),"tiles":[exactly 4 of {"label":UPPERCASE (10),"value":(28)}],"image":image number or -1}
{"layout":"keys","heading":(30),"keys":[4-6 of {"key":shortcut or command (14),"action":(24)}]}
{"layout":"split","heading":(40),"verdict":the takeaway, scrolls (200),"left_label":e.g. AGREE (10),"left_text":(70),"right_label":e.g. PUSHBACK (10),"right_text":(70)}
{"layout":"stat","value":key number with unit e.g. 84°F (6),"lines":[1-6 context lines (28 each)], scrolls,"alert_title":(24) or "","alert_detail":(36) or "","tiles":[0-4 of {"label":(10),"value":(24)}]}
{"layout":"timeline","heading":(36),"events":[3-12 of {"date":(6),"text":(60)}] oldest first; 7 or fewer one-line events show as two columns, more scrolls}
{"layout":"compare","heading":(40),"columns":[2-3 of {"name":(12),"lines":[2-3 lines (22 each)]}]}
{"layout":"steps","heading":(30),"steps":[2-10 of {"title":(40),"detail":scrolls (240)}]}
{"layout":"tagged","heading":(36),"rows":[3-8 of {"tag":UPPERCASE (8),"text":(60)}]; 4 or fewer one-line rows show as tag chips, more scrolls}`

const SYSTEM = `You turn a web page into a short deck of screens for Even Realities G2 smart glasses: a 576x288 green-on-black heads-up display with one small font, no bold, no colors, about 9 lines of text. The wearer swipes between screens and glances at them while doing something else.

First decide what kind of site this is and what the wearer wants from it:
- news: catch up on a story. Start with a headline screen (use the lead photo), then 1-3 points screens on the key angles; add a quote when a strong one exists; use timeline for a dated sequence.
- shop: a listing/search page becomes a list (every distinct item, up to 60, with price, rating, its link and product thumbnail; the wearer scrolls through photos, so include them all rather than a sample); a product page becomes a product screen, then tiles for key specs and points for review themes.
- catalogs (Goodreads, stores, search results): when a <products> list is given, those items — with their image and link numbers — MUST be the list. Do not invent extras, drop covers, or replace it with a text-only list.
- software: a product or tool site, docs or a creative app. Start with tiles (what it does, with an image if there is a good one); keys for keyboard shortcuts or commands; steps for how-tos; compare for plans or editions.
- forum: a feed (Hacker News, Reddit, forums) becomes a list (title, then site, points and comment count; link each item to its discussion or article). A discussion thread becomes a split screen (the overall verdict, what people agree on vs the main pushback), then points for other notable sub-threads.
- live: weather, scores, prices or status. Start with a stat screen (the single most important number), then other screens as needed. For a restaurant, dining hall, cafe or store with opening hours, the stat value is the current status or next opening time (e.g. Open, 5 PM), the tiles are the hours with one per meal or period (label BREAKFAST, value 7:00-10:00 a.m.), and the lines hold the date, location and anything else worth a glance; then a list of what is being served today.
- reference: wikis and explainers. Start with points (the gist), then whichever fit: tiles (fact sheet), timeline, compare, tagged (pros/cons, risks, categories), steps.

Only cover what the wearer opened this page for. Leave out site chrome and fine print: terms of service, privacy and cookie notices, copyright lines, legal disclaimers, newsletter or account prompts, "related" or "you may also like" blocks, navigation and footer links, and ads. Treat these as invisible unless the page itself is a legal or policy page (then summarize what it means for the reader). If a section would not help someone glancing at the page, drop it.

Use 2 to 6 screens; fewer is better when the page is thin. Pick each layout for the content, not for variety. Use only facts that are on the page; never invent prices, numbers or quotes. Write plain, short, concrete phrases; drop filler and marketing. Respect every character limit; text over the limit is cut off on the glasses. A field marked "scrolls" can run longer than the screen: the wearer swipes through it a line at a time, so put the most important fact first and use the extra room only when the page has more worth saying. No markdown, no emoji. Use "·" to join short facts on one line.

Images and links are referenced by their [number] from the lists given with the page; use -1 when there is none. Only reference an image that really shows the subject (lead photo, product shot, item thumbnail), never logos or ads.

${FORMAT}`

// Raw model output: images and links are still numbers.
type RawScreen = Record<string, any> & { layout: string }
interface RawDeck {
  kind: SiteKind
  screens: RawScreen[]
}

const KINDS: SiteKind[] = ['news', 'shop', 'software', 'forum', 'live', 'reference']

function parseDeck(text: string): RawDeck | null {
  const start = text.indexOf('{')
  const end = text.lastIndexOf('}')
  if (start < 0 || end <= start) return null
  try {
    const raw = JSON.parse(text.slice(start, end + 1))
    if (!Array.isArray(raw?.screens)) return null
    return {
      kind: KINDS.includes(raw.kind) ? raw.kind : 'reference',
      screens: raw.screens.filter((x: unknown) => x && typeof (x as RawScreen).layout === 'string'),
    }
  } catch {
    return null
  }
}

/**
 * Pulls each finished screen object out of the deck JSON while it streams:
 * finds `"screens": [`, then tracks nesting (skipping strings) and parses
 * every top-level object in that array the moment it closes. A list screen is
 * also returned early (`partial`) once its first few items are written.
 */
class ScreenScanner {
  kind?: SiteKind
  private buf = ''
  private pos = -1
  private depth = 0
  private start = 0
  private inString = false
  private escaped = false
  private ended = false
  /** Objects closed inside the current screen's arrays (list items). */
  private items = 0

  feed(chunk: string): { raw: RawScreen; partial: boolean }[] {
    this.buf += chunk
    const out: { raw: RawScreen; partial: boolean }[] = []
    if (this.pos < 0) {
      const m = /"screens"\s*:\s*\[/.exec(this.buf)
      if (!m) return out
      const kind = /"kind"\s*:\s*"(\w+)"/.exec(this.buf)?.[1] as SiteKind
      this.kind = KINDS.includes(kind) ? kind : 'reference'
      this.pos = m.index + m[0].length
    }
    for (; this.pos < this.buf.length && !this.ended; this.pos++) {
      const ch = this.buf[this.pos]
      if (this.inString) {
        if (this.escaped) this.escaped = false
        else if (ch === '\\') this.escaped = true
        else if (ch === '"') this.inString = false
      } else if (ch === '"') {
        this.inString = true
      } else if (ch === '{' || ch === '[') {
        if (this.depth++ === 0) {
          this.start = this.pos
          this.items = 0
        }
      } else if (ch === '}' || ch === ']') {
        if (this.depth === 0) this.ended = true
        else if (--this.depth === 0) {
          const s = this.parse('')
          if (s) out.push({ raw: s, partial: false })
        } else if (ch === '}' && this.depth === 2 && ++this.items === EARLY_LIST_ITEMS) {
          // `items` is the list's last key, so closing it here gives a valid prefix.
          const s = this.parse(']}')
          if (s?.layout === 'list') out.push({ raw: s, partial: true })
        }
      }
    }
    return out
  }

  private parse(closing: string): RawScreen | null {
    try {
      const s = JSON.parse(this.buf.slice(this.start, this.pos + 1) + closing)
      return s && typeof s.layout === 'string' ? s : null
    } catch {
      return null
    }
  }
}

let client: Anthropic | null = null

// These messages end up on the glasses, so make the common setup failures actionable.
function friendlyError(err: unknown): never {
  if (err instanceof Anthropic.AuthenticationError || /authentication method/i.test(String(err))) {
    throw new Error('Smart view is not set up: add ANTHROPIC_API_KEY to the server .env')
  }
  if (err instanceof Anthropic.RateLimitError) throw new Error('Smart view is rate limited; try again shortly')
  throw err
}

function promptFor(page: CondensedPage, images: ImageCandidate[], links: Link[], products: ProductCandidate[], seededList: boolean) {
  const text = toPlainText(page)
  const truncated = text.length > MAX_INPUT_CHARS
  const imageList = images.length
    ? images.map(i => `[${i.id}] alt="${i.alt}" near="${i.context}"`).join('\n')
    : '(none)'
  const linkList = links.length
    ? links.slice(0, MAX_LINKS).map((l, i) => `[${i}] ${l.text.slice(0, 80)} -> ${l.url}`).join('\n')
    : '(none)'
  const productList = products.length
    ? products
        .map((p, i) => {
          const link = links.findIndex(l => l.url === p.url)
          return `[${i}] ${p.title} | ${p.byline} | ${p.price} | ${p.rating} | image=${p.imageId} | link=${link}`
        })
        .join('\n')
    : ''
  const task = seededList
    ? 'The illustrated list is already on the glasses. Add 1-3 extra screens if useful (tiles for the collection, points for notable titles). Do not output another list.'
    : 'Build the glasses deck for this page.'
  const content =
    `<page url="${page.url}" site="${page.site}" title="${page.title.replace(/"/g, "'")}"${truncated ? ' truncated="true"' : ''}>\n` +
    `${truncated ? text.slice(0, MAX_INPUT_CHARS) : text}\n</page>\n\n` +
    `<images>\n${imageList}\n</images>\n\n<links>\n${linkList}\n</links>` +
    (productList ? `\n\n<products>\n${productList}\n</products>` : '') +
    `\n\n${task}`
  return { content, truncated }
}

const clean = (s: unknown, max: number) => {
  const t = normalizeGlyphs(String(s ?? '')).replace(/\s+/g, ' ').trim()
  return t.length > max ? t.slice(0, max - 3).trimEnd() + '...' : t
}

/** "4 out of 5 stars (210)" / "4.5/5" -> "★★★★☆ 4.5 (210)"; bare star glyphs are dropped. */
function stars(rating: unknown): string {
  const t = clean(rating, 40)
  const m = /([0-5](?:\.\d)?)\s*(?:\/|out of)\s*5/i.exec(t)
  if (!m) return /^[★☆\s]*$/.test(t) ? '' : t.slice(0, 16)
  const n = parseFloat(m[1])
  const full = Math.min(5, Math.floor(n + 0.25))
  const count = /\(([\d,.]+[km]?)\)/i.exec(t)?.[1]
  return `${'★'.repeat(full)}${'☆'.repeat(5 - full)} ${m[1]}${count ? ` (${count})` : ''}`
}

// Image box sizes per layout (G2 image containers: 20-288 x 20-144).
const IMAGE_BOX: Record<string, [number, number, Treatment]> = {
  headline: [288, 144, 'auto'],
  tiles: [288, 144, 'auto'],
  product: [200, 144, 'auto'],
  list: [48, 72, 'auto'],
}

function itemMeta(p: ProductCandidate) {
  const rated = p.rating ? stars(p.rating) : ''
  if (/^[$€£]/.test(p.price)) return [p.price, rated].filter(Boolean).join(' · ')
  return [p.byline, rated, p.price].filter(Boolean).join(' · ')
}

/** Turns raw screens into wire screens, fetching their images; shares one image cache per deck. */
function screenResolver(page: CondensedPage, images: ImageCandidate[], links: Link[]) {
  const imageCache = new Map<string, Promise<GlassImage | undefined>>()
  const norm = (t: string) => t.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()
  const image = (n: unknown, layoutName: string, title = '') => {
    let cand = typeof n === 'number' && n >= 0 ? images[n] : undefined
    if (!cand && title) {
      const t = norm(title).slice(0, 24)
      if (t.length >= 6) cand = images.find(i => norm(i.alt).includes(t) || norm(i.context).includes(t))
    }
    if (!cand) return Promise.resolve(undefined)
    const [w, h, treat] = IMAGE_BOX[layoutName] ?? IMAGE_BOX.product
    const key = `${cand.src}@${w}x${h}`
    if (!imageCache.has(key)) imageCache.set(key, fetchGlassImage(cand.src, w, h, treat))
    return imageCache.get(key)!
  }
  // The model sometimes leaves link at -1 for items it did see; match by title.
  const link = (n: unknown, title: string) => {
    if (typeof n === 'number' && n >= 0 && links[n]) return links[n].url
    const t = norm(title).slice(0, 40)
    if (t.length < 6) return undefined
    return links.find(l => norm(l.text).startsWith(t) || (norm(l.text).length >= 12 && t.startsWith(norm(l.text))))?.url
  }

  return async (s: RawScreen): Promise<Screen[]> => {
    const r = await resolveOne(s)
    return r === null ? [] : Array.isArray(r) ? r : [r]
  }

  async function resolveOne(s: RawScreen): Promise<Screen | Screen[] | null> {
    switch (s.layout) {
      case 'headline':
        return {
          layout: 'headline',
          headline: clean(s.headline, 90),
          source: clean(s.source, 44),
          summary: clean(s.summary, 420),
          image: await image(s.image, 'headline'),
        }
      case 'points':
        return {
          layout: 'points',
          heading: clean(s.heading, 32),
          points: (s.points ?? []).slice(0, 8).map((p: string) => clean(p, 100)),
          quote: s.quote_text ? { text: clean(s.quote_text, 110), by: clean(s.quote_by, 44) } : undefined,
        }
      case 'list': {
        const items = await Promise.all(
          (s.items ?? []).slice(0, MAX_LIST_ITEMS).map(async (it: any) => {
            const title = clean(it.title, 40)
            return {
              title,
              meta: clean(it.meta, 44),
              url: link(it.link, String(it.title ?? '')),
              image: await image(it.image, 'list', title),
            }
          }),
        )
        return { layout: 'list', heading: clean(s.heading, 40), items }
      }
      case 'product':
        return {
          layout: 'product',
          title: clean(s.title, 44),
          subtitle: clean(s.subtitle, 44),
          price: clean(s.price, 14),
          rating: stars(s.rating),
          stock: clean(s.stock, 18),
          description: clean(s.description, 420),
          url: page.url,
          image: await image(s.image, 'product'),
        }
      case 'tiles':
        return {
          layout: 'tiles',
          heading: clean(s.heading, 40),
          intro: clean(s.intro, 210),
          tiles: (s.tiles ?? []).slice(0, 4).map((t: any) => ({ label: clean(t.label, 12), value: clean(t.value, 30) })),
          image: await image(s.image, 'tiles'),
        }
      case 'keys':
        return {
          layout: 'keys',
          heading: clean(s.heading, 34),
          keys: (s.keys ?? []).slice(0, 6).map((k: any) => ({ key: clean(k.key, 16), action: clean(k.action, 28) })),
        }
      case 'split':
        return {
          layout: 'split',
          heading: clean(s.heading, 44),
          verdict: clean(s.verdict, 210),
          left: { label: clean(s.left_label, 12), text: clean(s.left_text, 80) },
          right: { label: clean(s.right_label, 12), text: clean(s.right_text, 80) },
        }
      case 'stat': {
        const value = clean(s.value, 8)
        return {
          layout: 'stat',
          value,
          lines: (s.lines ?? []).slice(0, 6).map((l: string) => clean(l, 32)),
          alert: s.alert_title ? { title: clean(s.alert_title, 26), detail: clean(s.alert_detail, 40) } : undefined,
          tiles: (s.tiles ?? []).slice(0, 4).map((t: any) => ({ label: clean(t.label, 12), value: clean(t.value, 26) })),
          valueImage: value ? await bigText(value, 200, 100).catch(() => undefined) : undefined,
        }
      }
      case 'timeline':
        return {
          layout: 'timeline',
          heading: clean(s.heading, 40),
          events: (s.events ?? []).slice(0, 12).map((e: any) => ({ date: clean(e.date, 7), text: clean(e.text, 66) })),
        }
      case 'compare':
        return {
          layout: 'compare',
          heading: clean(s.heading, 44),
          columns: (s.columns ?? []).slice(0, 3).map((c: any) => ({
            name: clean(c.name, 14),
            lines: (c.lines ?? []).slice(0, 3).map((l: string) => clean(l, 26)),
          })),
        }
      case 'steps': {
        // One screen per step: on a HUD the current step should own the display.
        const steps = (s.steps ?? []).slice(0, 10)
        return steps.map((st: any, i: number) => ({
          layout: 'step' as const,
          heading: clean(s.heading, 34),
          index: i + 1,
          total: steps.length,
          title: clean(st.title, 44),
          detail: clean(st.detail, 250),
          next: steps[i + 1] ? clean(steps[i + 1].title, 44) : undefined,
        }))
      }
      case 'tagged':
        return {
          layout: 'tagged',
          heading: clean(s.heading, 40),
          rows: (s.rows ?? []).slice(0, 8).map((r: any) => ({ tag: clean(r.tag, 9), text: clean(r.text, 66) })),
        }
      default:
        return null
    }
  }
}

/**
 * Streams the deck through `emit`: the kind once the reply names it, then
 * screens in order as each one completes (with its images). Resolves once the
 * last screen is out; throws only when no screen could be built.
 */
function listingHeading(page: CondensedPage) {
  const parts = page.title.split(':').map(s => s.trim()).filter(p => p && !/^amazon\.com$/i.test(p))
  return parts.slice(-2).join(' · ') || page.title
}

export async function buildDeck(
  page: CondensedPage,
  images: ImageCandidate[],
  links: Link[],
  emit: (event: CondenseEvent) => void,
  products: ProductCandidate[] = [],
): Promise<{ truncated: boolean }> {
  client ??= new Anthropic()
  const resolve = screenResolver(page, images, links)

  let sent = 0
  let kind: SiteKind | undefined
  /** Index of an early list screen waiting for its complete version. */
  let early: number | null = null
  const seededList = products.length >= 3
  // Screens resolve in parallel (image fetches) but go out in reply order.
  let queue = Promise.resolve()
  const enqueue = (raw: RawScreen, partial = false) => {
    const screens = resolve(raw).catch(() => [])
    queue = queue.then(async () => {
      const ready = await screens
      if (seededList && ready[0]?.layout === 'list') return
      if (early !== null && !partial) {
        if (ready[0]) emit({ replace: early, screen: ready[0] })
        early = null
        return
      }
      if (!ready.length) return
      if (partial) early = sent
      sent += ready.length
      emit({ screens: ready })
    })
  }

  if (seededList) {
    const shoppy = products.filter(p => /^[$€£]/.test(p.price)).length >= products.length / 2
    kind = shoppy ? 'shop' : 'reference'
    emit({ kind })
    const ready = await resolve({
      layout: 'list',
      heading: listingHeading(page),
      items: products.map(p => ({
        title: p.title,
        meta: itemMeta(p),
        link: links.findIndex(l => l.url === p.url),
        image: p.imageId,
      })),
    })
    if (ready.length) {
      sent += ready.length
      emit({ screens: ready })
    }
  }

  const { content, truncated } = promptFor(page, images, links, products, seededList)
  const setKind = (k: SiteKind) => {
    if (k === kind) return
    kind = k
    emit({ kind })
  }

  for (let attempt = 0; attempt < 2; attempt++) {
    // Seeded shop lists already have a screen; still ask once for extra cards.
    if (sent && !seededList) break
    if (attempt && (sent || seededList)) break
    const scanner = new ScreenScanner()
    let seen = 0
    const stream = client.beta.messages.stream({
      model: MODEL,
      max_tokens: 16000,
      ...refusalFallback,
      output_config: { effort: 'low' },
      system: SYSTEM,
      messages: [{ role: 'user', content }],
    })
    stream.on('text', delta => {
      const found = scanner.feed(delta)
      if (scanner.kind) setKind(scanner.kind)
      for (const { raw, partial } of found) {
        if (!partial) seen++
        enqueue(raw, partial)
      }
    })
    const response = await stream.finalMessage().catch(err => {
      if (sent) {
        console.warn('[deck] extra screens failed:', err instanceof Error ? err.message : err)
        return null
      }
      friendlyError(err)
    })
    if (!response) break

    // A reply that didn't stream in the expected shape may still parse whole.
    const text = response.content.flatMap(b => (b.type === 'text' ? [b.text] : [])).join('')
    const whole = parseDeck(text)
    if (whole) setKind(whole.kind)
    if (!seen && whole) whole.screens.forEach(raw => enqueue(raw))
    await queue

    if (sent) break
    if (response.stop_reason === 'refusal') throw new Error('The model declined to process this page')
    if (response.stop_reason === 'max_tokens') throw new Error('Smart view was cut off on this page')
  }
  if (!sent) throw new Error('Smart view could not lay out this page')
  return { truncated }
}
