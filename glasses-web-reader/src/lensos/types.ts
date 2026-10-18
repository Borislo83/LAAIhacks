export interface DisplayAction {
  action_id: string; label: string; description: string; status: string; requires_approval: boolean
  /** The watcher's handler: compare_prices, find_reviews, add_to_amazon_cart... */
  type?: string
  result?: { message?: string; result_url?: string } | null
  source?: 'task' | 'session'
}
export interface DisplayState {
  schema_version: '1.0'; session_id: string; revision: number
  page: null | { page_id: string; title: string; kind: string; summary: string[]; facts: { label: string; value: string }[] }
  actions: DisplayAction[]; mode: string; notice: string | null
}
export interface Preferences {
  display_density: 'comfortable' | 'compact'; shopping_priority: 'balanced' | 'price' | 'quality'
  proactive_enabled: boolean; notes: string
}
export interface Task {
  id: string; context_id: string; kind: 'price' | 'alternatives' | 'reviews'
  status: 'predicted' | 'running' | 'succeeded' | 'failed'; payload: Record<string, unknown>
  result: { summary: string; [key: string]: unknown } | null; error: string | null
  created_at: string
}
export interface PageSubmission {
  schema_version: '1.0'; request_id: string; page_id: string; url: string; kind: 'product' | 'article' | 'generic'
  title: string; summary: string[]; facts: { label: string; value: string }[]; source_label: string; updated_at: string
}
export interface PageContext { url: string; title: string; text: string; product?: string }
export interface DecisionIntent {
  request_id: string; action_id: string; expected_revision: number; operation: 'approve' | 'reject'
}
export type DecisionOutcome = 'success' | 'uncertain' | 'failed'
export type Gesture = 'tap' | 'up' | 'down' | 'back'
export interface CardView { title: string; lines: string[]; hint: string }
/** One agent the orchestrator's chat called (ChatReply.calls[]). */
export interface AgentCall {
  kind: string; payload: Record<string, unknown>; result: Record<string, unknown> | null; error: string | null; ms: number
}
export interface ChatReply {
  reply: string; calls: AgentCall[]; source: string; picks?: Record<string, unknown>[]
  product?: string | null; image?: string | null
}
export interface MessageSent {
  status: 'sent' | 'failed' | 'needs_permission' | 'open_link'; summary: string; dry_run?: boolean; open_url?: string
}
