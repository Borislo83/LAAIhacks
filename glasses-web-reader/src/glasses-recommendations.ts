import { pxTruncate } from '@evenrealities/pretext'
import type { PageSpec, TextBox } from './screens'

const PANEL = { x: 216, y: 132, w: 360, h: 156 }
export const RECOMMENDATION_LAYOUTS = [
  { id: 'corner', label: 'Corner popup' },
  { id: 'bottom', label: 'Bottom strip' },
  { id: 'full', label: 'Full-screen list' },
] as const
export type RecommendationLayout = typeof RECOMMENDATION_LAYOUTS[number]['id']
const clean = (text: string) => text.replace(/[\r\n\t\u0000-\u001f]/g, ' ')
export const recommendationCursor = (index: number, delta: 1 | -1) => Math.max(0, Math.min(3, index + delta))

/** G2 containers are transparent: clip the underlying text rather than overlap it. */
export function glassesRecommendationSpec(base: PageSpec, titles: string[], cursor: number, selected = false,
  layout: RecommendationLayout = 'bottom'): PageSpec {
  const heading = selected ? 'Selected; not run' : 'Recommended actions'
  const choices = [...titles.slice(0, 3), 'Close recommendations']
  const row = (title: string, index: number, width: number) =>
    `${index === cursor ? '>' : ' '} ${pxTruncate(clean(title), width - 40)}`
  if (layout === 'full') return { images: [], texts: [
    { x: 0, y: 0, w: 576, h: 36, text: heading },
    ...choices.map((title, index) => ({ x: 0, y: 40 + index * 53, w: 576, h: 46,
      text: row(title, index, 576), border: 1, radius: 6, pad: 8 })),
    { x: 0, y: 258, w: 576, h: 30, text: 'Swipe: choose | Tap: select | Double: close', pad: 0 },
  ] }
  const panel = layout === 'bottom' ? { x: 0, y: 180, w: 576, h: 108 } : PANEL
  const overlaps = (box: { x: number; y: number; w: number; h: number }) =>
    box.x < panel.x + panel.w && box.x + box.w > panel.x && box.y < panel.y + panel.h && box.y + box.h > panel.y
  const texts: TextBox[] = base.texts.flatMap(box => {
    if (!overlaps(box)) return [{ ...box, scroll: false }]
    const height = panel.y - box.y - 4
    return height >= 35 ? [{ ...box, h: height, scroll: false }] : []
  }).slice(0, layout === 'bottom' ? 2 : 6)
  if (layout === 'bottom') {
    texts.push({ x: 0, y: 180, w: 576, h: 32, text: heading, pad: 0 })
    texts.push(...choices.map((title, index) => ({ x: (index % 2) * 288, y: 214 + Math.floor(index / 2) * 37,
      w: 284, h: 36, text: row(title, index, 284), border: 1, radius: 5, pad: 2 })))
  } else texts.push({ ...PANEL, text: [heading, ...choices.map((title, index) => row(title, index, PANEL.w))].join('\n'),
    pad: 4, border: 1, color: 13, radius: 8 })
  const images = base.images.filter(image => !overlaps({ x: image.x, y: image.y, w: image.image.w, h: image.image.h })).slice(0, 4)
  return { texts, images }
}
