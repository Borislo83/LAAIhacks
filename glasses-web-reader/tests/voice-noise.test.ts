import { test } from 'node:test'
import assert from 'node:assert/strict'
import { VoiceRecorder, WakeListener } from '../src/voice'

const FRAME_MS = 40
/** A 40 ms frame of a square wave at this RMS level. */
function frame(level: number) {
  const pcm = new Uint8Array(16_000 * 2 * FRAME_MS / 1000)
  const view = new DataView(pcm.buffer)
  for (let i = 0; i < pcm.length / 2; i++) view.setInt16(i * 2, i % 2 ? level : -level, true)
  return pcm
}
/** Runs frames through `push` on a fake clock; returns the ms at which `done()` first held. */
function run(levels: number[], push: (f: Uint8Array) => void, done: () => boolean) {
  const realNow = Date.now
  let now = 1_000_000
  Date.now = () => now
  try {
    for (let i = 0; i < levels.length; i++) {
      now += FRAME_MS
      push(frame(levels[i]))
      if (done()) return i * FRAME_MS
    }
    return -1
  } finally { Date.now = realNow }
}
const repeat = (level: number, ms: number) => Array(ms / FRAME_MS).fill(level)

test('in a noisy room a command ends when the wearer stops, not at the cap', () => {
  const realNow = Date.now
  let now = 1_000_000
  Date.now = () => now
  let stopped: string | null = null
  const rec = new VoiceRecorder(r => { stopped ??= r })
  Date.now = realNow
  // 2 s of room chatter, 2 s of the wearer speaking (close mic: much louder), then chatter again.
  const levels = [...repeat(1500, 2000), ...repeat(7000, 2000), ...repeat(1500, 6000)]
  const at = run(levels, f => rec.push(f), () => stopped !== null)
  assert.equal(stopped, 'silence')
  assert.ok(at > 5300 && at < 5900, `stopped at ${at} ms`)
})

test('in a quiet room a short pause does not end the command', () => {
  let stopped: string | null = null
  const rec = new VoiceRecorder(r => { stopped ??= r })
  const levels = [...repeat(5000, 1000), ...repeat(100, 600), ...repeat(5000, 1000), ...repeat(100, 2000)]
  const at = run(levels, f => rec.push(f), () => stopped !== null)
  assert.equal(stopped, 'silence')
  assert.ok(at > 2600, `stopped at ${at} ms`)
})

test('nonstop chatter is rechecked for the wake phrase every couple of seconds', () => {
  const rechecks: number[] = []
  const listener = new WakeListener(() => {}, () => {}, (window, from) => rechecks.push(from))
  // Chatter just over the quiet-room threshold with no gaps: one long utterance.
  run(repeat(1200, 9000).map((l, i) => (i % 3 ? l : l * 3)), f => listener.push(f), () => false)
  assert.ok(rechecks.length >= 2, `rechecks: ${rechecks.length}`)
})
