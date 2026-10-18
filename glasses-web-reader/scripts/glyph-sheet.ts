import { getTextWidth, measureTextWrap } from '@evenrealities/pretext'
import { drawable } from '../src/lensos/glyphs'

// Sheets of glyphs for scripts/gen-glyphs.mts to photograph off the simulator. Every
// glyph starts its own line in a column container, so its pen position is exactly the
// column's left edge (mid-line positions from pretext can be a pixel off).

/** Printable ASCII and Latin-1, plus the symbols the app puts on the lens. */
const EXTRA = '–—―‘’‚“”„•…′″€→−≡⏸─━■▲▶▷▼◀●○★☆✕⤢'
export const GLYPHS = [...new Set([
  ...Array.from({ length: 94 }, (_, i) => String.fromCharCode(33 + i)),
  ...Array.from({ length: 95 }, (_, i) => String.fromCharCode(161 + i)),
  ...EXTRA,
])].filter(ch => ch !== '­' && drawable(ch))

export const LINE_H = measureTextWrap('Ag', 576).height
/** Blank pixels kept either side of each glyph's advance (inks overhang a little). */
export const MARGIN = 4
/** Column containers per sheet (the firmware allows 8 text containers on a page). */
const COLUMNS = 8
const PITCH = 576 / COLUMNS
export const COLUMN_WIDTH = PITCH - MARGIN
const ROWS = Math.floor(288 / LINE_H)

export interface Cell { ch: string; x: number; y: number; adv: number }
export interface Column { x: number; text: string }
export interface Sheet { columns: Column[]; cells: Cell[] }

/** Where `ch` starts after `prefix` on a line: each glyph advances by its own width plus
 * its kerning with the next, rounded (pretext's pair widths). */
export function penAfter(prefix: string, ch: string) {
  const chars = [...prefix, ch]
  let pen = 0
  for (let i = 1; i < chars.length; i++) pen += getTextWidth(chars[i - 1] + chars[i]) - getTextWidth(chars[i])
  return pen
}

/** Each glyph on its own line, after `prefix` (none: at the column's left edge). */
export function sheets(prefix = ''): Sheet[] {
  const out: Sheet[] = []
  const perSheet = COLUMNS * ROWS
  for (let i = 0; i < GLYPHS.length; i += perSheet) {
    const glyphs = GLYPHS.slice(i, i + perSheet)
    const columns: Column[] = []
    const cells: Cell[] = []
    for (let c = 0; c * ROWS < glyphs.length; c++) {
      const x = c * PITCH + MARGIN
      const col = glyphs.slice(c * ROWS, (c + 1) * ROWS)
      columns.push({ x, text: col.map(ch => prefix + ch).join('\n') })
      col.forEach((ch, row) => cells.push({ ch, x: x + penAfter(prefix, ch), y: row * LINE_H, adv: getTextWidth(ch) }))
    }
    out.push({ columns, cells })
  }
  return out
}

/** Rounded-border corners to photograph: every radius and width the app draws. */
export const CORNER_RADII = Array.from({ length: 17 }, (_, r) => r)
export const CORNER_BORDERS = [1, 2, 3]
/** The square photographed at each corner: wide enough to hold all of its curve. */
export const cornerSize = (r: number, b: number) => Math.max(r, b) + 2
const BOX = 56

export interface CornerBox { r: number; b: number; x: number; y: number; w: number; h: number }

export function cornerSheets(): CornerBox[][] {
  const all = CORNER_BORDERS.flatMap(b => CORNER_RADII.map(r => ({ r, b })))
  const out: CornerBox[][] = []
  for (let i = 0; i < all.length; i += COLUMNS) {
    out.push(all.slice(i, i + COLUMNS).map(({ r, b }, n) => ({
      r, b, x: (n % 4) * 144 + 8, y: Math.floor(n / 4) * 144 + 8, w: BOX, h: BOX,
    })))
  }
  return out
}
