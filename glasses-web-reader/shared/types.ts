// Wire format between the condense server and the glasses app.

export type BlockType = 'h' | 'p' | 'li' | 'quote' | 'code'

export interface Block {
  type: BlockType
  text: string
}

export interface Section {
  /** Heading shown in the Contents menu. The first section may be untitled. */
  heading: string
  blocks: Block[]
}

export interface Link {
  text: string
  url: string
}

/** A G2-ready image: greyscale PNG at exactly `w`×`h`, base64 encoded. */
export interface GlassImage {
  png: string
  w: number
  h: number
}

// ── Smart mode ──────────────────────────────────────────────────────────────

export type SiteKind = 'news' | 'shop' | 'software' | 'forum' | 'live' | 'reference'

export interface ListItem {
  title: string
  meta: string
  /** Opening this item loads its page as a new deck. */
  url?: string
  image?: GlassImage
}

export type Screen =
  | { layout: 'headline'; headline: string; source: string; summary: string; image?: GlassImage }
  | { layout: 'points'; heading: string; points: string[]; quote?: { text: string; by: string } }
  | { layout: 'list'; heading: string; items: ListItem[] }
  | {
      layout: 'product'
      title: string
      subtitle: string
      price: string
      rating: string
      stock: string
      description: string
      url?: string
      image?: GlassImage
    }
  | { layout: 'tiles'; heading: string; intro: string; tiles: { label: string; value: string }[]; image?: GlassImage }
  | { layout: 'keys'; heading: string; keys: { key: string; action: string }[] }
  | { layout: 'split'; heading: string; verdict: string; left: { label: string; text: string }; right: { label: string; text: string } }
  | {
      layout: 'stat'
      value: string
      lines: string[]
      alert?: { title: string; detail: string }
      tiles: { label: string; value: string }[]
      valueImage?: GlassImage
    }
  | { layout: 'timeline'; heading: string; events: { date: string; text: string }[] }
  | { layout: 'compare'; heading: string; columns: { name: string; lines: string[] }[] }
  | { layout: 'step'; heading: string; index: number; total: number; title: string; detail: string; next?: string }
  | { layout: 'tagged'; heading: string; rows: { tag: string; text: string }[] }

export type Layout = Screen['layout']

export interface Deck {
  kind: SiteKind
  screens: Screen[]
}

export interface CondensedPage {
  url: string
  title: string
  site: string
  byline?: string
  /** Extracted text; the input Claude builds the deck from. */
  sections: Section[]
  links: Link[]
  wordCount: number
  /** AI-built screens; absent for manga. */
  deck?: Deck
  /** Set when the source was too long and only the first part was used. */
  truncated?: boolean
  /** Present for a MangaDex chapter: read as image strips instead of text. */
  manga?: MangaChapter
}

// ── Manga ───────────────────────────────────────────────────────────────────

export interface MangaChapter {
  chapterId: string
  series: string
  /** "Ch. 135 · Title" */
  chapter: string
  pages: number
  prev?: string
  next?: string
  seriesUrl: string
}

/** One full screen of a page: a 2x2 grid of 288x144 tiles (TL, TR, BL, BR). */
export interface MangaStrip {
  page: number
  strip: number
  /** Strips on this page. */
  strips: number
  tiles: GlassImage[]
}

export interface CondenseError {
  error: string
}

/**
 * /api/condense streams one of these per line (NDJSON): the page first, then
 * the deck's kind, then screens as Claude finishes each one, then done. A long
 * list arrives early with its first items and is later `replace`d whole.
 */
export type CondenseEvent =
  | { page: CondensedPage }
  | { kind: SiteKind }
  | { screens: Screen[] }
  | { replace: number; screen: Screen }
  | { done: true; truncated?: boolean }
  | CondenseError

// ── Voice ───────────────────────────────────────────────────────────────────

/**
 * POST /api/voice reply. The body sent is raw PCM from the glasses mic
 * (16 kHz, s16le, mono); the server turns it into the spoken instruction.
 */
export interface VoiceCommand {
  /** What the user said, or '' when nothing intelligible was heard. */
  transcript: string
  /** The transcript opened with the wake phrase ("Hey Reader"). */
  wake: boolean
  /** The instruction: the transcript with any wake phrase removed. */
  command: string
  durationMs: number
}

/** GET /api/search?q= reply: the one page picked for a voice command. */
export interface SearchResult {
  query: string
  url: string
  title: string
}

/** check: is this utterance for us? partial: live preview. final: the instruction. */
export type VoiceMode = 'check' | 'partial' | 'final'
