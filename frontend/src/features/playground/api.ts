import { request } from '@/api/client'
import type { ApiKey, ApiKeyCreated, Status } from '@/api/types'

/** One model as the signed-in person sees it right now (computed once, in /api/status; backend api/status.py).
 * now: callable now (`loading`: still starting where you may use it) · booking: a booking loads it ·
 * busy: running on a GPU someone else has booked (`holder` until `until`). */
export interface ModelAvailability {
  id: number
  name: string
  is_default: boolean
  state: 'now' | 'booking' | 'busy'
  loading: boolean
  lane_id: number | null
  lane_name: string | null
  holder: string | null
  until: string | null
  args_pending: boolean
}

export type StatusWithModels = Status & { models?: ModelAvailability[] }

export function availabilityOf(status: Status | null | undefined): ModelAvailability[] {
  return (status as StatusWithModels | null | undefined)?.models ?? []
}

export interface PlaygroundState {
  enabled: boolean
  rate_per_minute: number
  default_model_only: boolean
  default_model: string | null
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export interface ChatRequest {
  model?: string
  messages: ChatMessage[]
  temperature?: number
  max_tokens?: number
}

/** The OpenAI-style answer of the model (only what the page reads). */
export interface ChatResponse {
  model?: string
  choices: { message: { role: string; content: string }; finish_reason?: string }[]
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number }
}

/** A key with scope and expiry (the fields W1-05 added to the key list). */
export type KeyInfo = ApiKey & { kind?: string; scope?: string; expires_at?: string | null; expired?: boolean }

export const PLAYGROUND_KEY_NAME = 'playground'
export const PLAYGROUND_KEY_DAYS = 90

export const playgroundApi = {
  state: () => request<PlaygroundState>('GET', '/api/playground'),
  chat: (body: ChatRequest) => request<ChatResponse>('POST', '/api/playground/chat', { ...body, stream: false }),
  keys: () => request<KeyInfo[]>('GET', '/api/keys'),
  createKey: (name: string, expiresInDays: number | null) =>
    request<ApiKeyCreated & KeyInfo>('POST', '/api/keys', {
      name,
      scope: 'llm',
      ...(expiresInDays ? { expires_in_days: expiresInDays } : {}),
    }),
}
