import { lookup } from 'node:dns/promises'
import { isIP } from 'node:net'
import { isPdf, pdfToHtml } from './pdf'

// The server fetches arbitrary user-supplied URLs, so it must not become a
// way to reach the host's private network (SSRF). Every hop of a redirect
// chain is resolved and checked before it is requested.

const MAX_BYTES = 4 * 1024 * 1024
// A cut-off PDF can't be parsed at all. PDFs above this are read in ranges
// when the server allows it; otherwise they're downloaded whole, up to the cap.
const PDF_RANGE_FROM = 8 * 1024 * 1024
const MAX_PDF_BYTES = 100 * 1024 * 1024
const PDF_TIMEOUT_MS = 60_000
const MAX_REDIRECTS = 5
const TIMEOUT_MS = 12_000
const ALLOW_PRIVATE = process.env.ALLOW_PRIVATE_HOSTS === '1'

const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) ' +
  'Chrome/130.0 Safari/537.36 GlassesWebReader/0.1'

export class FetchError extends Error {
  constructor(message: string, readonly status = 400) {
    super(message)
  }
}

function isPrivateAddress(addr: string): boolean {
  if (isIP(addr) === 4) {
    const [a, b] = addr.split('.').map(Number)
    return (
      a === 0 || a === 10 || a === 127 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 168) ||
      (a === 198 && (b === 18 || b === 19)) ||
      a >= 224
    )
  }
  const v6 = addr.toLowerCase()
  if (v6.startsWith('::ffff:')) return isPrivateAddress(v6.slice(7))
  return (
    v6 === '::' || v6 === '::1' ||
    v6.startsWith('fc') || v6.startsWith('fd') ||
    v6.startsWith('fe8') || v6.startsWith('fe9') || v6.startsWith('fea') || v6.startsWith('feb') ||
    v6.startsWith('ff')
  )
}

export async function assertPublic(url: URL) {
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new FetchError('Only http(s) URLs are supported')
  }
  if (ALLOW_PRIVATE) return
  const host = url.hostname.replace(/^\[|\]$/g, '')
  const addrs = isIP(host) ? [host] : (await lookup(host, { all: true }).catch(() => [])).map(a => a.address)
  if (!addrs.length) throw new FetchError(`Could not resolve ${host}`)
  if (addrs.some(isPrivateAddress)) throw new FetchError('Refusing to fetch a private network address', 403)
}

export interface FetchedDoc {
  finalUrl: string
  contentType: string
  body: string
  /** The source was a PDF; `body` is its text rebuilt as an HTML article. */
  pdf?: boolean
}

function refererFor(url: URL): { referer: string } | Record<string, never> {
  const host = url.hostname
  if (/(?:^|\.)gr-assets\.com$|(?:^|\.)goodreads\.com$/.test(host)) return { referer: 'https://www.goodreads.com/' }
  if (/(?:^|\.)media-amazon\.com$|(?:^|\.)ssl-images-amazon\.com$/.test(host)) return { referer: 'https://www.amazon.com/' }
  return {}
}

/** Follows redirects itself so every hop is checked against private ranges. */
async function fetchPublic(
  input: string,
  accept: string,
  extra: { headers?: Record<string, string>; timeoutMs?: number } = {},
): Promise<{ res: Response; url: URL }> {
  let url: URL
  try {
    url = new URL(/^[a-z]+:\/\//i.test(input) ? input : `https://${input}`)
  } catch {
    throw new FetchError('That does not look like a URL')
  }

  for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
    await assertPublic(url)
    const res = await fetch(url, {
      redirect: 'manual',
      signal: AbortSignal.timeout(extra.timeoutMs ?? TIMEOUT_MS),
      headers: { 'user-agent': UA, accept, ...refererFor(url), ...extra.headers },
    }).catch(err => {
      throw new FetchError(`Fetch failed: ${err?.cause?.code ?? err?.name ?? err}`, 502)
    })

    if (res.status >= 300 && res.status < 400 && res.headers.get('location')) {
      url = new URL(res.headers.get('location')!, url)
      continue
    }
    if (!res.ok) throw new FetchError(`Site responded ${res.status}`, 502)
    return { res, url }
  }
  throw new FetchError('Too many redirects', 502)
}

export async function safeFetch(input: string): Promise<FetchedDoc> {
  const { res, url } = await fetchPublic(input, 'text/html,application/xhtml+xml,text/plain;q=0.9,application/pdf;q=0.8,*/*;q=0.5')
  const contentType = res.headers.get('content-type') ?? ''
  if (/text\/html|application\/xhtml|text\/plain/i.test(contentType)) {
    return { finalUrl: url.toString(), contentType, body: decode(res, await readBytes(res)) }
  }
  // PDFs, including ones served as a generic download (checked by their header).
  if (/application\/(pdf|octet-stream|x-pdf)/i.test(contentType) || /\.pdf$/i.test(url.pathname)) {
    const finalUrl = url.toString()
    const length = Number(res.headers.get('content-length') ?? 0)
    // Big and the server serves slices: read only the parts pdf.js asks for.
    if (length > PDF_RANGE_FROM && /bytes/i.test(res.headers.get('accept-ranges') ?? '') && /pdf/i.test(contentType + url.pathname)) {
      await res.body?.cancel()
      const fetchRange = (begin: number, end: number) => fetchRangeBytes(finalUrl, begin, end)
      return { finalUrl, contentType: 'text/html', body: await pdfToHtml({ length, fetchRange }, finalUrl), pdf: true }
    }
    if (length > MAX_PDF_BYTES) throw new FetchError('That PDF is too big to read (over 100 MB)', 413)
    // Downloaded whole: big files get longer than the page timeout allows.
    let body = res
    if (length > MAX_BYTES) {
      await res.body?.cancel()
      body = (await fetchPublic(finalUrl, 'application/pdf,*/*', { timeoutMs: PDF_TIMEOUT_MS })).res
    }
    const bytes = await readBytes(body, MAX_PDF_BYTES)
    if (isPdf(contentType, bytes)) {
      return { finalUrl, contentType: 'text/html', body: await pdfToHtml(new Uint8Array(bytes), finalUrl), pdf: true }
    }
  }
  throw new FetchError(`Unsupported content type: ${contentType || 'unknown'}`, 415)
}

/** One slice of a file, for pdf.js range reads (`end` is exclusive). */
async function fetchRangeBytes(url: string, begin: number, end: number): Promise<Uint8Array> {
  const { res } = await fetchPublic(url, 'application/pdf,*/*', { headers: { range: `bytes=${begin}-${end - 1}` } })
  if (res.status !== 206) {
    await res.body?.cancel()
    throw new FetchError('The site stopped serving parts of that PDF', 502)
  }
  return new Uint8Array(await readBytes(res, end - begin + 1024))
}

export async function safeFetchImage(input: string): Promise<Buffer> {
  const { res } = await fetchPublic(input, 'image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8')
  const contentType = res.headers.get('content-type') ?? ''
  if (!/^image\//i.test(contentType) || /svg/i.test(contentType)) {
    throw new FetchError(`Not a raster image: ${contentType || 'unknown'}`, 415)
  }
  return readBytes(res)
}

async function readBytes(res: Response, limit = MAX_BYTES): Promise<Buffer> {
  const reader = res.body?.getReader()
  if (!reader) return Buffer.alloc(0)
  const chunks: Uint8Array[] = []
  let total = 0
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    total += value.byteLength
    if (total > limit) {
      await reader.cancel()
      if (limit === MAX_PDF_BYTES) throw new FetchError('That PDF is too big to read (over 100 MB)', 413)
      break
    }
    chunks.push(value)
  }
  return Buffer.concat(chunks)
}

function decode(res: Response, bytes: Buffer): string {
  const charset = /charset=([\w-]+)/i.exec(res.headers.get('content-type') ?? '')?.[1] ?? 'utf-8'
  let decoder: TextDecoder
  try {
    decoder = new TextDecoder(charset)
  } catch {
    decoder = new TextDecoder('utf-8')
  }
  return decoder.decode(bytes)
}
