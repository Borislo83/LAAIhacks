/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the condense server. Empty = same origin (Vite dev proxy). */
  readonly VITE_API_BASE?: string
}
