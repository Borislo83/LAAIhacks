import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setDefaultResultOrder } from 'node:dns'

// IPv6 routes are broken on some venue Wi-Fi: reach the speech-to-text service over IPv4.
setDefaultResultOrder('ipv4first')

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

/** Silence added around a clip: StepFun returns nothing for short clips that start or end
 *  right on speech (a 2 s "Hey Focal, open YouTube"), and hears them fine when padded. */
const PAD_MS = 500
/** Per-attempt time limit and attempts for the speech-to-text request. */
const ATTEMPT_MS = 8_000
const ATTEMPTS = 3

export async function transcribe(pcm: Uint8Array, sampleRate: number, keep = true): Promise<string> {
  const pad = new Uint8Array(Math.round((sampleRate * 2 * PAD_MS) / 1000) & ~1)
  const padded = new Uint8Array(pad.length * 2 + pcm.length)
  padded.set(pcm, pad.length)
  const wav = toWav(padded, sampleRate)
  if (keep && process.env.VOICE_SAVE_DIR) void save(wav)

  const stt = provider()
  if (!stt) throw new Error('Voice needs STEPFUN_API_KEY or OPENAI_API_KEY on the server')

  const send = () => {
    const form = new FormData()
    form.append('file', new Blob([wav], { type: 'audio/wav' }), 'voice.wav')
    form.append('model', stt.model)
    form.append('response_format', 'json')
    form.append('language', LANGUAGE)
    return fetch(stt.url, {
      method: 'POST',
      headers: { authorization: `Bearer ${stt.key}` },
      body: form,
      signal: AbortSignal.timeout(ATTEMPT_MS),
    })
  }
  // On venue Wi-Fi about half the requests stall until the timeout while the rest answer in
  // 2-5 s, so each attempt gets a short timeout and a stalled, dropped ("fetch failed") or
  // 5xx attempt is retried on a fresh connection.
  let res: Response | null = null
  let lastError: unknown = null
  for (let attempt = 0; attempt < ATTEMPTS && !res; attempt++) {
    try {
      const r = await send()
      if (r.status >= 500 && attempt < ATTEMPTS - 1) { lastError = new Error(`Transcription failed (${r.status})`); continue }
      res = r
    } catch (err) {
      lastError = err
      await new Promise(r => setTimeout(r, 200))
    }
  }
  if (!res) throw lastError ?? new Error('Transcription failed')
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
// "Hey Focal" → wake (the question comes next). Speech-to-text writes the phrase
// loosely ("Hey, Focal." / "Hey-focal" / "Hey vocal" / "Hey, for cal"). The
// greeting must be a real word; the name after it may be split over a few words
// and a letter off, or be a known mishearing. It can come anywhere
// ("doing great, hey focal"). A long, unusual name may also open a sentence on
// its own; a short one like "focal" sounds like "local" and "vocal", so it never
// does: "a local store" or "focus on the task" don't wake it.

const WAKE_NAME = (process.env.WAKE_NAME ?? 'focal').toLowerCase().replace(/[^a-z]/g, '')
const DEFAULT_ALIASES: Record<string, string> = {
  // Mishearings of "focal", each only after a greeting ("hey vocal", "hey for cal").
  focal: 'vocal,focus,fokal,phocal,focall,focul,focle,fockle,forcal,forcall,folkal,folkall,fogal,vocals,focals,foco,foko,fokko,focco,phoco,foca',
  search: 'searches,searched,searching,searcher,serch,sirch,surch,searcha,research',
  searcher: 'search,searchher,researcher,searching,sercher,surcher,searcha,sirsure',
  reader: 'rita,reda,rider,raider,ritter,riter,reeta',
}
const WAKE_ALIASES = new Set(
  (process.env.WAKE_ALIASES ?? DEFAULT_ALIASES[WAKE_NAME] ?? '')
    .split(',').map(a => a.trim().toLowerCase().replace(/[^a-z]/g, '')).filter(Boolean),
)
const GREETINGS = new Set(['hey', 'hi', 'hello', 'ok', 'okay', 'yo', 'hay', 'hei', 'hej', 'heya', 'hiya', 'heyy', 'oi'])
/** "Hey" as speech-to-text sometimes writes it, flat or clipped ("A, focal.", "Eh focal").
 *  After these only a sound-alike of the name counts, never a near spelling like "local". */
const WEAK_GREETINGS = new Set(['a', 'eh', 'he', 'ay', 'aye', 'ah', 'uh', 'hm', 'hmm', 'k', 'kay', 'yeah'])

/**
 * How a word sounds, roughly: ph/v as f; c/k/g/q as k; r or l before that k dropped
 * ("forcal", "folkal"); vowels, h, w and y dropped; repeats collapsed.
 * "focal", "vocal", "fokal", "fogle", "phocal", "fickle", "for cal" all give "fkl".
 */
export function soundKey(word: string): string {
  return word.toLowerCase().replace(/[^a-z]/g, '')
    .replace(/ph/g, 'f').replace(/v/g, 'f').replace(/[cgq]/g, 'k')
    .replace(/[rl](?=k)/g, '')
    .replace(/[aeiouhwy]/g, '')
    .replace(/(.)\1+/g, '$1')
}
const NAME_KEY = soundKey(WAKE_NAME)
/** The name's sound, and with a dropped last consonant ("foco" for "focal"). */
const NAME_KEYS = new Set([NAME_KEY, NAME_KEY.slice(0, -1)].filter(k => k.length >= 2))
const soundsLikeName = (joined: string) => NAME_KEYS.has(soundKey(joined)) && soundKey(joined)[0] === NAME_KEY[0]
/** The name can be split over this many words ("for cal", "folk all"). */
const MAX_NAME_WORDS = 3
const NAME_SLIPS = WAKE_NAME.length >= 7 ? 2 : 1
const NAME_ALONE = WAKE_NAME.length >= 7

const letters = (word: string) => word.toLowerCase().replace(/[^a-z]/g, '')

/** How far these letters are from the name (after a greeting); null when not close. */
function nameDistance(joined: string, slips: number): number | null {
  if (WAKE_ALIASES.has(joined) || soundsLikeName(joined)) return 0
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
    // On a tie the longer match wins, so "hey for cal" doesn't leave "cal" behind.
    if (distance !== null && (!best || distance <= best.distance)) best = { end, distance }
  }
  return best?.end ?? null
}

/** After a bare "Hey" opening the sentence, these start a command ("Hey, open YouTube"). */
const COMMAND_START = new Set([
  'open', 'play', 'pause', 'resume', 'skip', 'stop', 'next', 'previous', 'show', 'hide', 'send', 'text',
  'message', 'imessage', 'tell', 'add', 'put', 'compare', 'find', 'search', 'look', 'pull', 'bring',
  'take', 'go', 'launch', 'turn', 'please', 'give', 'check', 'read', 'reviews', 'price', 'prices',
  'weather', 'latest', 'news',
])
// Small talk ("Hey, how are you?", "hey what's up") never counts: only action words do.

/** Where a sound-alike of the name ends if it starts at `from` (after a weak greeting). */
function matchSound(words: string[], from: number): number | null {
  let joined = ''
  let found: number | null = null
  for (let end = from + 1; end <= Math.min(words.length, from + MAX_NAME_WORDS); end++) {
    joined += letters(words[end - 1])
    if (soundsLikeName(joined) || WAKE_ALIASES.has(joined) && joined !== 'focus') found = end
  }
  return found
}

export function parseWake(transcript: string): { wake: boolean; command: string } {
  const text = transcript.trim()
  // Wake matching looks only at words with letters, but the command is cut from the
  // original text so numbers and hyphens survive ("AirPods Pro 3", "WH-1000XM5").
  const tokens = [...text.matchAll(/[^\s\-–—]+/g)].filter(m => letters(m[0]))
  const words = tokens.map(m => m[0])
  const after = (end: number) => {
    const last = tokens[end - 1]
    const rest = last ? text.slice(last.index! + last[0].length) : text
    return { wake: true, command: rest.replace(/^[\s,.!?;:\-–—]+/, '').trim() }
  }
  // A plain "Hey" opening the sentence with a command right after it ("Hey, open YouTube").
  // Only at the start: a "hey" mid-conversation is someone else's.
  if (words.length > 1 && ['hey', 'heya', 'hey'].includes(letters(words[0])) && COMMAND_START.has(letters(words[1]))) {
    return after(1)
  }
  for (let i = 0; i < words.length; i++) {
    const w = letters(words[i])
    // "Heyfocal" written as one word.
    for (const g of GREETINGS) {
      const rest = w.slice(g.length)
      if (w.startsWith(g) && rest.length > 3 && (WAKE_ALIASES.has(rest) || editDistance(rest, WAKE_NAME) <= 1)) return after(i + 1)
    }
    if (GREETINGS.has(w)) {
      const end = matchName(words, i + 1, NAME_SLIPS)
      if (end !== null) return after(end)
    }
    if (WEAK_GREETINGS.has(w)) {
      // "A, focal. ..." / "Eh focal, ...": the name starts like it and a pause follows,
      // so "a vocal performance" or "a focal length" don't wake it.
      const end = matchSound(words, i + 1)
      const first = end === null ? '' : letters(words[i + 1])
      const paused = end !== null && (/[,.!?:;]$/.test(words[i]) || /[,.!?:;]$/.test(words[end - 1]))
      if (end !== null && paused && soundKey(first)[0] === NAME_KEY[0] && first[0] !== 'v') return after(end)
    }
    // The name alone only opens a sentence, and must be near exact.
    if (NAME_ALONE && i === 0 && editDistance(w, WAKE_NAME) <= 1) return after(1)
    // "Focal, open YouTube": a short name opens a sentence only when it sounds like
    // the name and starts like it (so "vocal" or "local" at the start doesn't wake).
    if (!NAME_ALONE && i === 0 && w[0] === WAKE_NAME[0] && words.length > 1 && soundsLikeName(w) && !/^[a-z]+s$/.test(w)) {
      if (/[,.!?:;]$/.test(words[0])) return after(1)
    }
  }
  return { wake: false, command: text }
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

// ── Spoken product codes ────────────────────────────────────────────────────
// Speech-to-text spells model numbers out ("sony w h one thousand x m five"). Join runs of
// single letters and digits, and turn number words into digits only inside such a run, so
// "WH 1000 XM5" reaches the agents while "text Legend one sec" is left alone.
const UNITS: Record<string, number> = { zero: 0, oh: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6,
  seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15,
  sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19 }
const TENS: Record<string, number> = { twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60, seventy: 70, eighty: 80, ninety: 90 }
const isNumberWord = (w: string) => w in UNITS || w in TENS || w === 'hundred' || w === 'thousand'
const isCodeToken = (w: string) => /^[a-z0-9]$/i.test(w) || /^\d+$/.test(w)

/** "one thousand" -> 1000, "twenty five" -> 25; null when the words aren't a number. */
function wordsToNumber(words: string[]): number | null {
  let total = 0, current = 0
  for (const w of words) {
    if (w in UNITS) current += UNITS[w]
    else if (w in TENS) current += TENS[w]
    else if (w === 'hundred') current = (current || 1) * 100
    else if (w === 'thousand') { total += (current || 1) * 1000; current = 0 }
    else return null
  }
  return total + current
}

export function normalizeSpokenCodes(command: string): string {
  const raw = command.split(/\s+/).filter(Boolean)
  const bare = raw.map(w => w.toLowerCase().replace(/[.,!?;:]+$/, ''))
  const out: string[] = []
  let i = 0
  while (i < raw.length) {
    // A run of code tokens and number words that touches at least one single letter/digit.
    let j = i
    while (j < raw.length && (isCodeToken(bare[j]) || isNumberWord(bare[j]))) j++
    const run = bare.slice(i, j)
    const hasCode = run.some(isCodeToken)
    const letters = run.filter(w => /^[a-z]$/.test(w))
    // Lone "a"/"I" in ordinary speech is not a code.
    if (j - i >= 2 && hasCode && !(letters.length === run.length && run.every(w => w === 'a' || w === 'i'))) {
      const parts: string[] = []
      let k = 0
      while (k < run.length) {
        if (isNumberWord(run[k])) {
          let m = k
          while (m < run.length && isNumberWord(run[m])) m++
          const n = wordsToNumber(run.slice(k, m))
          parts.push(n === null ? run.slice(k, m).join(' ') : String(n)); k = m
        } else { parts.push(run[k].toUpperCase()); k++ }
      }
      // Letters join into one word; a digit glued to letters stays glued ("X M 5" -> "XM5").
      const joined = parts.reduce((acc, part) => {
        const prev = acc[acc.length - 1]
        if (prev !== undefined && /^[A-Z]+\d*$/.test(prev) && !/\d$/.test(prev) && /^([A-Z]|\d)$/.test(part)) acc[acc.length - 1] = prev + part
        else acc.push(part)
        return acc
      }, [] as string[])
      const trail = raw[j - 1].match(/[.,!?;:]+$/)?.[0] ?? ''
      out.push(joined.join(' ') + trail)
      i = j
    } else {
      out.push(raw[i]); i++
    }
  }
  return out.join(' ')
}
