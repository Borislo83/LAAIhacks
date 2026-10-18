import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

// Speech-to-text for voice commands from the glasses mic.
//
// The glasses app posts raw PCM (s16le mono, 16 kHz by default). It is wrapped
// as a WAV and sent to an OpenAI-compatible transcription API (Claude has no
// audio input): StepFun when STEPFUN_API_KEY is set, otherwise OpenAI.
// Set VOICE_SAVE_DIR to keep each utterance as a .wav for debugging.

const LANGUAGE = process.env.STT_LANGUAGE ?? 'en'
/** Any letter outside the Latin script (Chinese, Japanese, Cyrillic, ...). */
const NON_LATIN = /[^\p{Script=Latin}\p{Script=Common}\p{Script=Inherited}]/u

function provider() {
  if (process.env.STEPFUN_API_KEY) {
    return {
      url: 'https://api.stepfun.ai/v1/audio/transcriptions',
      key: process.env.STEPFUN_API_KEY,
      model: process.env.STT_MODEL ?? 'stepaudio-2.5-asr',
    }
  }
  if (process.env.OPENAI_API_KEY) {
    return {
      url: 'https://api.openai.com/v1/audio/transcriptions',
      key: process.env.OPENAI_API_KEY,
      model: process.env.STT_MODEL ?? 'gpt-4o-mini-transcribe',
    }
  }
  return null
}

export async function transcribe(pcm: Uint8Array, sampleRate: number, keep = true): Promise<string> {
  const wav = toWav(pcm, sampleRate)
  if (keep && process.env.VOICE_SAVE_DIR) void save(wav)

  const stt = provider()
  if (!stt) throw new Error('Voice needs STEPFUN_API_KEY or OPENAI_API_KEY on the server')

  const form = new FormData()
  form.append('file', new Blob([wav], { type: 'audio/wav' }), 'voice.wav')
  form.append('model', stt.model)
  form.append('response_format', 'json')
  form.append('language', LANGUAGE)
  const res = await fetch(stt.url, {
    method: 'POST',
    headers: { authorization: `Bearer ${stt.key}` },
    body: form,
    signal: AbortSignal.timeout(25_000),
  })
  const body = res.ok ? '' : await res.text()
  // Silence or breath noise: nothing was said, which isn't a failure.
  if (res.status === 400 && body.includes('no speech found')) return ''
  if (!res.ok) throw new Error(`Transcription failed (${res.status}): ${body.slice(0, 200)}`)
  const { text } = (await res.json()) as { text?: string }
  const out = (text ?? '').trim()
  // StepFun ignores the language hint, so speech in another language comes back
  // in its own script. English only: treat that as nothing said.
  if (LANGUAGE === 'en' && NON_LATIN.test(out)) return ''
  return out
}

function toWav(pcm: Uint8Array, sampleRate: number): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(44 + pcm.length)
  const v = new DataView(out.buffer)
  const ascii = (at: number, s: string) => [...s].forEach((ch, i) => v.setUint8(at + i, ch.charCodeAt(0)))
  ascii(0, 'RIFF')
  v.setUint32(4, 36 + pcm.length, true)
  ascii(8, 'WAVE')
  ascii(12, 'fmt ')
  v.setUint32(16, 16, true) // fmt chunk size
  v.setUint16(20, 1, true) // PCM
  v.setUint16(22, 1, true) // mono
  v.setUint32(24, sampleRate, true)
  v.setUint32(28, sampleRate * 2, true) // byte rate
  v.setUint16(32, 2, true) // block align
  v.setUint16(34, 16, true) // bits per sample
  ascii(36, 'data')
  v.setUint32(40, pcm.length, true)
  out.set(pcm, 44)
  return out
}

async function save(wav: Uint8Array) {
  try {
    const dir = process.env.VOICE_SAVE_DIR!
    await mkdir(dir, { recursive: true })
    await writeFile(join(dir, `voice-${Date.now()}.wav`), wav)
  } catch (err) {
    console.warn('[voice] could not save utterance:', err)
  }
}

// ── Wake phrase ─────────────────────────────────────────────────────────────
// "Hey Search" → wake (the question comes next). Speech-to-text writes the phrase
// loosely ("Hey, search." / "Hey-search" / "Hey searches" / "Hey, serch"). The
// greeting must be a real word; the name after it may be split over a few words
// and a letter or two off, or be a known mishearing. It can come anywhere
// ("doing great, hey search"). A long, unusual name may also open a sentence on
// its own; a short one like "search" is an ordinary word, so it never does.

const WAKE_NAME = (process.env.WAKE_NAME ?? 'search').toLowerCase().replace(/[^a-z]/g, '')
const DEFAULT_ALIASES: Record<string, string> = {
  search: 'searches,searched,searching,searcher,serch,sirch,surch,searcha,research',
  searcher: 'search,searchher,researcher,searching,sercher,surcher,searcha,sirsure',
  reader: 'rita,reda,rider,raider,ritter,riter,reeta',
}
const WAKE_ALIASES = new Set(
  (process.env.WAKE_ALIASES ?? DEFAULT_ALIASES[WAKE_NAME] ?? '')
    .split(',').map(a => a.trim().toLowerCase().replace(/[^a-z]/g, '')).filter(Boolean),
)
const GREETINGS = new Set(['hey', 'hi', 'hello', 'ok', 'okay', 'yo', 'hay'])
/** The name can be split over this many words ("search her"). */
const MAX_NAME_WORDS = 3
const NAME_SLIPS = WAKE_NAME.length >= 7 ? 2 : 1
const NAME_ALONE = WAKE_NAME.length >= 7

const letters = (word: string) => word.toLowerCase().replace(/[^a-z]/g, '')

/** How far these letters are from the name (after a greeting); null when not close. */
function nameDistance(joined: string, slips: number): number | null {
  if (WAKE_ALIASES.has(joined)) return 0
  const d = editDistance(joined, WAKE_NAME)
  return d <= slips ? d : null
}

/** Where the name ends if it starts at `from`: the longest of the closest matches. */
function matchName(words: string[], from: number, slips: number): number | null {
  let best: { end: number; distance: number } | null = null
  let joined = ''
  for (let end = from + 1; end <= Math.min(words.length, from + MAX_NAME_WORDS); end++) {
    joined += letters(words[end - 1])
    const distance = nameDistance(joined, slips)
    // On a tie the longer match wins, so "hey search her" doesn't leave "her" behind.
    if (distance !== null && (!best || distance <= best.distance)) best = { end, distance }
  }
  return best?.end ?? null
}

export function parseWake(transcript: string): { wake: boolean; command: string } {
  const words = transcript.trim().split(/[\s\-–—]+/).filter(w => letters(w))
  const after = (end: number) => ({
    wake: true,
    command: words.slice(end).join(' ').replace(/^[\s,.!?;:]+/, ''),
  })
  for (let i = 0; i < words.length; i++) {
    const w = letters(words[i])
    // "Heysearch" written as one word.
    for (const g of GREETINGS) {
      if (w.startsWith(g) && w.length > g.length + 3 && editDistance(w.slice(g.length), WAKE_NAME) <= 1) return after(i + 1)
    }
    if (GREETINGS.has(w)) {
      const end = matchName(words, i + 1, NAME_SLIPS)
      if (end !== null) return after(end)
    }
    // The name alone only opens a sentence, and must be near exact.
    if (NAME_ALONE && i === 0 && editDistance(w, WAKE_NAME) <= 1) return after(1)
  }
  return { wake: false, command: transcript.trim() }
}

function editDistance(a: string, b: string) {
  const row = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i++) {
    let prev = row[0]
    row[0] = i
    for (let j = 1; j <= b.length; j++) {
      const cur = row[j]
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, prev + (a[i - 1] === b[j - 1] ? 0 : 1))
      prev = cur
    }
  }
  return row[b.length]
}
