// Voice capture from the G2 mic array. The SDK delivers PCM frames (16 kHz,
// signed 16-bit little-endian, mono) through onEvenHubEvent.
//
// WakeListener runs while hands-free is on: it splits ambient sound into
// utterances and hands the start of each one over for a wake-phrase check.
// VoiceRecorder captures one command and decides when the user has finished.

export const SAMPLE_RATE = 16_000
const BYTES_PER_SEC = SAMPLE_RATE * 2
/** Hard cap on one utterance. */
const MAX_MS = 10_000
/** Give up when nothing louder than the noise floor arrives in this long. */
const NO_SPEECH_MS = 8_000
/** Quiet this long after speech ends the command (room for a thinking pause). */
const TRAILING_SILENCE_MS = 1_500
/** No pause ends the command this soon after the card appears, so there's time to start talking. */
const FIRST_PAUSE_MS = 3_000
/** RMS (of 32768) a frame needs to count as speech in a quiet room. */
const SPEECH_RMS = 900
/** In a noisy room speech must be this many times the background level. */
const OVER_NOISE = 2.2
/** After the user has spoken, a frame this far below their own level is not them
 *  (the glasses mic is close to the mouth; nearby talk is much quieter). */
const UNDER_PEAK = 0.3
/** Audio kept from just before speech starts, so the first syllable isn't clipped. */
const PREROLL_MS = 400
/** The wake phrase opens an utterance, so only its start is checked. */
const WAKE_CHECK_MS = 2_200
/** Quiet this long splits ambient speech into separate utterances. */
const UTTERANCE_GAP_MS = 550
/** Nonstop talk is cut into pieces this long, so a wake phrase in it isn't held up forever. */
const MAX_UTTERANCE_MS = 12_000
/** During nonstop sound, the latest stretch this long is checked for the wake phrase... */
const WAKE_WINDOW_MS = 3_000
/** ...every this much new audio, so "Hey Focal" mid-chatter is heard within seconds. */
const WAKE_RECHECK_MS = 2_000
const msToBytes = (ms: number) => Math.round((ms / 1000) * BYTES_PER_SEC) & ~1

export type StopReason = 'silence' | 'max' | 'no-speech'

/**
 * The room's background level: follows quiet frames quickly and loud ones slowly, so
 * steady chatter raises it and speech (short, louder bursts) doesn't.
 */
export class NoiseFloor {
  constructor(public level = 0) {}
  update(loud: number) {
    if (!this.level) this.level = loud
    else this.level = loud < this.level ? this.level * 0.85 + loud * 0.15 : this.level * 0.995 + loud * 0.005
  }
  /** What a frame needs to count as speech here. */
  get threshold() {
    return Math.max(SPEECH_RMS, this.level * OVER_NOISE)
  }
}

export class VoiceRecorder {
  private chunks: Uint8Array[] = []
  private bytes = 0
  private heardSpeech = false
  private lastLoudAt = 0
  private startedAt = Date.now()
  /** The user's own speaking level (decaying peak), once they have spoken. */
  private peak = 0
  /** Loudness of the latest frame, 0..1 (for a level meter). */
  level = 0

  /**
   * `onAutoStop` is called when the recorder decides the utterance is over.
   * `seed` is audio already captured (the wake utterance) to start from.
   * `noSpeechMs` is how long to wait for speech before giving up.
   */
  constructor(private onAutoStop: (reason: StopReason) => void, seed: Uint8Array[] = [],
    private floor = new NoiseFloor(), private noSpeechMs = NO_SPEECH_MS) {
    for (const c of seed) {
      this.chunks.push(c)
      this.bytes += c.length
    }
    // Only actual speech counts: a seed of background noise still waits for the command.
    const loudest = Math.max(0, ...seed.map(rms))
    if (loudest >= this.floor.threshold) {
      this.heardSpeech = true
      this.lastLoudAt = Date.now()
      this.peak = loudest
    }
  }

  /** Speech from the wearer: over the room's noise, and near their own level once heard. */
  private speaking(loud: number) {
    return loud >= Math.max(this.floor.threshold, this.peak * UNDER_PEAK)
  }

  push(pcm: Uint8Array) {
    if (!pcm.length) return
    this.chunks.push(pcm)
    this.bytes += pcm.length
    const now = Date.now()
    const loud = rms(pcm)
    this.level = Math.min(1, loud / (SPEECH_RMS * 4))
    if (this.speaking(loud)) {
      this.heardSpeech = true
      this.lastLoudAt = now
      this.peak = Math.max(this.peak, loud)
    } else this.floor.update(loud)
    // The peak decays slowly, so one shout doesn't make normal speech count as quiet.
    this.peak *= 0.998
    const elapsed = (this.bytes / BYTES_PER_SEC) * 1000
    if (elapsed >= MAX_MS) this.onAutoStop('max')
    else if (this.heardSpeech && now - this.lastLoudAt >= TRAILING_SILENCE_MS && now - this.startedAt >= FIRST_PAUSE_MS) {
      this.onAutoStop('silence')
    }
    else if (!this.heardSpeech && now - this.startedAt >= this.noSpeechMs) this.onAutoStop('no-speech')
  }

  get hasSpeech() {
    return this.heardSpeech
  }

  get durationMs() {
    return Math.round((this.bytes / BYTES_PER_SEC) * 1000)
  }

  /** All captured PCM as one buffer. */
  pcm(): Uint8Array<ArrayBuffer> {
    return concat(this.chunks)
  }
}

export class WakeListener {
  private preroll: Uint8Array[] = []
  private prerollBytes = 0
  /** The utterance in progress, pre-roll included; null between utterances. */
  private utterance: Uint8Array[] | null = null
  private utteranceBytes = 0
  private lastLoudAt = 0
  private checked = false
  /** Bytes of the utterance at its last wake check. */
  private checkedAt = 0
  /** The room's background level, shared with the capture that follows a wake. */
  readonly floor = new NoiseFloor()
  level = 0

  /**
   * `onCheck` gets the start of each utterance once. The array it receives keeps
   * filling until the utterance ends, so a confirmed wake can carry on from it.
   * `onEnd` gets the same array once the utterance is over.
   */
  constructor(
    private onCheck: (utterance: Uint8Array[]) => void,
    private onEnd: (utterance: Uint8Array[]) => void,
    /** During nonstop sound: the latest few seconds, and where they start in the utterance. */
    private onRecheck: (window: Uint8Array[], from: number, utterance: Uint8Array[]) => void = () => {},
  ) {}

  /** The utterance from chunk `from` on (what was said since a recheck's window began). */
  tail(utterance: Uint8Array[], from: number): Uint8Array[] {
    return utterance.slice(from)
  }

  /** The utterance in progress (or the pre-roll before one), so a capture can pick up from it. */
  get current(): Uint8Array[] {
    return this.utterance ?? this.preroll
  }

  push(pcm: Uint8Array) {
    if (!pcm.length) return
    const loud = rms(pcm)
    this.level = Math.min(1, loud / (SPEECH_RMS * 4))
    const now = Date.now()
    const speech = loud >= this.floor.threshold
    if (!speech) this.floor.update(loud)
    if (!this.utterance) {
      this.preroll.push(pcm)
      this.prerollBytes += pcm.length
      while (this.prerollBytes - this.preroll[0].length >= msToBytes(PREROLL_MS)) {
        this.prerollBytes -= this.preroll.shift()!.length
      }
      if (!speech) return
      this.utterance = this.preroll
      this.utteranceBytes = this.prerollBytes
      this.preroll = []
      this.prerollBytes = 0
      this.lastLoudAt = now
      this.checked = false
      this.checkedAt = 0
      return
    }
    this.utterance.push(pcm)
    this.utteranceBytes += pcm.length
    if (speech) this.lastLoudAt = now
    const ended = now - this.lastLoudAt >= UTTERANCE_GAP_MS || this.utteranceBytes >= msToBytes(MAX_UTTERANCE_MS)
    if (!this.checked && (ended || this.utteranceBytes >= msToBytes(WAKE_CHECK_MS))) {
      this.checked = true
      this.checkedAt = this.utteranceBytes
      this.onCheck(this.utterance)
    } else if (this.checked && !ended && this.utteranceBytes - this.checkedAt >= msToBytes(WAKE_RECHECK_MS)) {
      // Still going: check the latest few seconds, so a wake phrase mid-chatter is heard now.
      this.checkedAt = this.utteranceBytes
      let from = this.utterance.length, bytes = 0
      while (from > 0 && bytes < msToBytes(WAKE_WINDOW_MS)) bytes += this.utterance[--from].length
      this.onRecheck(this.utterance.slice(from), from, this.utterance)
    }
    if (ended) {
      this.onEnd(this.utterance)
      this.utterance = null
    }
  }
}

export function concat(chunks: Uint8Array[]): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(chunks.reduce((n, c) => n + c.length, 0))
  let at = 0
  for (const c of chunks) {
    out.set(c, at)
    at += c.length
  }
  return out
}

function rms(pcm: Uint8Array) {
  const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength)
  const n = pcm.byteLength >> 1
  if (!n) return 0
  let sum = 0
  for (let i = 0; i < n; i++) {
    const s = view.getInt16(i * 2, true)
    sum += s * s
  }
  return Math.sqrt(sum / n)
}
