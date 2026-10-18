import type { CondenseEvent, CondenseError, MangaStrip, SearchResult, VoiceCommand, VoiceMode } from '../shared/types'
import { validBatch, type RecommendationBatch, type RecommendationContext } from '../shared/recommendations'

const API_BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

export async function recommendActions(context: RecommendationContext, signal: AbortSignal): Promise<RecommendationBatch> {
  const res = await fetch(`${API_BASE}/api/recommendations`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(context), signal: AbortSignal.any([signal, AbortSignal.timeout(30_000)]),
  })
  const body: unknown = await res.json()
  if (!res.ok || !validBatch(body) || body.page.url !== context.url) throw new Error('Recommendations unavailable.')
  return body
}

/**
 * Streams a page's condense events into `onEvent` (page, kind, screens…) and
 * resolves on done. Rejects on a server error or when `signal` aborts.
 */
export async function condense(url: string, signal: AbortSignal, onEvent: (e: CondenseEvent) => void): Promise<void> {
  const ctl = new AbortController()
  const abort = () => ctl.abort()
  signal.addEventListener('abort', abort)
  const timer = setTimeout(abort, 90_000)
  try {
    const res = await fetch(`${API_BASE}/api/condense?${new URLSearchParams({ url })}`, { signal: ctl.signal })
    if (!res.ok || !res.body) {
      const body = (await res.json().catch(() => ({}))) as Partial<CondenseError>
      throw new Error(body.error ?? `Server error ${res.status}`)
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { value, done } = await reader.read()
      buf += decoder.decode(value, { stream: !done })
      for (let nl = buf.indexOf('\n'); nl >= 0; nl = buf.indexOf('\n')) {
        const line = buf.slice(0, nl).trim()
        buf = buf.slice(nl + 1)
        if (!line) continue
        const event = JSON.parse(line) as CondenseEvent
        if ('error' in event) throw new Error(event.error)
        onEvent(event)
        if ('done' in event) return
      }
      if (done) throw new Error('The server closed the connection early')
    }
  } finally {
    clearTimeout(timer)
    signal.removeEventListener('abort', abort)
  }
}

export type MangaStyle = 'natural' | 'lines'

export async function fetchStrip(chapter: string, page: number, strip: number, style: MangaStyle = 'natural'): Promise<MangaStrip> {
  const qs = new URLSearchParams({ chapter, page: String(page), strip: String(strip), style })
  const res = await fetch(`${API_BASE}/api/manga/strip?${qs}`, { signal: AbortSignal.timeout(30_000) })
  const body = (await res.json().catch(() => ({ error: `Server error ${res.status}` }))) as MangaStrip | CondenseError
  if (!res.ok || 'error' in body) throw new Error('error' in body ? body.error : `Server error ${res.status}`)
  return body
}

/**
 * Sends glasses-mic PCM (16 kHz s16le mono) and gets back what was said.
 * Only a 'final' send is an instruction; 'check' and 'partial' are transcribed for the app.
 */
export async function sendVoice(pcm: Uint8Array<ArrayBuffer>, mode: VoiceMode = 'final'): Promise<VoiceCommand> {
  const res = await fetch(`${API_BASE}/api/voice?rate=16000&mode=${mode}`, {
    method: 'POST',
    headers: { 'content-type': 'application/octet-stream' },
    body: pcm,
    signal: AbortSignal.timeout(30_000),
  })
  const body = (await res.json().catch(() => ({ error: `Server error ${res.status}` }))) as VoiceCommand | CondenseError
  if (!res.ok || 'error' in body) throw new Error('error' in body ? body.error : `Server error ${res.status}`)
  return body
}

/** Asks the server for the one page that best answers a voice command. */
export async function searchPage(query: string): Promise<SearchResult> {
  const res = await fetch(`${API_BASE}/api/search?${new URLSearchParams({ q: query })}`, {
    signal: AbortSignal.timeout(60_000),
  })
  const body = (await res.json().catch(() => ({ error: `Server error ${res.status}` }))) as SearchResult | CondenseError
  if (!res.ok || 'error' in body) throw new Error('error' in body ? body.error : `Server error ${res.status}`)
  return body
}

/** Fire-and-forget timing report; the server prints it so real-device latency can be read. */
export function report(event: Record<string, unknown>) {
  fetch(`${API_BASE}/api/telemetry`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(event),
    keepalive: true,
  }).catch(() => {})
}
