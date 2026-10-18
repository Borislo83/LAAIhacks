// Adapted from LensOS apps/even-g2/src/action-cards.ts.
import type { CardView, Gesture, DisplayState } from './types';

export interface DecisionIntent {
  request_id: string;
  action_id: string;
  expected_revision: number;
  operation: 'approve' | 'reject';
}
export type DecisionOutcome = 'success' | 'uncertain' | 'failed';

const clip = (line: string) => Array.from(line.replace(/[\r\n\t\u0000-\u001f]/g, ' ')).slice(0, 46).join('');

/** API-only cards: no fixture proposals, no network side effects without confirmation. */
export function apiActionCards(send: (intent: DecisionIntent) => void, changed = () => {}) {
  let display: DisplayState | undefined;
  let mode: 'summary' | 'actions' | 'detail' | 'confirm' | 'outcome' = 'summary';
  let selected = 0;
  let reviewPage = 0;
  let choice: 'approve' | 'reject' = 'approve';
  let enabled = false;
  let pending: { intent: DecisionIntent; state: 'sending' | 'uncertain'; message: string } | undefined;
  let notice = '';
  const action = () => display?.actions[selected];
  const wrap = (text: string) => {
    const characters = Array.from(text.replace(/[\r\n\t\u0000-\u001f]/g, ' '));
    const lines: string[] = [];
    for (let i = 0; i < characters.length; i += 46) lines.push(characters.slice(i, i + 46).join(''));
    return lines;
  };
  const review = () => {
    const a = action();
    return a ? [...wrap(a.label), ...wrap(a.description), ...(a.result?.message ? wrap(a.result.message) : []), `Status: ${a.status}`] : [];
  };
  const make = (title: string, lines: string[], hint: string): CardView => ({
    title: clip(title), lines: lines.slice(0, 7).map(clip), hint: clip(hint),
  });
  return {
    open() {
      if (!pending) { mode = display?.actions.length ? 'actions' : 'summary'; selected = 0; reviewPage = 0; }
      changed();
    },
    show(next: DisplayState) {
      if (!display || next.revision !== display.revision) {
        mode = 'summary'; selected = 0; reviewPage = 0; notice = '';
      }
      display = next;
      changed();
    },
    setEnabled(value: boolean) {
      const cancel = enabled && !value && mode === 'confirm';
      enabled = value;
      if (cancel) { mode = 'summary'; changed(); }
    },
    finish(requestId: string, outcome: DecisionOutcome, message: string) {
      if (!pending || pending.intent.request_id !== requestId) return;
      if (outcome === 'uncertain') { pending.state = 'uncertain'; pending.message = message; }
      else {
        const alreadyUpdated = display && display.revision > pending.intent.expected_revision;
        pending = undefined;
        mode = outcome === 'failed' ? 'outcome' : 'summary';
        notice = outcome === 'failed' || !alreadyUpdated ? message : '';
      }
      changed();
    },
    dispatch(gesture: Gesture): 'exit' | undefined {
      // Local navigation remains available offline and while an acknowledgment is unknown.
      if (gesture === 'back') {
        if (pending || mode === 'summary' || mode === 'outcome') return 'exit';
        mode = mode === 'confirm' ? 'detail' : mode === 'detail' ? 'actions' : 'summary';
        changed();
        return;
      }
      if (!display || !enabled) return;
      if (pending) {
        if (pending.state === 'uncertain' && gesture === 'tap') {
          pending.state = 'sending';
          send({ ...pending.intent });
          changed();
        }
        return;
      }
      if (mode === 'summary' || mode === 'outcome') {
        if (gesture === 'tap' && display.actions.length) mode = 'actions';
      } else if (mode === 'actions') {
        if (gesture === 'tap') { mode = 'detail'; reviewPage = 0; }
        else selected = Math.max(0, Math.min(display.actions.length - 1, selected + (gesture === 'down' ? 1 : -1)));
      } else if (mode === 'detail') {
        const lastPage = Math.max(0, Math.ceil(review().length / 6) - 1);
        if (gesture === 'tap' && reviewPage === lastPage && action()?.status === 'proposed') {
          mode = 'confirm'; choice = 'approve';
        } else reviewPage = Math.max(0, Math.min(lastPage, reviewPage + (gesture === 'up' ? -1 : 1)));
      } else if (mode === 'confirm') {
        if (gesture !== 'tap') choice = gesture === 'down' ? 'reject' : 'approve';
        else if (action()?.status === 'proposed') {
          const intent: DecisionIntent = { request_id: crypto.randomUUID(), action_id: action()!.action_id,
            expected_revision: display.revision, operation: choice };
          pending = { intent, state: 'sending', message: '' };
          send({ ...intent });
        }
      }
      changed();
    },
    view(): CardView {
      if (pending) return make(pending.state === 'sending' ? 'Sending decision' : 'Decision uncertain',
        [pending.intent.operation === 'approve' ? 'Approve' : 'Reject', pending.intent.action_id,
          pending.state === 'sending' ? 'Waiting for API acknowledgment' : 'No new decision will be created',
          pending.message], pending.state === 'uncertain' ? 'Tap: retry same request' : 'Please wait');
      if (!display) return make('LensOS | API', ['Waiting for API display'], 'Connecting');
      const a = action();
      if (mode === 'actions') {
        const start = Math.floor(selected / 6) * 6;
        return make('Actions | API', display.actions.slice(start, start + 6).map((a, i) =>
          `${start + i === selected ? '>' : ' '} ${a.label} (${a.status})`), 'Swipe: select | Tap: detail | Double: back');
      }
      if (mode === 'detail' && a) {
        const lines = review();
        const pages = Math.ceil(lines.length / 6);
        return make(pages === 1 ? 'Review action | API' : `Review ${reviewPage + 1}/${pages} | API`,
          lines.slice(reviewPage * 6, reviewPage * 6 + 6), reviewPage < pages - 1
            ? 'Tap/down: next page | Double: back' : a.status === 'proposed'
              ? 'Tap: confirmation | Up: review | Double: back' : 'Up: review | Double: back');
      }
      if (mode === 'confirm' && a) return make('Confirm action', [clip(a.label),
        `${choice === 'approve' ? '>' : ' '} ${a.source === 'task' ? 'Run recommendation' : 'Approve'}`,
        `${choice === 'reject' ? '>' : ' '} ${a.source === 'task' ? 'Dismiss for this page' : 'Reject'}`], 'Swipe: choose | Tap: confirm | Double: cancel');
      if (notice) return make('Decision status | API', [notice], 'Tap: actions | Double: summary');
      if (['loading', 'result', 'error'].includes(display.mode)) return make(
        { loading: 'Pending | API', result: 'Completed | API', error: 'Failed | API' }[display.mode]!,
        [display.notice ?? 'Waiting for worker'], 'Tap: actions | Double: summary');
      return make(`LensOS | API r${display.revision}`, display.page ?
        [display.page.title, ...(display.notice ? [display.notice] : []), ...display.page.summary,
          ...display.page.facts.map(f => `${f.label}: ${f.value}`)] : ['Waiting for page'],
        display.actions.length ? 'Tap: actions | No action runs without approval' : 'Live | No proposed actions');
    },
  };
}
