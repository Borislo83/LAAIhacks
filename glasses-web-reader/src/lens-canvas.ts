import { getTextWidth } from '@evenrealities/pretext'
import { lensText } from './lensos/glyphs'
import { CORNER_ATLAS, CORNER_CELLS, GLYPH_ATLAS, GLYPH_CELLS, GLYPH_LINE_H, GLYPH_MARGIN } from './glyph-atlas'
import type { PageSpec, TextBox } from './screens'

// Draws a PageSpec the way the G2 does: green on black, the firmware's own glyphs and
// rounded corners (src/glyph-atlas.ts), its line breaks and pen positions (pretext), and
// the lens's brightness steps and scrollbar, measured off the simulator.

/** Coverage (0-255) of a border at grey level 0-15. */
const BORDER = [0, 96, 131, 157, 179, 197, 214, 230, 244, 250, 255, 255, 255, 255, 255, 255]
/** Coverage of text at brightness 0-4. */
const TEXT = [131, 179, 197, 230, 255]
const GREEN = '#00ff00'
/** The firmware's scrollbar: 4px wide, 2px in from the padding, grey 3 with dimmer corners. */
const SCROLLBAR_W = 4
const SCROLLBAR_INSET = 2
const SCROLLBAR = 157
const SCROLLBAR_CORNER = 96

/** An atlas of white coverage, tinted green once. */
function tinted(src: string) {
  return new Promise<HTMLCanvasElement>(resolve => {
    const img = new Image()
    img.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = img.width
      canvas.height = img.height
      const g = canvas.getContext('2d')!
      g.drawImage(img, 0, 0)
      g.globalCompositeOperation = 'source-in'
      g.fillStyle = GREEN
      g.fillRect(0, 0, img.width, img.height)
      resolve(canvas)
    }
    img.src = src
  })
}

let atlas: HTMLCanvasElement | null = null
let corners: HTMLCanvasElement | null = null
const ready = Promise.all([tinted(GLYPH_ATLAS), tinted(CORNER_ATLAS)]).then(([a, c]) => { atlas = a; corners = c })

/** Greedy wrap at spaces, breaking inside a word only when it can't fit a line alone. */
export function wrap(text: string, width: number): string[] {
  const out: string[] = []
  for (const para of text.split('\n')) {
    let line = ''
    for (const word of para.split(/(?<= )/)) {
      if (getTextWidth(line + word.trimEnd()) <= width) { line += word; continue }
      if (line) out.push(line.trimEnd())
      line = ''
      for (const ch of word) {
        if (line && getTextWidth(line + ch) > width) { out.push(line); line = '' }
        line += ch
      }
    }
    out.push(line.trimEnd())
  }
  return out
}

function drawLine(g: CanvasRenderingContext2D, line: string, x: number, y: number) {
  let pen = 0
  let last = ''
  for (const ch of line) {
    // The previous glyph's advance plus the kerning between the two, rounded as LVGL does.
    if (last) pen += getTextWidth(last + ch) - getTextWidth(ch)
    const cell = GLYPH_CELLS[ch]
    if (cell && atlas) {
      g.drawImage(atlas, cell[0], 0, cell[1], GLYPH_LINE_H, x + pen - GLYPH_MARGIN, y, cell[1], GLYPH_LINE_H)
    } else if (ch.trim()) {
      g.fillStyle = GREEN
      g.font = `${GLYPH_LINE_H - 7}px system-ui, sans-serif`
      g.textBaseline = 'top'
      g.fillText(ch, x + pen, y + 3)
    }
    last = ch
  }
}

function roundRect(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  g.beginPath()
  g.roundRect(x, y, w, h, Math.max(0, Math.min(r, w / 2, h / 2)))
}

/** Straight edges as runs, corners stamped from the firmware's own (mirrored for the other three). */
function drawBorder(g: CanvasRenderingContext2D, t: TextBox, border: number) {
  const r = Math.min(t.radius ?? 0, Math.floor(Math.min(t.w, t.h) / 2))
  const cell = CORNER_CELLS[`${r},${border}`]
  g.globalAlpha = BORDER[t.color ?? 13] / 255
  g.fillStyle = GREEN
  if (!cell || !corners || 2 * cell[1] > Math.min(t.w, t.h)) {
    // A radius or width the atlas lacks: the ring between the outer and inner rounded rects.
    roundRect(g, t.x, t.y, t.w, t.h, r)
    g.roundRect(t.x + border, t.y + border, t.w - 2 * border, t.h - 2 * border, Math.max(0, r - border))
    g.fill('evenodd')
    return
  }
  const [cx, k] = cell
  g.fillRect(t.x + k, t.y, t.w - 2 * k, border)
  g.fillRect(t.x + k, t.y + t.h - border, t.w - 2 * k, border)
  g.fillRect(t.x, t.y + k, border, t.h - 2 * k)
  g.fillRect(t.x + t.w - border, t.y + k, border, t.h - 2 * k)
  for (const [sx, sy] of [[1, 1], [-1, 1], [1, -1], [-1, -1]]) {
    g.save()
    g.translate(sx > 0 ? t.x : t.x + t.w, sy > 0 ? t.y : t.y + t.h)
    g.scale(sx, sy)
    g.drawImage(corners, cx, 0, k, k, 0, 0, k, k)
    g.restore()
  }
}

function drawText(g: CanvasRenderingContext2D, t: TextBox) {
  const border = t.border ?? 0
  if (border) drawBorder(g, t, border)
  const inset = border + (t.pad ?? 4)
  const innerW = t.w - 2 * inset
  const innerH = t.h - 2 * inset
  const lines = wrap(lensText(t.text), innerW)
  g.save()
  g.beginPath()
  g.rect(t.x + inset, t.y + inset, innerW, innerH)
  g.clip()
  g.globalAlpha = TEXT[t.textColor ?? 4] / 255
  lines.forEach((line, i) => {
    const y = t.y + inset + i * GLYPH_LINE_H
    if (y < t.y + t.h) drawLine(g, line, t.x + inset, y)
  })
  g.restore()
  // A scrolling box shows the firmware's scrollbar once its text overflows (scrolled
  // to the top: the firmware doesn't report how far the wearer has scrolled).
  const total = lines.length * GLYPH_LINE_H
  if (t.scroll && total > innerH) {
    const track = innerH - 2 * SCROLLBAR_INSET
    const x = t.x + t.w - inset - SCROLLBAR_INSET - SCROLLBAR_W
    const y = t.y + inset + SCROLLBAR_INSET
    const h = Math.round(track * innerH / total)
    g.fillStyle = GREEN
    g.globalAlpha = SCROLLBAR / 255
    g.fillRect(x + 1, y, SCROLLBAR_W - 2, h)
    g.fillRect(x, y + 1, 1, h - 2)
    g.fillRect(x + SCROLLBAR_W - 1, y + 1, 1, h - 2)
    g.globalAlpha = SCROLLBAR_CORNER / 255
    for (const [cx, cy] of [[x, y], [x + SCROLLBAR_W - 1, y], [x, y + h - 1], [x + SCROLLBAR_W - 1, y + h - 1]]) g.fillRect(cx, cy, 1, 1)
  }
}

const images = new Map<string, HTMLImageElement>()
function image(png: string, onLoad: () => void) {
  let img = images.get(png)
  if (!img) {
    if (images.size > 64) images.clear()
    img = new Image()
    img.onload = onLoad
    img.src = `data:image/png;base64,${png}`
    images.set(png, img)
  }
  return img.complete ? img : null
}

/** Paints `spec` onto a 576x288 canvas. */
export function paintLens(canvas: HTMLCanvasElement, spec: PageSpec) {
  const g = canvas.getContext('2d')!
  const repaint = () => paintLens(canvas, spec)
  if (!atlas) void ready.then(repaint)
  g.globalAlpha = 1
  g.globalCompositeOperation = 'source-over'
  g.fillStyle = '#000'
  g.fillRect(0, 0, 576, 288)
  for (const t of spec.texts) drawText(g, t)
  g.globalAlpha = 1
  for (const im of spec.images) {
    const img = image(im.image.png, repaint)
    if (!img) continue
    // Greyscale on the lens is green brightness: draw the grey, keep only its green.
    g.save()
    g.beginPath()
    g.rect(im.x, im.y, im.image.w, im.image.h)
    g.clip()
    g.drawImage(img, im.x, im.y, im.image.w, im.image.h)
    g.globalCompositeOperation = 'multiply'
    g.fillStyle = GREEN
    g.fillRect(im.x, im.y, im.image.w, im.image.h)
    g.restore()
  }
}
