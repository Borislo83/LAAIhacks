import type { DisplayAction, DisplayState, Gesture } from './types'

// Proposed session actions pop up over the reader as a short list. The popup only
// chooses what to review: a tap opens the action-cards flow (review, confirm,
// approve), so nothing is ever approved from here.

export const POPUP_HEADING = 'Focal can help'
export const POPUP_DISMISS = 'Dismiss'
/** Action rows besides Dismiss; the bottom strip holds four cells. */
const MAX_ITEMS = 3
/** A tap this soon after the popup appears was meant for the screen under it. */
export const POPUP_TAP_GRACE_MS = 600

/** G2 list rows hold 63 bytes; ~46 characters keeps every label whole on the lens. */
export function popupLabel(label: string) {
  let out = ''
  for (const ch of Array.from(label.replace(/[\r\n\t\u0000-\u001f]/g, ' ').replace(/\s+/g, ' ').trim()).slice(0, 46)) {
    if (new TextEncoder().encode(out + ch).length > 63) break
    out += ch
  }
  return out
}

export interface PopupView { kind: 'proposed' | 'result'; heading: string; items: { action_id: string; label: string }[] }
export type PopupStep = { kind: 'render' } | { kind: 'close' } | { kind: 'review'; action_id: string } | { kind: 'none' }

/** What is on the glasses now; the popup waits until only the page is. */
export interface Screen { view: string; loading: boolean; status: boolean; recording: boolean; card: boolean; page: boolean }
export const popupAllowed = (s: Screen) =>
  s.page && !s.loading && !s.status && !s.recording && !s.card && ['reader', 'deck', 'manga'].includes(s.view)

export class ActionPopup {
  enabled = true
  /** Queued or on screen; null when there's nothing to show. */
  view: PopupView | null = null
  cursor = 0
  private pageId = ''
  private popped = new Set<string>()
  private dismissed = false
  private waiting = new Set<string>()

  /** Takes each display poll; true when a new popup is ready (show it now or when the reader is back). */
  update(display: DisplayState | null): boolean {
    if (!display?.page) { this.reset(''); return false }
    if (display.page.page_id !== this.pageId) this.reset(display.page.page_id)
    const actions = display.actions
    let finished: DisplayAction | undefined
    for (const a of actions) {
      if (a.status === 'approved' || a.status === 'running') this.waiting.add(a.action_id)
      else if ((a.status === 'completed' || a.status === 'failed') && this.waiting.delete(a.action_id)) finished ??= a
    }
    // A queued list drops anything no longer proposed.
    if (this.view?.kind === 'proposed') {
      const items = this.view.items.filter(i => actions.some(a => a.action_id === i.action_id && a.status === 'proposed'))
      this.view = items.length ? { ...this.view, items } : null
      this.cursor = Math.min(this.cursor, this.view?.items.length ?? 0)
    }
    if (!this.enabled) return false
    if (finished) {
      // The result of an action approved on this page, while still on it.
      this.show({ kind: 'result', heading: finished.status === 'completed' ? 'Focal finished' : "Focal couldn't finish",
        items: [{ action_id: finished.action_id, label: popupLabel(`View: ${finished.label}`) }] })
      return true
    }
    const proposed = actions.filter(a => a.status === 'proposed')
    if (this.dismissed || display.mode === 'loading' || display.mode === 'confirm' ||
      !proposed.some(a => !this.popped.has(a.action_id))) return false
    for (const a of proposed) this.popped.add(a.action_id)
    this.show({ kind: 'proposed', heading: POPUP_HEADING,
      items: proposed.slice(0, MAX_ITEMS).map(a => ({ action_id: a.action_id, label: popupLabel(a.label) })) })
    return true
  }
  /** Labels for the strip, Dismiss last. */
  labels() { return this.view ? [...this.view.items.map(i => i.label), POPUP_DISMISS] : [] }
  gesture(g: Gesture): PopupStep {
    const view = this.view
    if (!view) return { kind: 'none' }
    if (g === 'back') { this.dismiss(); return { kind: 'close' } }
    if (g !== 'tap') {
      this.cursor = Math.max(0, Math.min(view.items.length, this.cursor + (g === 'down' ? 1 : -1)))
      return { kind: 'render' }
    }
    const item = view.items[this.cursor]
    if (!item) { this.dismiss(); return { kind: 'close' } }
    this.view = null
    return { kind: 'review', action_id: item.action_id }
  }
  /** Hides it; dismissing proposals keeps them off this page, still in the menu. */
  dismiss() {
    if (this.view?.kind === 'proposed') this.dismissed = true
    this.view = null
  }
  /** An approval sent from this page: its result pops up even if it finishes between polls. */
  expect(actionId: string) { this.waiting.add(actionId) }
  setEnabled(on: boolean) { this.enabled = on; if (!on) this.view = null }
  private show(view: PopupView) { this.view = view; this.cursor = 0 }
  private reset(pageId: string) {
    this.pageId = pageId; this.popped.clear(); this.waiting.clear(); this.dismissed = false; this.view = null; this.cursor = 0
  }
}
