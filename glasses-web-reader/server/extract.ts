import { JSDOM, VirtualConsole } from 'jsdom'
import { Readability } from '@mozilla/readability'
import type { Block, CondensedPage, Link, Section } from '../shared/types'

// Turns raw HTML into a small, structured, text-only document that fits the
// G2's constraints: one font, no styling, no images, characters outside the
// firmware font silently dropped.

const BLOCK_TAGS = new Set([
  'ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'DD', 'DETAILS', 'DIV', 'DL', 'DT', 'FIELDSET',
  'FIGCAPTION', 'FIGURE', 'FOOTER', 'FORM', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'HEADER', 'HR',
  'LI', 'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION', 'TABLE', 'TBODY', 'THEAD', 'TR', 'UL',
])
const SKIP_TAGS = new Set([
  'SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'CANVAS', 'IFRAME', 'VIDEO', 'AUDIO', 'BUTTON', 'INPUT',
  'SELECT', 'TEXTAREA', 'IMG', 'PICTURE', 'SOURCE', 'TEMPLATE', 'OBJECT', 'EMBED',
])
// Stripped only in the fallback path, where Readability did not already remove chrome.
const CHROME_SELECTOR =
  'nav, header, footer, aside, form, [role=navigation], [role=banner], [role=contentinfo], ' +
  '[aria-hidden=true], .cookie, .cookies, .newsletter, .ad, .ads, .advert, .sidebar'

// Wiki/CMS furniture that reads as gibberish once flattened to text. Removed
// before Readability runs, because Readability drops class names.
const NOISE_SELECTOR =
  '.infobox, .navbox, .vertical-navbox, .sidebar, .metadata, .hatnote, .ambox, .mw-editsection, ' +
  'sup.reference, .reference, .reflist, .references, .mw-references-wrap, .noprint, .toc, #toc, ' +
  '.sr-only, .visually-hidden, [hidden]'

const BOILERPLATE =
  /^(advertisement|sponsored|share( this)?|tweet|subscribe|sign up|log in|related( articles)?|read more|click here|follow us|cookie|accept|skip to)/i

// Fine print that slips past Readability: legal lines and footer sections.
const LEGAL_LINE =
  /(©|\(c\)\s*\d{4}|all rights reserved|by (clicking|continuing|using|signing)[^.]{0,80}\b(agree|accept|consent)|(terms of (service|use)|privacy (policy|notice)|cookie (policy|settings|preferences))\s*[|·•]?\s*$)/i
const FINE_PRINT_HEADING =
  /^(terms( of (service|use))?|terms (and|&) conditions|privacy( policy| notice)?|cookies?( policy| settings| notice)?|legal( notice)?|disclaimer|copyright|imprint|newsletter|subscribe|related( (articles|stories|posts))?|you (may|might) also like|more from|recommended( for you)?|follow us|footer)\b/i
// Opening a terms or privacy page on purpose should still show it.
const LEGAL_PAGE = /\/(terms|tos|legal|privacy|cookies?|policies|policy|disclaimer|imprint)\b/i

const MAX_BLOCKS = 600
const MAX_CODE_LINES = 15
const MAX_LINKS = 40
// Smart mode needs item links deep in listings (products, threads), not just the first few.
const MAX_ALL_LINKS = 250

/** Map typography the G2 font may lack onto plain equivalents. */
export function normalizeGlyphs(s: string): string {
  return s
    .replace(/[  -   　]/g, ' ')
    .replace(/[​-‍⁠﻿­]/g, '')
    .replace(/[‘’‚′]/g, "'")
    .replace(/[“”„″]/g, '"')
    .replace(/[–—―−]/g, '-')
    .replace(/…/g, '...')
    .replace(/[\u2192\u27F6\u21D2]/g, '->')
    .replace(/[\u2190\u27F5\u21D0]/g, '<-')
    .replace(/\s*[\u00B6\u00A7]$/, '')
    .replace(/[•●◦‣]/g, '-')
    // ★ is Extended_Pictographic but the G2 font has it (ratings pair it with ☆).
    .replace(/(?!★)\p{Extended_Pictographic}️?/gu, '')
}

// Wikipedia-style reference markers mean nothing without hyperlinks.
const CITATION = /\[(?:\d+|[a-z]|note \d+|citation needed|edit|update)\]/gi

const clean = (s: string) => normalizeGlyphs(s).replace(CITATION, '').replace(/\s+/g, ' ').trim()

class Builder {
  sections: Section[] = [{ heading: '', blocks: [] }]
  private inline = ''
  private count = 0

  get full() {
    return this.count >= MAX_BLOCKS
  }

  heading(text: string) {
    this.flush()
    const t = clean(text)
    if (!t) return
    const cur = this.sections[this.sections.length - 1]
    if (!cur.blocks.length && !cur.heading) cur.heading = t
    else this.sections.push({ heading: t, blocks: [] })
  }

  block(type: Block['type'], text: string) {
    this.flush()
    this.push(type, text)
  }

  addInline(text: string) {
    this.inline += text
  }

  flush() {
    if (this.inline.trim()) this.push('p', this.inline)
    this.inline = ''
  }

  private push(type: Block['type'], raw: string) {
    if (this.full) return
    const text = type === 'code' ? trimCode(raw) : clean(raw)
    if (!text || (type !== 'code' && text.length < 3)) return
    if (type === 'p' && text.length < 60 && BOILERPLATE.test(text)) return
    const blocks = this.sections[this.sections.length - 1].blocks
    if (blocks.length && blocks[blocks.length - 1].text === text) return
    blocks.push({ type, text })
    this.count++
  }
}

function trimCode(raw: string): string {
  const lines = normalizeGlyphs(raw).replace(/\t/g, '  ').split('\n').map(l => l.trimEnd())
  while (lines.length && !lines[0].trim()) lines.shift()
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop()
  if (lines.length > MAX_CODE_LINES) {
    const more = lines.length - MAX_CODE_LINES
    lines.length = MAX_CODE_LINES
    lines.push(`... (${more} more lines)`)
  }
  return lines.join('\n')
}

const STAR_WORDS: Record<string, number> = { one: 1, two: 2, three: 3, four: 4, five: 5 }

/** Ratings are often drawn with CSS (class="star-rating Three") or only labelled for screen readers. */
function ratingOf(el: Element): string | null {
  const label = el.getAttribute('aria-label') || el.getAttribute('title') || ''
  if (/\b(star|rating|out of)\b/i.test(label) && !el.textContent?.trim()) return ` ${label} `
  const cls = /\bstar-rating\s+(one|two|three|four|five)\b/i.exec(el.getAttribute('class') ?? '')
  return cls ? ` ${STAR_WORDS[cls[1].toLowerCase()]} out of 5 stars ` : null
}

/** Like textContent, but keeps a space where a <br>, cell or block boundary was. */
function textOf(node: Node): string {
  if (node.nodeType === 3) return node.textContent ?? ''
  if (node.nodeType !== 1) return ''
  const el = node as Element
  if (el.tagName === 'BR') return ' '
  const rating = ratingOf(el)
  if (rating) return rating
  if (SKIP_TAGS.has(el.tagName)) return ''
  let out = ''
  for (const child of Array.from(el.childNodes)) out += textOf(child)
  return BLOCK_TAGS.has(el.tagName) || el.tagName === 'TD' || el.tagName === 'TH' ? ` ${out} ` : out
}

function hasBlockChild(el: Element): boolean {
  for (const child of Array.from(el.children)) {
    if (BLOCK_TAGS.has(child.tagName) || hasBlockChild(child)) return true
  }
  return false
}

function walk(node: Node, b: Builder) {
  if (b.full) return
  if (node.nodeType === 3) {
    b.addInline(node.textContent ?? '')
    return
  }
  if (node.nodeType !== 1) return
  const el = node as Element
  const tag = el.tagName
  if (SKIP_TAGS.has(tag)) return

  switch (tag) {
    case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6':
      b.heading(textOf(el))
      return
    case 'BR':
      b.flush()
      return
    case 'HR':
      b.flush()
      return
    case 'PRE':
      b.block('code', textOf(el))
      return
    case 'BLOCKQUOTE':
      if (!hasBlockChild(el)) b.block('quote', textOf(el))
      else {
        b.flush()
        for (const p of Array.from(el.querySelectorAll('p'))) b.block('quote', textOf(p))
      }
      return
    case 'UL': case 'OL': {
      b.flush()
      let n = 1
      for (const li of Array.from(el.children)) {
        if (li.tagName !== 'LI') continue
        // Nested lists flatten into their own items.
        const nested = Array.from(li.querySelectorAll(':scope > ul, :scope > ol'))
        nested.forEach(x => x.remove())
        const prefix = tag === 'OL' ? `${n++}. ` : ''
        b.block('li', prefix + (textOf(li)))
        nested.forEach(x => walk(x, b))
      }
      return
    }
    case 'TABLE':
      b.flush()
      for (const row of Array.from(el.querySelectorAll('tr'))) {
        const cells = Array.from(row.children).map(c => clean(textOf(c))).filter(Boolean)
        if (!cells.length) continue
        // "1." rank cells read better attached to the row than as a column.
        const lead = /^\d+\.?$/.test(cells[0]) && cells.length > 1 ? `${cells.shift()} ` : ''
        b.block('li', lead + cells.join(' | '))
      }
      return
    case 'FIGURE':
      // Captions without the image are noise on a text-only display.
      return
    case 'DT':
      b.block('p', textOf(el))
      return
    case 'DD':
      b.block('li', textOf(el))
      return
  }

  if (BLOCK_TAGS.has(tag)) {
    if (!hasBlockChild(el)) {
      b.block('p', textOf(el))
    } else {
      b.flush()
      for (const child of Array.from(el.childNodes)) walk(child, b)
      b.flush()
    }
    return
  }
  // Inline element (a, span, strong, ...) containing block children is malformed but common.
  if (hasBlockChild(el)) {
    for (const child of Array.from(el.childNodes)) walk(child, b)
  } else {
    b.addInline(textOf(el))
  }
}

function collectLinks(root: ParentNode, base: string, into: Map<string, Link>, max = MAX_LINKS) {
  for (const a of Array.from(root.querySelectorAll('a[href]'))) {
    if (into.size >= max) return
    // Product tiles often link only an image: fall back to its alt text or the link's label.
    const text = clean(
      a.textContent?.trim()
        ? a.textContent
        : a.getAttribute('aria-label') || a.getAttribute('title') || a.querySelector('img[alt]')?.getAttribute('alt') || '',
    ).replace(/^image of\s+/i, '')
    if (text.length < 4) continue
    let href: URL
    try {
      href = new URL(a.getAttribute('href')!, base)
    } catch {
      continue
    }
    if (href.protocol !== 'http:' && href.protocol !== 'https:') continue
    href.hash = ''
    const key = href.toString()
    if (key === base || into.has(key)) continue
    into.set(key, { text, url: key })
  }
}

function finalize(sections: Section[], title: string): Section[] {
  const t = title.toLowerCase()
  const out = sections
    .map(s => ({ ...s, heading: s.heading.toLowerCase() === t ? '' : s.heading }))
    .filter(s => s.blocks.length)
  if (out.length && !out[0].heading) out[0].heading = 'Start'
  return out
}

const words = (sections: Section[]) =>
  sections.reduce((n, s) => n + s.blocks.reduce((m, b) => m + b.text.split(/\s+/).length, 0), 0)

export interface ImageCandidate {
  id: number
  src: string
  alt: string
  /** Nearby text, so the model can tell a product shot from a headline photo. */
  context: string
}

/** One catalog tile (product, book, …) so listing pages keep name, photo and link together. */
export interface ProductCandidate {
  title: string
  /** Author, brand, or other byline. */
  byline: string
  /** Sale price, year, or other short fact. */
  price: string
  rating: string
  url: string
  imageId: number
}

const MAX_IMAGES = 60
const JUNK_IMAGE = /logo|icon|sprite|avatar|pixel|tracking|badge|spinner|placeholder|prime|nophoto|\.svg(\?|$)|\.gif(\?|$)|^data:/i
const HIDDEN_IMAGE = /hidden|secondary|sr-only|visually-hidden|prime/i
const PRODUCT_CARD_SEL = [
  '[data-testid="product-grid-item"]',
  'li[class*="ProductGridItem"]',
  '[data-component-type="s-search-result"]',
  '[data-testid*="product-card"]',
  '.product-card',
  '.product-item',
  'li[class*="product-card"]',
  'li[class*="productCard"]',
].join(', ')

/** Goodreads (and similar CDNs) serve a 50px derivative in the page; ask for a cover that will actually read on glass. */
function upgradeThumbUrl(src: string): string {
  return src.replace(/\._S[XY]\d+(?:_S[XY]\d+)?_/g, '._SY300_')
}

function pickSrc(img: Element): string | null {
  const dyn = img.getAttribute('data-a-dynamic-image')
  if (dyn) {
    try {
      const map = JSON.parse(dyn) as Record<string, number[]>
      const best = Object.entries(map).sort((a, b) => (b[1]?.[0] ?? 0) - (a[1]?.[0] ?? 0))[0]
      if (best?.[0]) return best[0]
    } catch {
      /* not JSON */
    }
  }
  const srcset = img.getAttribute('srcset') || img.getAttribute('data-srcset')
  if (srcset) {
    // Largest candidate in a srcset ("url 320w, url 640w").
    const best = srcset
      .split(',')
      .map(part => part.trim().split(/\s+/))
      .sort((a, b) => parseFloat(b[1] ?? '0') - parseFloat(a[1] ?? '0'))[0]
    if (best?.[0]) return best[0]
  }
  return img.getAttribute('data-src') || img.getAttribute('src')
}

function addImage(
  out: ImageCandidate[],
  seen: Set<string>,
  raw: string | null,
  alt: string,
  context: string,
  base: string,
  cap = MAX_IMAGES,
): number {
  if (!raw) return -1
  let src: string
  try {
    src = new URL(raw, base).toString()
  } catch {
    return -1
  }
  src = upgradeThumbUrl(src)
  if (!/^https?:/.test(src) || JUNK_IMAGE.test(src) || JUNK_IMAGE.test(alt)) return -1
  const hit = out.find(i => i.src === src)
  if (hit) return hit.id
  if (out.length >= cap || seen.has(src)) return -1
  seen.add(src)
  const id = out.length
  out.push({ id, src, alt: clean(alt).replace(/^image of\s+/i, '').slice(0, 100), context: clean(context).slice(0, 140) })
  return id
}

function collectImages(doc: Document, base: string): { images: ImageCandidate[]; seen: Set<string> } {
  const images: ImageCandidate[] = []
  const seen = new Set<string>()
  const og = doc.querySelector('meta[property="og:image"], meta[name="twitter:image"]')?.getAttribute('content')
  addImage(images, seen, og ?? null, 'page preview image (og:image)', doc.title, base)
  for (const img of Array.from(doc.body?.querySelectorAll('img') ?? [])) {
    const w = Number(img.getAttribute('width') || 0)
    const h = Number(img.getAttribute('height') || 0)
    if ((w && w < 40) || (h && h < 40)) continue
    const cls = `${img.getAttribute('class') ?? ''} ${img.parentElement?.getAttribute('class') ?? ''}`
    if (HIDDEN_IMAGE.test(cls)) continue
    const holder =
      img.closest('[data-testid="product-grid-item"], [data-asin], article, li, figure') ?? img.parentElement
    addImage(images, seen, pickSrc(img), img.getAttribute('alt') ?? '', textOf(holder ?? img), base)
  }
  return { images, seen }
}

const PRICE_RE = /\$\s?\d[\d,]*(?:\.\d{2})?/
const RATING_RE = /([0-5](?:\.\d)?)\s*(?:out of\s*5|\/\s*5)/i

function firstAbsUrl(el: Element, base: string, pred: (url: URL) => boolean): string {
  for (const a of Array.from(el.querySelectorAll('a[href]'))) {
    try {
      const url = new URL(a.getAttribute('href')!, base)
      url.hash = ''
      if ((url.protocol === 'http:' || url.protocol === 'https:') && pred(url)) return url.toString()
    } catch {
      /* skip */
    }
  }
  return ''
}

function collectProducts(doc: Document, base: string, images: ImageCandidate[], seen: Set<string>): ProductCandidate[] {
  const cards = Array.from(doc.querySelectorAll(PRODUCT_CARD_SEL)).filter(
    (el, i, all) => !all.some((other, j) => j !== i && other.contains(el)),
  )
  const out: ProductCandidate[] = []
  const seenUrl = new Set<string>()
  for (const card of cards) {
    if (out.length >= 60) break
    // Amazon labels its star-rating link too, so the h2 title wins and rating labels never count.
    const overlay =
      card.querySelector('h2[aria-label]') ??
      card.querySelector('a[aria-label]:not([aria-label*="out of 5"]), a[title], [data-testid="product-grid-title"]')
    const brand = clean(card.querySelector('[class*="Byline"]')?.textContent ?? '')
    const name = clean(
      overlay?.getAttribute('aria-label') ||
        overlay?.getAttribute('title') ||
        overlay?.textContent ||
        card.querySelector('h2, h3, [class*="Title"]')?.textContent ||
        '',
    ).replace(/^image of\s+/i, '')
    // Sponsored tiles are ads, often for unrelated items.
    if (/^sponsored\b/i.test(name) || /\bAdHolder\b/.test(card.getAttribute('class') ?? '')) continue
    const title = name && brand && !name.toLowerCase().startsWith(brand.toLowerCase()) ? `${brand} ${name}` : name
    if (title.length < 4) continue

    const buy = card.querySelector('[data-testid="grid-item-buy-price"], [class*="buyPrice"], .a-price:not([data-a-strike])')
    const buyText = buy ? textOf(buy.cloneNode(true) as Element).replace(/strike through.*$/i, '') : textOf(card)
    let price = PRICE_RE.exec(buyText)?.[0]?.replace(/\s+/g, '') ?? ''
    // Amazon audiobooks show "$0.00 ... Free with Audible trial".
    if (/^\$0(?:\.00)?$/.test(price)) price = /free with .*trial/i.test(textOf(card)) ? 'Free trial' : ''
    const ratingLabel = card.querySelector('[data-testid="grid-item-short-review"], [class*="rating"], [aria-label*="out of 5"]')
    const ratingText = clean(
      ratingLabel?.getAttribute('aria-label') || ratingLabel?.textContent || textOf(card.querySelector('[class*="rating"]') ?? card),
    )
    const stars = RATING_RE.exec(ratingText)
    const reviews = /(\d[\d,]*)\s*(?:customer reviews|ratings?|\bco\b)?/i.exec(
      clean(card.querySelector('[data-testid="grid-item-review-count"]')?.textContent ?? '') ||
        (card.querySelector('a[aria-label$=" ratings"], a[aria-label$=" rating"]')?.getAttribute('aria-label') ?? '') ||
        (stars ? ratingText.slice(stars.index + stars[0].length) : ratingText),
    )
    const rating = stars ? `${stars[1]}/5${reviews ? ` (${reviews[1]})` : ''}` : ''

    const url =
      firstAbsUrl(card, base, u => /\/(?:dp|gp\/product)\//.test(u.pathname)) ||
      firstAbsUrl(card, base, u => u.pathname.length > 3 && u.toString() !== base)
    // The same item recurs with different ref= tracking, so key on the ASIN when there is one.
    const key = card.getAttribute('data-asin') || /\/(?:dp|gp\/product)\/([A-Z0-9]{10})/.exec(url)?.[1] || url
    // Colour variants have their own ASIN and title ("... Matte Black") but share price and review count.
    const variant = stars && reviews ? `${price}|${rating}` : ''
    if (!url || seenUrl.has(key) || (variant && seenUrl.has(variant))) continue
    seenUrl.add(key)
    if (variant) seenUrl.add(variant)

    const img = Array.from(card.querySelectorAll('img')).find(im => {
      const cls = `${im.getAttribute('class') ?? ''} ${im.parentElement?.getAttribute('class') ?? ''}`
      return !HIDDEN_IMAGE.test(cls) && !JUNK_IMAGE.test(im.getAttribute('alt') ?? '')
    })
    const imageId = img
      ? addImage(images, seen, pickSrc(img), img.getAttribute('alt') || title, title, base, MAX_IMAGES + 60)
      : -1
    out.push({ title: title.slice(0, 80), byline: '', price, rating, url, imageId })
    card.remove()
  }
  uniquifyTitles(out)
  return out
}

function compactCount(raw: string): string {
  const n = Number(raw.replace(/,/g, ''))
  if (!Number.isFinite(n)) return raw
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1).replace(/\.0$/, '')}m`
  if (n >= 10_000) return `${Math.round(n / 1_000)}k`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, '')}k`
  return String(n)
}

/**
 * Book shelves and similar catalogs: a cover, a title link, an author, a rating.
 * Goodreads uses `.elementList`; schema.org Book markup shows up elsewhere.
 */
function collectCatalog(doc: Document, base: string, images: ImageCandidate[], seen: Set<string>): ProductCandidate[] {
  const cards = Array.from(doc.querySelectorAll('.elementList, [itemtype*="schema.org/Book"], .bookalike')).filter(
    (el, i, all) => !all.some((other, j) => j !== i && other.contains(el)),
  )
  const out: ProductCandidate[] = []
  const seenUrl = new Set<string>()
  for (const card of cards) {
    if (out.length >= 60) break
    const titleEl = card.querySelector('a.bookTitle, a[class*="bookTitle"]')
    const title = clean(
      titleEl?.textContent ||
        card.querySelector('a.leftAlignedImage')?.getAttribute('title') ||
        card.querySelector('img[alt]')?.getAttribute('alt') ||
        '',
    ).replace(/\s*\((?:paperback|hardcover|mass market paperback|kindle edition|ebook|audiobook)\)\s*$/i, '')
    if (title.length < 2) continue
    const url =
      firstAbsUrl(card, base, u => /\/book\/show\//.test(u.pathname)) ||
      firstAbsUrl(card, base, u => /\/(?:title|book)\//.test(u.pathname))
    if (!url || seenUrl.has(url)) continue
    seenUrl.add(url)

    const author = clean(
      card.querySelector('.authorName [itemprop="name"], a.authorName, [itemprop="author"] [itemprop="name"]')?.textContent ?? '',
    ).replace(/\s*\(goodreads author\)/i, '')
    const grey = clean(card.querySelector('.greyText.smallText, .minirating, [class*="RatingStatistics"]')?.textContent ?? textOf(card))
    const avg = /(?:avg(?:erage)?\s+rating|rating)\s*([0-5](?:\.\d+)?)/i.exec(grey)
    const ratings = /([\d,]+)\s+ratings/i.exec(grey)
    const year = /published\s+(\d{4})/i.exec(grey)?.[1] ?? ''
    const rating = avg ? `${avg[1]}/5${ratings ? ` (${compactCount(ratings[1])})` : ''}` : ''

    const img =
      card.querySelector('a.leftAlignedImage img, img.bookCover, .leftAlignedImage img') ??
      Array.from(card.querySelectorAll('img')).find(im => {
        const cls = `${im.getAttribute('class') ?? ''} ${im.parentElement?.getAttribute('class') ?? ''}`
        return !HIDDEN_IMAGE.test(cls) && !JUNK_IMAGE.test(im.getAttribute('alt') ?? '')
      })
    const imageId = img
      ? addImage(images, seen, pickSrc(img), img.getAttribute('alt') || title, title, base, MAX_IMAGES + 60)
      : -1
    out.push({ title: title.slice(0, 80), byline: author.slice(0, 40), price: year, rating, url, imageId })
    card.remove()
  }
  return out
}

/** Colorways often share a name; borrow a unique slug token (Black, Cognac) so rows differ. */
function uniquifyTitles(products: ProductCandidate[]) {
  const groups = new Map<string, ProductCandidate[]>()
  for (const p of products) {
    const k = p.title.toLowerCase()
    const g = groups.get(k) ?? []
    g.push(p)
    groups.set(k, g)
  }
  for (const group of groups.values()) {
    if (group.length < 2) continue
    for (const p of group) {
      let slug = ''
      try {
        slug = new URL(p.url).pathname.split('/').find(s => s.includes('-')) ?? ''
      } catch {
        continue
      }
      const extra = slug.split('-').find(
        w => w.length > 2 && !/^(medium|large|small|mens|womens|boots|shoes|pawz|\d+)$/i.test(w) && !p.title.toLowerCase().includes(w.toLowerCase()),
      )
      if (extra) p.title = `${p.title} · ${extra[0]!.toUpperCase()}${extra.slice(1).toLowerCase()}`
    }
  }
}

export type Extracted = CondensedPage & { images: ImageCandidate[]; allLinks: Link[]; products: ProductCandidate[] }

export function extract(html: string, finalUrl: string, contentType: string): Extracted {
  const site = new URL(finalUrl).hostname.replace(/^www\./, '')

  if (/text\/plain/i.test(contentType)) {
    const blocks = html.split(/\n{2,}/).map(p => ({ type: 'p' as const, text: clean(p) })).filter(b => b.text)
    const sections = finalize([{ heading: '', blocks: blocks.slice(0, MAX_BLOCKS) }], '')
    return { url: finalUrl, title: site, site, sections, links: [], wordCount: words(sections), images: [], allLinks: [], products: [] }
  }

  // Swallow jsdom's CSS parse noise; we never execute page scripts.
  const dom = new JSDOM(html, { url: finalUrl, virtualConsole: new VirtualConsole() })
  const doc = dom.window.document
  const links = new Map<string, Link>()

  doc.querySelectorAll(NOISE_SELECTOR).forEach(el => el.remove())
  const { images, seen } = collectImages(doc, finalUrl)
  const products = [...collectProducts(doc, finalUrl, images, seen), ...collectCatalog(doc, finalUrl, images, seen)]
  // Bake CSS/aria ratings into text before Readability drops class names.
  for (const el of Array.from(doc.querySelectorAll('[class*=star-rating], [aria-label], [title]'))) {
    const rating = ratingOf(el)
    if (rating) el.textContent = rating.trim()
  }
  const article = new Readability(doc.cloneNode(true) as Document, { charThreshold: 300 }).parse()
  const pageTitle = clean(article?.title || doc.title || site)

  let sections: Section[] = []
  if (article?.content && (article.textContent?.trim().length ?? 0) > 400) {
    const frag = JSDOM.fragment(article.content)
    const b = new Builder()
    for (const child of Array.from(frag.childNodes)) walk(child, b)
    b.flush()
    sections = finalize(b.sections, pageTitle)
    collectLinks(frag, finalUrl, links)
  }

  // Readability gives up on (or latches onto one blurb of) homepages, link
  // aggregators and app-like pages. Build a de-chromed outline of the whole
  // body as well, and keep whichever says more. Skip this when we already
  // pulled a product grid — the outline is usually footer chrome.
  if (words(sections) < 250 && products.length < 3) {
    const body = doc.body.cloneNode(true) as HTMLElement
    body.querySelectorAll(CHROME_SELECTOR).forEach(el => el.remove())
    const root = body.querySelector('main, [role=main], #main, #content') ?? body
    const b = new Builder()
    walk(root, b)
    b.flush()
    const outline = finalize(b.sections, pageTitle)
    if (words(outline) > words(sections)) sections = outline
  }
  if (!LEGAL_PAGE.test(new URL(finalUrl).pathname)) sections = dropFinePrint(sections)
  if (links.size < 10) collectLinks(doc.body, finalUrl, links)
  const allLinks = new Map<string, Link>()
  collectLinks(doc.body, finalUrl, allLinks, MAX_ALL_LINKS)
  for (const p of products) {
    if (!allLinks.has(p.url)) allLinks.set(p.url, { text: p.title, url: p.url })
    if (!links.has(p.url)) links.set(p.url, { text: p.title, url: p.url })
  }
  if (products.length) {
    const shoppy = products.filter(p => /^[$€£]/.test(p.price)).length >= products.length / 2
    const heading = shoppy ? 'Products' : 'Books'
    const listing = {
      heading,
      blocks: products.map(p => ({
        type: 'li' as const,
        text: [p.title, p.byline, p.price, p.rating].filter(Boolean).join(' | '),
      })),
    }
    // Prefer the structured listing over whatever Readability flattened from the grid.
    const rest = sections.filter(s => !/^(products|books)$/i.test(s.heading))
    sections = rest.length && rest[0].heading === 'Start' ? [rest[0], listing, ...rest.slice(1)] : [listing, ...rest]
  }

  return {
    url: finalUrl,
    title: pageTitle,
    site,
    byline: article?.byline ? clean(article.byline) : undefined,
    sections,
    links: [...links.values()],
    wordCount: words(sections),
    images,
    allLinks: [...allLinks.values()],
    products,
  }
}

/** Drops legal/footer sections and short legal lines; keeps the page if that would empty it. */
function dropFinePrint(sections: Section[]): Section[] {
  const kept = sections
    .filter(s => !FINE_PRINT_HEADING.test(s.heading))
    .map(s => ({ ...s, blocks: s.blocks.filter(b => b.type === 'code' || b.text.length > 240 || !LEGAL_LINE.test(b.text)) }))
    .filter(s => s.blocks.length)
  return words(kept) >= 50 ? kept : sections
}

/** Plain-text rendering used as summarizer input. */
export function toPlainText(page: CondensedPage): string {
  return page.sections
    .map(s => [`## ${s.heading}`, ...s.blocks.map(b => (b.type === 'li' ? `- ${b.text}` : b.text))].join('\n\n'))
    .join('\n\n')
}
