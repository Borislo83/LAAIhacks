import { getDocumentProxy, getMeta, getResolvedPDFJS } from 'unpdf'
import { FetchError } from './safe-fetch'

// PDF → a plain HTML article, so a PDF link goes through the same extraction
// and deck building as a web page. Text only: scanned PDFs (page images with
// no text layer) are refused rather than shown blank.
//
// Big files are read with HTTP range requests: pdf.js fetches only the parts
// it needs (the index, then the first pages), so a 300 MB textbook costs about
// what a small paper does.

/** Pages read from one PDF; the deck only uses the first part of long sources anyway. */
const MAX_PAGES = 60
/** Below this a PDF is almost certainly scanned images. */
const MIN_WORDS = 30
/** Paragraphs are rebuilt from sentences, about this many words each. */
const PARAGRAPH_WORDS = 90
/** Size of each range request pdf.js makes. */
const RANGE_CHUNK = 256 * 1024
/** A whole read, ranges included, gives up after this. */
const READ_TIMEOUT_MS = 60_000

type PdfDocument = Awaited<ReturnType<typeof getDocumentProxy>>

/** A PDF too big to download whole, read through range requests. */
export interface RangeSource {
  length: number
  fetchRange: (begin: number, end: number) => Promise<Uint8Array>
}

export const isPdf = (contentType: string, bytes: Uint8Array) =>
  /application\/pdf/i.test(contentType) || Buffer.from(bytes.subarray(0, 5)).toString('latin1') === '%PDF-'

export async function pdfToHtml(source: Uint8Array | RangeSource, url: string): Promise<string> {
  // A failed range request leaves pdf.js waiting; it's surfaced from here instead.
  let failed: unknown = null
  let pdf: PdfDocument
  try {
    if (source instanceof Uint8Array) pdf = await getDocumentProxy(source)
    else {
      const { PDFDataRangeTransport } = await getResolvedPDFJS()
      const transport = new PDFDataRangeTransport(source.length, null)
      transport.requestDataRange = (begin: number, end: number) => {
        source.fetchRange(begin, end).then(
          chunk => transport.onDataRange(begin, chunk),
          err => (failed = err),
        )
      }
      const open = getDocumentProxy(undefined, {
        range: transport,
        disableAutoFetch: true,
        disableStream: true,
        rangeChunkSize: RANGE_CHUNK,
      })
      pdf = await withTimeout(open, () => failed)
    }
  } catch (err) {
    if (err instanceof FetchError) throw err
    throw new FetchError("Couldn't open that PDF - it may be damaged or password-protected", 422)
  }
  try {
    return await withTimeout(read(pdf, url), () => failed)
  } finally {
    void pdf.loadingTask.destroy()
  }
}

async function read(pdf: PdfDocument, url: string): Promise<string> {
  const { info } = await getMeta(pdf).catch(() => ({ info: {} as Record<string, unknown> }))
  // Page by page, so a long book only loads the pages actually read.
  const pages: string[] = []
  for (let n = 1; n <= Math.min(pdf.numPages, MAX_PAGES); n++) {
    const page = await pdf.getPage(n)
    const content = await page.getTextContent()
    pages.push(content.items.map(item => ('str' in item ? item.str + (item.hasEOL ? '\n' : '') : '')).join(''))
    page.cleanup()
  }
  const words = pages.join(' ').split(/\s+/).filter(Boolean).length
  if (words < MIN_WORDS) throw new FetchError('This PDF is scanned pages with no text to read', 422)
  const title = (typeof info.Title === 'string' && info.Title.trim()) || firstPageTitle(pages[0] ?? '') || fileTitle(url)
  const body = pages.flatMap(paragraphs).map(p => `<p>${escape(p)}</p>`).join('\n')
  const more = pdf.numPages > MAX_PAGES ? `<p>${escape(`(First ${MAX_PAGES} of ${pdf.numPages} pages.)`)}</p>` : ''
  return `<!doctype html><html><head><title>${escape(title)}</title></head><body><article><h1>${escape(title)}</h1>\n${body}\n${more}</article></body></html>`
}

/** Settles with `work`, or rejects on a range failure or after READ_TIMEOUT_MS. */
function withTimeout<T>(work: Promise<T>, failure: () => unknown): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const started = Date.now()
    const timer = setInterval(() => {
      const err = failure()
      if (err) reject(err)
      else if (Date.now() - started > READ_TIMEOUT_MS) reject(new FetchError('That PDF took too long to read', 504))
      else return
      clearInterval(timer)
    }, 250)
    work.then(
      v => (clearInterval(timer), resolve(v)),
      e => (clearInterval(timer), reject(failure() ?? e)),
    )
  })
}

/** pdf.js gives a page as lines; rebuild readable paragraphs from them. */
function paragraphs(page: string): string[] {
  // Rejoin words hyphenated across a line break, then flatten the lines.
  const flat = page.replace(/(\w)-\n(\w)/g, '$1$2').replace(/\s+/g, ' ').trim()
  if (!flat) return []
  const sentences = flat.split(/(?<=[.!?]["”’)]?)\s+(?=["“‘(]?[A-Z0-9])/)
  const out: string[] = []
  let cur = ''
  for (const s of sentences) {
    cur = cur ? `${cur} ${s}` : s
    if (cur.split(' ').length >= PARAGRAPH_WORDS) {
      out.push(cur)
      cur = ''
    }
  }
  if (cur) out.push(cur)
  return out
}

/**
 * Many PDFs carry no title metadata. The title is then usually the first short
 * line on page 1 that isn't part of a running sentence (a notice above it wraps
 * onto lowercase lines) and doesn't end like one.
 */
function firstPageTitle(page: string): string | null {
  const lines = page.split('\n').map(l => l.trim()).filter(Boolean).slice(0, 15)
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const words = line.split(/\s+/).length
    const continues = /^[a-z]/.test(lines[i + 1] ?? '')
    if (words < 2 || words > 14 || continues || /[.,;:]$/.test(line) || /@|https?:/.test(line)) continue
    if (/[A-Za-z]{3}/.test(line)) return line
  }
  return null
}

function fileTitle(url: string) {
  const name = decodeURIComponent(new URL(url).pathname.split('/').pop() ?? '').replace(/\.pdf$/i, '')
  return name.replace(/[-_]+/g, ' ').trim() || 'PDF document'
}

const escape = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
