import sharp from 'sharp'
import type { GlassImage } from '../shared/types'
import { safeFetchImage } from './safe-fetch'

// Prepares images for G2 image containers: 4-bit greyscale at the exact box
// size. On the glasses lit pixels glow green and black is see-through, so a
// bright photo becomes a glowing slab across the wearer's view. Images keep
// their tones, but a curve pulls the average down to TONE_MEAN: highlights
// (stars, faces, lettering) stay bright while broad midtones sink, so the
// picture reads without flooding the view. Mostly-white images (prints,
// paper, diagrams) are inverted first so the paper goes dark.

export type Treatment = 'auto' | 'photo'

// Mean brightness (0-255) of a small image that is probably a product on white.
const BRIGHT_MEAN = 105
// Below this width a bright image is treated as a product shot on white.
const MIN_TONE_MAP_W = 220
// Average on-glasses brightness the tone curve aims for; a third keeps it see-through.
const TONE_MEAN = 85
// Average (after stretching) above which an image is mostly paper and is inverted.
const PAPER_MEAN = 150
const MAX_GAMMA = 3

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

// The glasses read an image's grey value g as level g >> 3 (so 128+ is level
// 15), and lit levels look far brighter than linear: measured in the
// simulator, level 1 already shows at 96/255 and level 9 and up at full.
// SHOWN is that brightness per level; past level 9 nothing gets brighter.
const SHOWN = [0, 96, 131, 157, 179, 197, 214, 230, 244, 255]
/** Grey value that lands in the middle of `level`'s range. */
const greyFor = (level: number) => (level ? level * 8 + 4 : 0)

/**
 * Maps wanted on-glasses brightness (0-255) to grey values the glasses show
 * that way. Dithering spreads the error so in-between shades still read.
 */
function encodeShown(px: Uint8Array, w: number, h: number): Uint8Array {
  const buf = Float32Array.from(px)
  const out = new Uint8Array(w * h)
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = y * w + x
      const want = buf[i]
      let level = 0
      for (let l = 1; l < SHOWN.length; l++) if (Math.abs(SHOWN[l] - want) < Math.abs(SHOWN[level] - want)) level = l
      out[i] = greyFor(level)
      const err = want - SHOWN[level]
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

/** Stretched tones, paper inverted, then a gamma curve that lands the average on TONE_MEAN. */
function toneMap(px: Uint8Array) {
  stretch(px)
  let sum = px.reduce((a, v) => a + v, 0)
  if (sum / px.length > PAPER_MEAN) {
    for (let i = 0; i < px.length; i++) px[i] = 255 - px[i]
    sum = px.length * 255 - sum
  }
  const mean = Math.max(0.02, sum / px.length / 255)
  // Already dim images keep their tones (gamma 1).
  const gamma = Math.max(1, Math.min(MAX_GAMMA, Math.log(TONE_MEAN / 255) / Math.log(mean)))
  for (let i = 0; i < px.length; i++) px[i] = Math.round(255 * Math.pow(px[i] / 255, gamma))
}

async function toPng(px: Uint8Array, w: number, h: number): Promise<GlassImage> {
  const png = await sharp(Buffer.from(px), { raw: { width: w, height: h, channels: 1 } })
    .toColourspace('b-w')
    .png({ compressionLevel: 9 })
    .toBuffer()
  return { png: png.toString('base64'), w, h }
}

export async function glassImage(bytes: Buffer, w: number, h: number, treatment: Treatment = 'auto'): Promise<GlassImage> {
  const px = new Uint8Array(
    await sharp(bytes)
      .rotate()
      .flatten({ background: '#000000' })
      .resize(w, h, { fit: 'cover', position: 'attention' })
      // Shrinking softens edges; a light sharpen keeps detail readable at 4 bits.
      .sharpen({ sigma: 0.8 })
      .toColourspace('b-w')
      .raw()
      .toBuffer(),
  )
  const mean = px.reduce((a, v) => a + v, 0) / px.length

  // Small bright images are usually product shots on white: knock the
  // background out to black so only the product glows. The background is the
  // brightest broad region; its exact shade varies (white, off-white, cream),
  // so key the cut-off to the 90th percentile.
  if (treatment !== 'photo' && w < MIN_TONE_MAP_W && mean > BRIGHT_MEAN) {
    const p90 = Array.from(px).sort((a, b) => a - b)[Math.floor(px.length * 0.9)]
    const cut = Math.max(170, p90 - 18)
    for (let i = 0; i < px.length; i++) if (px[i] > cut) px[i] = 0
  }
  toneMap(px)
  return toPng(encodeShown(px, w, h), w, h)
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
