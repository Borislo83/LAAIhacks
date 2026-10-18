// Adapted from LensOS apps/even-g2/src/action-cards.ts.
import { pxTruncate } from '@evenrealities/pretext'
import { actionTitle } from './action-popup'
import { CARD_W, wrap } from './agents'
import { lensText } from './glyphs'
import type { CardView, DecisionIntent, DecisionOutcome, DisplayAction, DisplayState, Gesture } from './types'

export type { DecisionIntent, DecisionOutcome } from './types'

/** Rows under a card's title (the reader body holds the title, a gap and 7 rows). */
export const CARD_ROWS = 7

const flat = (text: string) => lensText(text.replace(/[\r\n\t\u0000-\u001f]/g, ' '))
/** One lens row: never wider than the body, cut with "..." rather than mid-word wrapping. */
const row = (text: string) => pxTruncate(flat(text), CARD_W)
/** Glasses rows at real glyph widths, so words are never split across rows. */
const rows = (text: string) => wrap(lensText(text), CARD_W)

const STATUS: Record<string, string> = {
  proposed: 'needs approval', approved: 'approved', running: 'running', completed: 'done',
  failed: 'failed', rejected: 'rejected', expired: 'expired', dismissed: 'dismissed', saved: 'saved',
}
const statusText = (status: string) => STATUS[status] ?? status
const OUTCOME: Record<string, 'loading' | 'result' | 'error'> = { approved: 'loading', running: 'loading', completed: 'result', failed: 'error' }

const hostOf = (value: string) => {
  try {
    const url = new URL(value)
    return /^https?:$/.test(url.protocol) ? url.hostname.replace(/^www\./, '') : ''
  } catch { return '' }
}

/**
 * A worker's result message as lens text: "Source: <url>" lines become one compact
 * "Sources: apple.com, amazon.com" line and the "Retrieved: <timestamp>" line is dropped.
 */
export function resultText(message: string): string {
  const kept: string[] = []
  const sources: string[] = []
  for (const raw of message.split('\n')) {
    const line = raw.trim()
    if (/^retrieved:/i.test(line)) continue
    const source = /^sources?:\s*(.+)$/i.exec(line)
    if (source) {
      for (const part of source[1].split(/,\s*|\s+/)) {
        const name = hostOf(part) || (/^https?:/i.test(part) ? '' : part.trim())
        if (name && !sources.includes(name)) sources.push(name)
      }
      continue
    }
    if (line || kept[kept.length - 1]) kept.push(line)
  }
  while (kept.length && !kept[kept.length - 1]) kept.pop()
  return [...kept, ...(sources.length ? [`Sources: ${sources.join(', ')}`] : [])].join('\n')
}

/** Pages of CARD_ROWS rows; always at least one (possibly empty) page. */
const pagesOf = (lines: string[]) => {
  const pages: string[][] = []
  for (let i = 0; i < lines.length; i += CARD_ROWS) pages.push(lines.slice(i, i + CARD_ROWS))
  return pages.length ? pages : [[]]
}

/** API-only cards: no fixture proposals, no network side effects without confirmation. */
export function apiActionCards(send: (intent: DecisionIntent) => void, changed = () => {}) {
  let display: DisplayState | undefined
  let mode: 'summary' | 'actions' | 'detail' | 'confirm' | 'outcome' = 'summary'
  let selected = 0
  /** The page of whichever paged card is on screen (review, result, list). */
  let page = 0
  /** Opened on one action (from the "Focal can help" strip or a result popup): back returns to the page. */
  let focused = false
  let choice: 'approve' | 'reject' = 'approve'
  let enabled = false
  let pending: { intent: DecisionIntent; state: 'sending' | 'uncertain'; message: string } | undefined
  let notice = ''
  /** The last action approved on these cards. */
  let approvedHere = ''
  const action = () => display?.actions[selected]
  const review = () => {
    const a = action()
    if (!a) return []
    return [...rows(actionTitle(a)), ...rows(a.description),
      ...(a.result?.message ? rows(resultText(a.result.message)) : []), `Status: ${statusText(a.status)}`]
  }
  /**
   * What the summary reports: the action approved here (its own status says working, done
   * or failed, even while the session's mode still describes an earlier one), else the
   * session's latest outcome.
   */
  const outcome = (): { action?: DisplayAction; state: 'loading' | 'result' | 'error' } | undefined => {
    if (!display) return undefined
    const mine = display.actions.find(x => x.action_id === approvedHere)
    const own = mine && OUTCOME[mine.status]
    if (mine && own) return { action: mine, state: own }
    if (display.mode !== 'loading' && display.mode !== 'result' && display.mode !== 'error') return undefined
    const state = display.mode
    return { action: [...display.actions].reverse().find(x => OUTCOME[x.status] === state), state }
  }
  const resultPages = () => {
    const o = outcome()
    if (!o || o.state === 'loading' || notice) return [[]]
    return pagesOf(rows(resultText(o.action?.result?.message ?? display?.notice ?? 'No details were returned.')))
  }
  const lastPage = () => (mode === 'detail' ? pagesOf(review()).length
    : mode === 'actions' ? Math.ceil((display?.actions.length ?? 0) / CARD_ROWS)
    : mode === 'summary' ? resultPages().length : 1) - 1
  const make = (title: string, lines: string[], hint: string): CardView => ({
    title: row(title), lines: lines.slice(0, CARD_ROWS).map(row), hint: row(hint),
  })
  /** "2/3  " before a hint when the card has more than one page. */
  const pager = (count: number) => (count > 1 ? `${page + 1}/${count}  ` : '')
  return {
    open() {
      if (!pending) { mode = display?.actions.length ? 'actions' : 'summary'; selected = 0; page = 0; focused = false }
      changed()
    },
    /** Opens one action from the strip (its review; approving still needs confirm and a tap) or a result popup (its result). */
    focus(actionId: string) {
      const index = display?.actions.findIndex(a => a.action_id === actionId) ?? -1
      if (!pending && index >= 0) {
        selected = index; page = 0; focused = true
        // A finished action (a result popup's "View") opens the same Done/Failed card as waiting would.
        const finished = ['completed', 'failed'].includes(display!.actions[index].status)
        if (finished) approvedHere = actionId
        mode = finished ? 'summary' : 'detail'
      }
      changed()
    },
    show(next: DisplayState) {
      if (!display || next.revision !== display.revision) {
        // A new revision never carries a confirmation over: the action is reviewed again.
        const id = action()?.action_id
        const index = id ? next.actions.findIndex(a => a.action_id === id) : -1
        notice = ''
        if (mode === 'outcome') mode = 'summary'
        if (index >= 0) selected = index
        else if (mode === 'detail' || mode === 'confirm') { mode = 'summary'; selected = 0; page = 0 }
        else selected = 0
        if (mode === 'confirm') { mode = 'detail'; page = 0 }
        if (mode === 'actions' && !next.actions.length) mode = 'summary'
      }
      display = next
      page = Math.max(0, Math.min(page, lastPage()))
      changed()
    },
    setEnabled(value: boolean) {
      const cancel = enabled && !value && mode === 'confirm'
      enabled = value
      if (cancel) { mode = 'summary'; page = 0; changed() }
    },
    finish(requestId: string, outcome: DecisionOutcome, message: string) {
      if (!pending || pending.intent.request_id !== requestId) return
      if (outcome === 'uncertain') { pending.state = 'uncertain'; pending.message = message }
      else {
        const alreadyUpdated = display && display.revision > pending.intent.expected_revision
        pending = undefined
        mode = outcome === 'failed' ? 'outcome' : 'summary'
        page = 0
        notice = outcome === 'failed' || !alreadyUpdated ? message : ''
      }
      changed()
    },
    dispatch(gesture: Gesture): 'exit' | undefined {
      // Local navigation remains available offline and while an acknowledgment is unknown.
      if (gesture === 'back') {
        if (pending || mode === 'summary' || mode === 'outcome' || (mode === 'detail' && focused)) return 'exit'
        mode = mode === 'confirm' ? 'detail' : mode === 'detail' ? 'actions' : 'summary'
        page = mode === 'actions' ? Math.floor(selected / CARD_ROWS) : 0
        changed()
        return
      }
      if (!display || !enabled) return
      if (pending) {
        if (pending.state === 'uncertain' && gesture === 'tap') {
          pending.state = 'sending'
          send({ ...pending.intent })
          changed()
        }
        return
      }
      const turn = (delta: number) => { page = Math.max(0, Math.min(lastPage(), page + delta)) }
      if (mode === 'summary' || mode === 'outcome') {
        if (gesture === 'tap') { if (display.actions.length) { mode = 'actions'; page = Math.floor(selected / CARD_ROWS); notice = '' } }
        else turn(gesture === 'down' ? 1 : -1)
      } else if (mode === 'actions') {
        if (gesture === 'tap') { mode = 'detail'; page = 0; focused = false }
        else {
          selected = Math.max(0, Math.min(display.actions.length - 1, selected + (gesture === 'down' ? 1 : -1)))
          page = Math.floor(selected / CARD_ROWS)
        }
      } else if (mode === 'detail') {
        if (gesture === 'tap' && page === lastPage() && action()?.status === 'proposed') {
          mode = 'confirm'; choice = 'approve'
        } else turn(gesture === 'up' ? -1 : 1)
      } else if (mode === 'confirm') {
        if (gesture !== 'tap') choice = gesture === 'down' ? 'reject' : 'approve'
        else if (action()?.status === 'proposed') {
          const intent: DecisionIntent = { request_id: crypto.randomUUID(), action_id: action()!.action_id,
            expected_revision: display.revision, operation: choice }
          pending = { intent, state: 'sending', message: '' }
          if (choice === 'approve') approvedHere = intent.action_id
          send({ ...intent })
        }
      }
      changed()
    },
    view(): CardView {
      if (pending) {
        const a = display?.actions.find(x => x.action_id === pending!.intent.action_id)
        return make(pending.state === 'sending' ? 'Sending decision' : 'Decision uncertain',
          [`${pending.intent.operation === 'approve' ? 'Approve' : 'Reject'}: ${a ? actionTitle(a) : 'this action'}`,
            ...(pending.state === 'sending' ? ['Waiting for the session to answer'] : ['No new decision will be created', ...rows(pending.message)])],
          pending.state === 'uncertain' ? 'Tap: retry same request | Double: close' : 'Please wait | Double: close')
      }
      if (!display) return make('Focal', ['Connecting to your session'], 'Double: close')
      const a = action()
      if (mode === 'actions') {
        const count = Math.max(1, Math.ceil(display.actions.length / CARD_ROWS))
        const start = page * CARD_ROWS
        return make(count > 1 ? `Actions ${page + 1}/${count}` : 'Actions', display.actions.slice(start, start + CARD_ROWS).map((x, i) =>
          `${start + i === selected ? '>' : ' '} ${actionTitle(x)} (${statusText(x.status)})`), 'Swipe: select | Tap: details | Double: back')
      }
      if (mode === 'detail' && a) {
        const pages = pagesOf(review())
        const last = page >= pages.length - 1
        const back = focused ? 'Double: close' : 'Double: back'
        return make(pages.length === 1 ? 'Review action' : `Review ${page + 1}/${pages.length}`, pages[page] ?? [],
          !last ? `Swipe/tap: next page | ${back}`
            : a.status === 'proposed' ? `Tap: approve or reject | ${back}` : `${pages.length > 1 ? 'Swipe up: back a page | ' : ''}${back}`)
      }
      if (mode === 'confirm' && a) return make('Confirm action', [actionTitle(a),
        `${choice === 'approve' ? '>' : ' '} ${a.source === 'task' ? 'Run recommendation' : 'Approve'}`,
        `${choice === 'reject' ? '>' : ' '} ${a.source === 'task' ? 'Dismiss for this page' : 'Reject'}`], 'Swipe: choose | Tap: confirm | Double: cancel')
      const more = display.actions.length ? 'Tap: all actions | ' : ''
      if (notice) return make('Decision status', rows(notice), `${more}Double: close`)
      const o = outcome()
      if (o) {
        // The server sends the outcome on the action, not in notice (which is null today).
        const name = o.action ? actionTitle(o.action) : 'your action'
        if (o.state === 'loading') {
          return make(`Working: ${name}`, ['Focal is working on it.', 'You can close this card; the result', 'pops up over the page when it is ready.'],
            `${more}Double: close`)
        }
        const pages = resultPages()
        return make(`${o.state === 'result' ? 'Done' : 'Failed'}: ${name}`, pages[page] ?? [],
          `${pager(pages.length)}${pages.length > 1 ? 'Swipe: page | ' : ''}${more}Double: close`)
      }
      return make('Focal', display.page ?
        [display.page.title, ...(display.notice ? [display.notice] : []), ...display.page.summary,
          ...display.page.facts.map(f => `${f.label}: ${f.value}`)] : ['Waiting for page'],
        display.actions.length ? 'Tap: all actions | Double: close' : 'No proposed actions | Double: close')
    },
  }
}
