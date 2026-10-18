import { getTextWidth, pxTruncate } from '@evenrealities/pretext'
import type { PageSpec } from './screens'
import { SPINNER_FRAMES, SPINNER_SIZE } from './spinner-frames'

// Loading screen: the page being opened at the top, a small spinning ring in
// the middle and the current step under it. The ring is a pre-rendered image
// (scripts/gen-spinner.mts) swapped in place each frame; frames are ~550 bytes
// so a swap is one small BLE write. Text is always left-aligned on G2, so the
// title and step lines are centred with spaces at the font's real widths.

/** How often to try a frame; one is skipped while the previous is still in flight. */
export const SPIN_MS = 60
/** One full turn of the ring, whatever the frame rate. */
const TURN_MS = 900

/** The frame the ring should show now. */
export const clockFrame = () => Math.floor((performance.now() / TURN_MS) * SPINNER_FRAMES.length)
export const SPINNER_X = 288 - SPINNER_SIZE / 2
export const SPINNER_Y = 106

/** Centres one line in a box of inner width `w`. */
function centred(text: string, w: number) {
  const t = pxTruncate(text, w)
  return ' '.repeat(Math.max(0, Math.floor((w - getTextWidth(t)) / 2 / getTextWidth(' ')))) + t
}

export const spinnerFrame = (frame: number) => ({
  png: SPINNER_FRAMES[((frame % SPINNER_FRAMES.length) + SPINNER_FRAMES.length) % SPINNER_FRAMES.length],
  w: SPINNER_SIZE,
  h: SPINNER_SIZE,
})

export function loadingSpec(title: string, host: string, step: string, frame: number): PageSpec {
  const inner = 576 - 2 * 4
  return {
    texts: [
      { x: 0, y: 24, w: 576, h: 62, text: `${centred(title, inner)}\n${centred(title === host ? 'Opening page' : host, inner)}`, pad: 4 },
      { x: 0, y: 178, w: 576, h: 36, text: centred(step, inner), pad: 4 },
      { x: 0, y: 248, w: 576, h: 36, text: centred('Swipe to cancel', inner), pad: 4 },
    ],
    images: [{ x: SPINNER_X, y: SPINNER_Y, image: spinnerFrame(frame) }],
  }
}
