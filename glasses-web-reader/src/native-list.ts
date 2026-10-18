import { getTextWidth } from '@evenrealities/pretext'
import type { PageSpec, TextBox } from './screens'

// The firmware's native list (menu, contents, links...) drawn for the phone mirror,
// measured off the simulator: 40px rows under the 36px header, a 1px rounded border
// round the selected item only, unselected items dimmed. Swiping moves the highlight
// at once (no slide) and scrolls whole rows, keeping one neighbour of the selection in
// view; at the end the list sits flush with the bottom edge.
//
// The firmware never reports list swipes, so the app can't follow the real highlight;
// the mirror keeps its own cursor for the console remote.

const HEADER_H = 36
const PAD = 4
const ROW = 40
const TOP = HEADER_H + PAD
const VIEW_H = 288 - HEADER_H - 2 * PAD
// Border 1 + pad 5 + a leading space puts the text at x=16, where the firmware's list has it.
const ITEM_PAD = 5
/** Unselected items, as dim as the firmware's own list draws them. */
const DIM_TEXT = 0

/** Scroll offset (px) after the selection moves to `selected`, from offset `scroll`. */
export function listScroll(scroll: number, selected: number, count: number) {
  const max = Math.max(0, count * ROW - VIEW_H)
  const above = Math.max(0, selected - 1) * ROW
  const below = Math.min(count, selected + 2) * ROW
  if (above < scroll) scroll = above
  else if (below > scroll + VIEW_H) scroll = Math.ceil((below - VIEW_H) / ROW) * ROW
  return Math.min(scroll, max)
}

export function nativeListSpec(title: string, labels: string[], selected: number, scroll: number): PageSpec {
  const texts: TextBox[] = [{ x: 0, y: 0, w: 576, h: HEADER_H, text: title }]
  labels.forEach((label, i) => {
    const y = TOP + i * ROW - scroll
    if (y < TOP || y + ROW > TOP + VIEW_H) return
    texts.push({
      x: PAD, y, w: getTextWidth(label) + 26, h: ROW, text: ` ${label}`, pad: ITEM_PAD, border: 1, radius: 8,
      ...(i === selected ? {} : { color: 0, textColor: DIM_TEXT }),
    })
  })
  return { texts, images: [] }
}
