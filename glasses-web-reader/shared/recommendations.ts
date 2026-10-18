export interface RecommendationContext { url: string; title: string; text: string }
export interface RecommendedAction { id: string; title: string; description: string; instruction: string }
export interface RecommendationBatch {
  source: 'claude'
  page: { url: string; title: string }
  actions: RecommendedAction[]
}

/** Selection only. The Fetch.ai integration owns confirmation, dispatch and results. */
export interface RecommendationSelection {
  schema_version: '1.0'
  request_id: string
  executor: 'fetch.ai'
  source: 'claude'
  action: RecommendedAction
  context: RecommendationContext
}
export const RECOMMENDATION_SELECTED = 'lensos:recommendation-selected'

/** Fits one cell of the glasses' Bottom strip layout (about 244px) without truncation. */
export const TITLE_MAX = 24
export const DESCRIPTION_MAX = 100
export const INSTRUCTION_MAX = 600

const text = (value: unknown, max: number): value is string =>
  typeof value === 'string' && value.trim().length > 0 && Array.from(value.trim()).length <= max

/** Titles in a model reply that are too long to show on the glasses. */
export const longTitles = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.flatMap(a => (typeof a?.title === 'string' && Array.from(a.title.trim()).length > TITLE_MAX ? [a.title] : []))
    : []

export function validContext(value: unknown): value is RecommendationContext {
  if (!value || typeof value !== 'object') return false
  const c = value as RecommendationContext
  if (!text(c.url, 2048) || !text(c.title, 500) || !text(c.text, 16000)) return false
  try {
    const url = new URL(c.url)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
  } catch { return false }
}

export function validActions(value: unknown): value is Omit<RecommendedAction, 'id'>[] {
  return Array.isArray(value) && value.length === 3 && value.every(action => action &&
    text(action.title, TITLE_MAX) && text(action.description, DESCRIPTION_MAX) && text(action.instruction, INSTRUCTION_MAX)) &&
    new Set(value.map(action => action.title.trim().toLowerCase())).size === 3
}

export function validBatch(value: unknown): value is RecommendationBatch {
  if (!value || typeof value !== 'object') return false
  const batch = value as RecommendationBatch
  return batch.source === 'claude' && !!batch.page && text(batch.page.url, 2048) && text(batch.page.title, 500) &&
    validActions(batch.actions) && batch.actions.every(action => text(action.id, 100)) &&
    new Set(batch.actions.map(action => action.id)).size === 3
}
