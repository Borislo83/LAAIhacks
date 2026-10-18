import { getAdvW } from '@evenrealities/pretext'

// The G2 fonts (evenroster, its Cyrillic/Greek and CJK fallbacks, and a small emoji set)
// lack many symbols; the firmware draws a missing glyph as a blank ("glyph dsc. not
// found for U+25B8"). Every string bound for the lens goes through lensText: a symbol
// with a plain equivalent in the font becomes it, any other missing glyph is dropped.

/** Missing glyph -> one the font has (or plain text). */
const PLAIN: Record<string, string> = {
  '◉': '●', '◎': '●', '⦿': '●', '⏺': '●',
  '▸': '▶', '►': '▶', '▹': '▷', '▻': '▷',
  '◂': '◀', '◄': '◀', '▾': '▼', '▴': '▲',
  '‣': '•', '◦': '•', '⁃': '-', '▪': '■', '⏹': '■',
  '✓': 'OK', '✔': 'OK', '☑': 'OK', '✗': 'x', '✘': 'x', '❌': 'x',
  '⚠': '!', '⭐': '★',
}

/** Whether the lens can draw this character (control characters and spaces always pass). */
export const drawable = (ch: string) => {
  const cp = ch.codePointAt(0)!
  return cp < 32 || /\s/.test(ch) || getAdvW(cp) > 0
}

/** Lens-safe text: symbols the G2 font lacks are mapped to plain ones or dropped. */
export function lensText(text: string): string {
  if (!text) return text
  // Emoji presentation selectors and joiners have no glyph ("▶️" is "▶" + U+FE0F).
  const chars = Array.from(text.replace(/[︎️‍⃣]/g, ''))
  let out = ''
  for (let i = 0; i < chars.length; i++) {
    const ch = chars[i]
    if (drawable(ch)) { out += ch; continue }
    const plain = PLAIN[ch]
    if (plain !== undefined) { out += plain; continue }
    // A dropped icon leads its words ("🔊 Volume 50%"): drop its space too.
    if ((!out || /\s$/.test(out)) && chars[i + 1] === ' ') i++
  }
  return out
}

interface LensPage {
  textObject?: { content?: string }[]
  listObject?: { itemContainer?: { itemName?: string[] } }[]
}
/** Makes every text and list label in a page container lens-safe (in place). */
export function lensPage<T extends LensPage>(page: T): T {
  for (const t of page.textObject ?? []) if (t.content) t.content = lensText(t.content)
  for (const l of page.listObject ?? []) {
    const item = l.itemContainer
    if (item?.itemName) item.itemName = item.itemName.map(name => lensText(name) || ' ')
  }
  return page
}

interface GlyphBridge {
  createStartUpPageContainer(page: never): Promise<unknown>
  rebuildPageContainer(page: never): Promise<unknown>
  textContainerUpgrade(update: never): Promise<unknown>
}
/** Routes every text the app sends to the glasses through lensText. */
export function guardGlyphs<B extends GlyphBridge>(bridge: B): B {
  const b = bridge as unknown as Record<string, (arg: unknown) => Promise<unknown>>
  for (const name of ['createStartUpPageContainer', 'rebuildPageContainer'] as const) {
    const send = b[name].bind(bridge)
    b[name] = page => send(lensPage(page as LensPage))
  }
  const upgrade = b.textContainerUpgrade.bind(bridge)
  b.textContainerUpgrade = update => {
    const u = update as { content?: string }
    if (u.content) u.content = lensText(u.content)
    return upgrade(u)
  }
  return bridge
}
