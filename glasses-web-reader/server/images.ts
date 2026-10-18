import sharp from 'sharp'
import type { GlassImage } from '../shared/types'
import { safeFetchImage } from './safe-fetch'

// Prepares images for G2 image containers: 4-bit greyscale at the exact box
// size. On the glasses lit pixels glow green and black is see-through, so a
// bright photo becomes a glowing slab across the wearer's view. Bright
// images are therefore turned into line art (edges only); dark ones keep
// their tones as a dithered photo.

export type Treatment = 'auto' | 'photo' | 'lines'

const LEVELS = 16
const STEP = 255 / (LEVELS - 1)
// Mean brightness (0-255) above which a large image reads as a glowing slab.
const BRIGHT_MEAN = 105
// Line art needs room to read; small thumbnails always stay photos.
const MIN_LINE_ART_W = 160

export function stretch(px: Uint8Array) {
  const hist = new Array(256).fill(0)
  for (const v of px) hist[v]++
  const cut = px.length * 0.01
  let lo = 0
  let hi = 255
  for (let acc = 0; lo < 255 && acc + hist[lo] <= cut; lo++) acc += hist[lo]
  for (let acc = 0; hi > 0 && acc + hist[hi] <= cut; hi--) acc += hist[hi]
  const range = Math.max(1, hi - lo)
  for (let i = 0; i < px.length; i++) px[i] = Math.max(0, Math.min(255, ((px[i] - lo) * 255) / range))
}

/** Floyd-Steinberg down to the panel's 16 levels. */
function dither(px: Uint8Array, w: number, h: number): Uint8Array {
  const buf = Float32Array.from(px)
  const out = new Uint8Array(w * h)
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x
      const level = Math.max(0, Math.min(LEVELS - 1, Math.round(buf[i] / STEP)))
      const err = buf[i] - level * STEP
      out[i] = Math.round(level * STEP)
      if (x + 1 < w) buf[i + 1] += (err * 7) / 16
      if (y + 1 < h) {
        if (x > 0) buf[i + w - 1] += (err * 3) / 16
        buf[i + w] += (err * 5) / 16
        if (x + 1 < w) buf[i + w + 1] += err / 16
      }
    }
  }
  return out
}

/** Sobel edges on a 2x supersampled image, then quantized; the border is cleared. */
function lineArt(px2: Uint8Array, w: number, h: number): Uint8Array {
  const W = w * 2
  const H = h * 2
  const mag = new Float32Array(W * H)
  let max = 1
  for (let y = 1; y < H - 1; y++) {
    for (let x = 1; x < W - 1; x++) {
      const p = (dx: number, dy: number) => px2[(y + dy) * W + x + dx]
      const gx = p(1, -1) + 2 * p(1, 0) + p(1, 1) - p(-1, -1) - 2 * p(-1, 0) - p(-1, 1)
      const gy = p(-1, 1) + 2 * p(0, 1) + p(1, 1) - p(-1, -1) - 2 * p(0, -1) - p(1, -1)
      const m = Math.hypot(gx, gy)
      mag[y * W + x] = m
      if (m > max) max = m
    }
  }
  const out = new Uint8Array(w * h)
  for (let y = 2; y < h - 2; y++) {
    for (let x = 2; x < w - 2; x++) {
      const i = 2 * y * W + 2 * x
      const m = Math.max(mag[i], mag[i + 1], mag[i + W], mag[i + W + 1]) / max
      const v = Math.min(1, m * 2.2)
      out[y * w + x] = v < 0.18 ? 0 : Math.round(Math.min(LEVELS - 1, Math.round(v * (LEVELS - 1)) + 3) * STEP)
    }
  }
  return out
}

async function toPng(px: Uint8Array, w: number, h: number): Promise<GlassImage> {
  const png = await sharp(Buffer.from(px), { raw: { width: w, height: h, channels: 1 } })
    .toColourspace('b-w')
    .png({ compressionLevel: 9 })
    .toBuffer()
  return { png: png.toString('base64'), w, h }
}

export async function glassImage(bytes: Buffer, w: number, h: number, treatment: Treatment = 'auto'): Promise<GlassImage> {
  const grey = (scale: number) =>
    sharp(bytes)
      .rotate()
      .flatten({ background: '#000000' })
      .resize(w * scale, h * scale, { fit: 'cover', position: 'attention' })
      .toColourspace('b-w')
      .raw()
      .toBuffer()
      .then(b => new Uint8Array(b))

  const px = await grey(1)
  const mean = px.reduce((a, v) => a + v, 0) / px.length
  const useLines = treatment === 'lines' || (treatment === 'auto' && w >= MIN_LINE_ART_W && mean > BRIGHT_MEAN)

  if (useLines) {
    const px2 = await sharp(Buffer.from(await grey(2)), { raw: { width: w * 2, height: h * 2, channels: 1 } })
      .blur(1)
      .toColourspace('b-w')
      .raw()
      .toBuffer()
    return toPng(lineArt(new Uint8Array(px2), w, h), w, h)
  }
  // Small bright images are usually product shots on white: knock the
  // background out to black so only the product glows.
  if (treatment !== 'photo' && mean > BRIGHT_MEAN) {
    // The background is the brightest broad region; its exact shade varies
    // (white, off-white, cream), so key the cut-off to the 90th percentile.
    const p90 = Array.from(px).sort((a, b) => a - b)[Math.floor(px.length * 0.9)]
    const cut = Math.max(170, p90 - 18)
    for (let i = 0; i < px.length; i++) if (px[i] > cut) px[i] = 0
  }
  stretch(px)
  return toPng(dither(px, w, h), w, h)
}

export async function fetchGlassImage(url: string, w: number, h: number, treatment: Treatment = 'auto') {
  try {
    return await glassImage(await safeFetchImage(url), w, h, treatment)
  } catch (err) {
    console.warn(`[image] ${url}: ${err instanceof Error ? err.message : err}`)
    return undefined
  }
}

const escapeXml = (s: string) => s.replace(/[<>&"']/g, c => `&#${c.charCodeAt(0)};`)

/** G2 text has a single size, so big readouts (84°F, $129) ship as images. */
export async function bigText(text: string, w: number, h: number): Promise<GlassImage> {
  const size = Math.floor(Math.min(h * 0.92, (w - 8) / Math.max(1, text.length * 0.6)))
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">` +
    `<rect width="100%" height="100%" fill="#000"/>` +
    `<text x="4" y="${Math.round(h / 2 + size * 0.36)}" font-family="Helvetica, Arial, sans-serif" ` +
    `font-size="${size}" fill="#fff">${escapeXml(text)}</text></svg>`
  const px = await sharp(Buffer.from(svg)).flatten({ background: '#000000' }).toColourspace('b-w').raw().toBuffer()
  return toPng(new Uint8Array(px), w, h)
}
