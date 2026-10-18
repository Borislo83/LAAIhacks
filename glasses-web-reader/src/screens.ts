import { measureTextWrap, pxTruncate } from '@evenrealities/pretext'
import type { GlassImage, Screen } from '../shared/types'

// Turns one smart-mode screen into absolutely positioned G2 containers.
// Limits per page: 8 text/list containers (one is the invisible event
// layer), 4 images, 12 total. Text has one font, one size, one brightness
// and is always left-aligned, so hierarchy comes from borders: a bright
// thick border is "selected/primary", a dim thin one is "secondary".

export interface TextBox {
  x: number
  y: number
  w: number
  h: number
  text: string
  pad?: number
  border?: number
  /** 0-15 greyscale; 13 bright, 5 dim. */
  color?: number
  radius?: number
}

export interface ImageBox {
  x: number
  y: number
  image: GlassImage
}

export interface PageSpec {
  texts: TextBox[]
  images: ImageBox[]
}

export interface ScreenContext {
  /** "2 / 5" */
  position: string
  /** Selected item on list screens. */
  selected: number
  /** Headings of the screens after this one, for "▼ next" hints. */
  upcoming: string[]
}

const LINE = 27
const MAX_TEXTS = 7
const MAX_IMAGES = 4
const BRIGHT = 13
const DIM = 5
const LIST_WITH_THUMBS = 3
const LIST_TEXT_ROWS = 5

export const listVisible = (s: Extract<Screen, { layout: 'list' }>) =>
  s.items.some(it => it.image) ? LIST_WITH_THUMBS : LIST_TEXT_ROWS

/** First item of the 3-or-5 row window that keeps `sel` on screen. */
export function listWindowStart(s: Extract<Screen, { layout: 'list' }>, sel: number) {
  const vis = listVisible(s)
  const n = s.items.length
  return Math.max(0, Math.min(sel - (vis > 2 ? 1 : 0), Math.max(0, n - vis)))
}

const inner = (b: { w: number; pad?: number; border?: number }) => b.w - 2 * ((b.pad ?? 4) + (b.border ?? 0))

/** One line, cut with "..." at the glasses' real glyph widths. */
export const line = (text: string, width: number) => pxTruncate(text.replace(/\s+/g, ' ').trim(), width)

/** At most `lines` wrapped lines, cut at a word boundary. */
export function fit(text: string, width: number, lines: number): string {
  const t = text.replace(/\s+/g, ' ').trim()
  if (measureTextWrap(t, width).lineCount <= lines) return t
  const words = t.split(' ')
  let lo = 0
  let hi = words.length
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2)
    if (measureTextWrap(words.slice(0, mid).join(' ') + '...', width).lineCount <= lines) lo = mid
    else hi = mid - 1
  }
  return lo ? words.slice(0, lo).join(' ') + '...' : line(t, width)
}

function box(b: TextBox, build: (w: number) => string): TextBox {
  return { ...b, text: build(inner(b)) || ' ' }
}

/** Title and position in one box, for layouts that need every container. */
const compactHeader = (title: string, position: string): TextBox =>
  box({ x: 0, y: 0, w: 576, h: 34, text: '', pad: 3 }, w => `${line(title, w - 90)}   ${position}`)

const header = (title: string, position?: string): TextBox[] => {
  const head = box({ x: 0, y: 0, w: position ? 480 : 576, h: 34, text: '', pad: 3 }, w => line(title, w))
  return position ? [head, { x: 488, y: 0, w: 88, h: 34, text: position, pad: 3 }] : [head]
}

const footer = (text: string, y = 256, position?: string): TextBox =>
  box({ x: 0, y, w: 576, h: 288 - y, text: '', pad: 2 }, w =>
    position ? `${line(text, w - 90)}   ${position}` : line(text, w),
  )

const nextHint = (ctx: ScreenContext) =>
  ctx.upcoming.length ? `▼ ${ctx.upcoming.slice(0, 3).join(' · ')}` : ''

export function screenTitle(s: Screen): string {
  switch (s.layout) {
    case 'headline':
      return s.headline
    case 'product':
      return s.title
    case 'stat':
      return s.value
    case 'step':
      return `${s.index}. ${s.title}`
    default:
      return s.heading
  }
}

export function renderScreen(s: Screen, ctx: ScreenContext): PageSpec {
  const texts: TextBox[] = []
  const images: ImageBox[] = []

  switch (s.layout) {
    case 'headline': {
      if (s.image) {
        images.push({ x: 0, y: 0, image: s.image })
        texts.push(box({ x: 292, y: 0, w: 284, h: 144, text: '' }, w => `${fit(s.headline, w, 4)}\n${line(s.source, w)}`))
      } else {
        texts.push(box({ x: 0, y: 0, w: 576, h: 144, text: '', pad: 6 }, w => `${fit(s.headline, w, 3)}\n${line(s.source, w)}`))
      }
      texts.push(box({ x: 0, y: 150, w: 576, h: 102, text: '', pad: 8, border: 2, color: BRIGHT, radius: 10 }, w => fit(s.summary, w, 3)))
      texts.push(footer(nextHint(ctx), 256, ctx.position))
      break
    }

    case 'points': {
      texts.push(...header(`■ ${s.heading}`, ctx.position))
      const bodyH = s.quote ? 148 : 218
      const maxPoints = Math.floor((bodyH - 8) / LINE)
      texts.push(
        box({ x: 0, y: 34, w: 576, h: bodyH, text: '' }, w =>
          s.points.slice(0, maxPoints).map(p => line(`▶ ${p}`, w)).join('\n'),
        ),
      )
      if (s.quote) {
        texts.push(
          box({ x: 0, y: 186, w: 576, h: 102, text: '', pad: 6, border: 1, color: DIM, radius: 10 }, w =>
            `${fit(`"${s.quote!.text}"`, w, 2)}\n${line(`- ${s.quote!.by}`, w)}`,
          ),
        )
      } else {
        texts.push(footer(nextHint(ctx)))
      }
      break
    }

    case 'list': {
      const n = s.items.length
      const sel = n ? Math.min(Math.max(0, ctx.selected), n - 1) : 0
      const withThumbs = s.items.some(it => it.image)
      const vis = listVisible(s)
      const start = listWindowStart(s, sel)
      texts.push(compactHeader(s.heading, n ? `${sel + 1}/${n}` : ''))
      const rowH = withThumbs ? 82 : 48
      s.items.slice(start, start + vis).forEach((it, i) => {
        const y = 34 + i * (rowH + 3)
        const selected = start + i === sel
        const frame: TextBox = {
          x: 0, y, w: 576, h: rowH, text: '', pad: 6,
          border: selected ? 3 : 1, color: selected ? BRIGHT : DIM, radius: 10,
        }
        const body = (w: number) => `${line(`${selected ? '▶ ' : ''}${it.title}`, w)}\n${line(it.meta, w)}`
        if (withThumbs) {
          // Uniform padding only: the frame is its own box, text sits beside the cover.
          const tw = it.image?.w ?? 48
          texts.push({ ...frame, text: ' ' })
          if (it.image) images.push({ x: 12, y: y + Math.max(5, Math.floor((rowH - it.image.h) / 2)), image: it.image })
          texts.push(box({ x: 12 + tw + 8, y: y + 6, w: 576 - 20 - tw, h: rowH - 12, text: '', pad: 2 }, body))
        } else {
          texts.push(box(frame, body))
        }
      })
      break
    }

    case 'product': {
      const x = s.image ? 128 : 0
      if (s.image) images.push({ x: 0, y: 0, image: s.image })
      texts.push(box({ x, y: 0, w: 576 - x, h: 62, text: '', pad: 2 }, w => `${line(s.title, w)}\n${line(s.subtitle, w)}`))
      const half = Math.floor((576 - x - 8) / 2)
      texts.push(box({ x, y: 64, w: half, h: 80, text: '', pad: 6, border: 2, color: BRIGHT, radius: 10 }, w => `PRICE\n${line(s.price, w)}`))
      texts.push(
        box({ x: x + half + 8, y: 64, w: 576 - x - half - 8, h: 80, text: '', pad: 6, border: 1, color: DIM, radius: 10 }, w =>
          `${line(s.rating || 'No rating', w)}\n${line(s.stock || ' ', w)}`,
        ),
      )
      texts.push(box({ x: 0, y: 150, w: 576, h: 104, text: '', pad: 4 }, w => fit(s.description, w, 3)))
      texts.push(footer(nextHint(ctx), 256, ctx.position))
      break
    }

    case 'tiles': {
      let tileY: number
      let tileH: number
      if (s.image) {
        images.push({ x: 0, y: 0, image: s.image })
        texts.push(box({ x: 292, y: 0, w: 284, h: 144, text: '', pad: 2 }, w => `${line(s.heading, w)}\n${fit(s.intro, w, 4)}`))
        tileY = 150
        tileH = 138
      } else {
        texts.push(...header(`■ ${s.heading}`, ctx.position))
        texts.push(box({ x: 0, y: 34, w: 576, h: 60, text: '' }, w => fit(s.intro, w, 2)))
        tileY = 96
        tileH = 94
      }
      const tiles = s.tiles.slice(0, 4)
      tiles.forEach((t, i) => {
        const tile: TextBox = s.image
          ? { x: i * 145, y: tileY, w: i === 3 ? 141 : 140, h: tileH, text: '', pad: 6, border: 1, color: BRIGHT, radius: 10 }
          : { x: (i % 2) * 292, y: tileY + Math.floor(i / 2) * (tileH + 4), w: 284, h: tileH, text: '', pad: 4, border: 1, color: BRIGHT, radius: 10 }
        const valueLines = Math.floor((tile.h - 2 * (tile.pad! + 1)) / LINE) - 1
        texts.push(box(tile, w => `${line(t.label.toUpperCase(), w)}\n${fit(t.value, w, Math.max(1, valueLines))}`))
      })
      break
    }

    case 'keys': {
      texts.push(compactHeader(`■ ${s.heading}`, ctx.position))
      s.keys.slice(0, 6).forEach((k, i) => {
        texts.push(
          box({ x: (i % 2) * 292, y: 38 + Math.floor(i / 2) * 85, w: 284, h: 80, text: '', pad: 6, border: 2, color: BRIGHT, radius: 10 }, w =>
            `${line(k.key, w)}\n${line(k.action, w)}`,
          ),
        )
      })
      break
    }

    case 'split': {
      texts.push(...header(s.heading, ctx.position))
      texts.push(box({ x: 0, y: 34, w: 576, h: 74, text: '', pad: 4, border: 2, color: BRIGHT, radius: 10 }, w => fit(s.verdict, w, 2)))
      for (const [i, side] of [s.left, s.right].entries()) {
        texts.push(
          box({ x: i * 292, y: 114, w: 284, h: 140, text: '', pad: 4, border: 1, color: DIM, radius: 10 }, w =>
            `${line(side.label.toUpperCase(), w)}\n${fit(side.text, w, 3)}`,
          ),
        )
      }
      texts.push(footer(`${nextHint(ctx)}`, 258))
      break
    }

    case 'stat': {
      if (s.valueImage) images.push({ x: 0, y: 0, image: s.valueImage })
      else texts.push({ x: 0, y: 0, w: 200, h: 100, text: s.value, pad: 8 })
      texts.push(box({ x: 212, y: 4, w: 364, h: 100, text: '' }, w => s.lines.slice(0, 3).map(l => line(l, w)).join('\n')))
      let tilesY = 108
      if (s.alert) {
        texts.push(
          box({ x: 0, y: 106, w: 576, h: 66, text: '', pad: 2, border: 3, color: BRIGHT, radius: 10 }, w =>
            `${line(`▲ ${s.alert!.title.toUpperCase()}`, w)}\n${line(s.alert!.detail, w)}`,
          ),
        )
        tilesY = 178
      }
      const tiles = s.tiles.slice(0, 4)
      const tw = tiles.length ? Math.floor((576 - 5 * (tiles.length - 1)) / tiles.length) : 0
      tiles.forEach((t, i) => {
        const tile: TextBox = { x: i * (tw + 5), y: tilesY, w: tw, h: 288 - tilesY, text: '', pad: 6, border: 1, color: DIM, radius: 10 }
        const valueLines = Math.floor((tile.h - 14) / LINE) - 1
        texts.push(box(tile, w => `${line(t.label, w)}\n${fit(t.value, w, Math.max(1, valueLines))}`))
      })
      break
    }

    case 'timeline': {
      texts.push(...header(`■ ${s.heading}`, ctx.position))
      const events = s.events.slice(0, 7)
      texts.push({ x: 0, y: 40, w: 92, h: 214, text: events.map(e => line(e.date, 76)).join('\n') || ' ', pad: 4 })
      texts.push({ x: 96, y: 46, w: 3, h: Math.max(30, events.length * LINE - 6), text: ' ', pad: 0, border: 1, color: BRIGHT, radius: 1 })
      texts.push(box({ x: 108, y: 40, w: 468, h: 214, text: '' }, w => events.map(e => line(e.text, w)).join('\n')))
      texts.push(footer(nextHint(ctx)))
      break
    }

    case 'compare': {
      texts.push(...header(`■ ${s.heading}`, ctx.position))
      const cols = s.columns.slice(0, 3)
      const cw = Math.floor((576 - 12 * (cols.length - 1)) / Math.max(1, cols.length))
      cols.forEach((c, i) => {
        texts.push(
          box({ x: i * (cw + 12), y: 40, w: cw, h: 212, text: '', pad: 6, border: 1, color: BRIGHT, radius: 10 }, w => {
            // Name + rule take 2 lines; entries wrap only while the column has room.
            let budget = Math.floor((212 - 14) / LINE) - 2
            const rows: string[] = []
            c.lines.forEach((l, j) => {
              const left = c.lines.length - j - 1
              if (budget <= 0) return
              const t = fit(l, w, Math.max(1, Math.min(2, budget - left)))
              budget -= measureTextWrap(t, w).lineCount
              rows.push(t)
            })
            return [line(c.name.toUpperCase(), w), line('━━━━━━━━━━━━━━━━━━━━', w).replace(/\.\.\.$/, ''), ...rows].join('\n')
          }),
        )
      })
      texts.push(footer(nextHint(ctx)))
      break
    }

    case 'step': {
      const dots = s.total <= 8 ? Array.from({ length: s.total }, (_, i) => (i < s.index ? '●' : '○')).join(' ') : `${s.index} / ${s.total}`
      const dotsW = s.total <= 8 ? Math.min(250, s.total * 22 + 16) : 100
      texts.push(box({ x: 0, y: 0, w: 576 - dotsW, h: 34, text: '', pad: 3 }, w => line(`■ ${s.heading}`, w)))
      texts.push({ x: 576 - dotsW, y: 0, w: dotsW, h: 34, text: dots, pad: 3 })
      texts.push(
        box({ x: 0, y: 40, w: 576, h: 150, text: '', pad: 8, border: 3, color: BRIGHT, radius: 10 }, w =>
          `STEP ${s.index} OF ${s.total}\n${line(s.title, w)}\n${fit(s.detail, w, 2)}`,
        ),
      )
      texts.push(
        box({ x: 0, y: 198, w: 576, h: 60, text: '', pad: 2, border: 1, color: DIM, radius: 10 }, w =>
          s.next ? `NEXT\n${line(s.next, w)}` : `LAST STEP\n${line(ctx.upcoming[0] ? `▼ ${ctx.upcoming[0]}` : 'Done', w)}`,
        ),
      )
      texts.push(footer('▼ next step   ▲ back', 260))
      break
    }

    case 'tagged': {
      texts.push(...header(`■ ${s.heading}`, ctx.position))
      const rows = s.rows.slice(0, 4)
      rows.forEach((r, i) => {
        texts.push(box({ x: 0, y: 40 + i * 54, w: 112, h: 48, text: '', pad: 6, border: 1, color: BRIGHT, radius: 10 }, w => line(r.tag.toUpperCase(), w)))
      })
      // One box for all row texts: blank lines keep each on its tag's 54px pitch.
      texts.push(box({ x: 120, y: 44, w: 456, h: 216, text: '', pad: 6 }, w => rows.map(r => line(r.text, w)).join('\n\n')))
      break
    }
  }

  // Hard device limits: 7 visible text boxes (+1 event layer), 4 images.
  return { texts: texts.slice(0, MAX_TEXTS), images: images.slice(0, MAX_IMAGES) }
}

/** Plain-text version for the phone mirror's accessibility label. */
export function specText(spec: PageSpec): string {
  return spec.texts.map(t => t.text.trim()).filter(Boolean).join('\n')
}
