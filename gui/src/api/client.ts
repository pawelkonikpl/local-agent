export interface CurrentUser {
  id: string
  email: string
  role: string
}

export interface ChatSession {
  id: string
  status: string
  approval_mode: string
  created_at: string
  updated_at: string
}

export interface ContentBlock {
  type: string
  text?: string
}

export interface ChatMessageOut {
  id: string
  session_id: string
  sequence_number: number
  role: 'user' | 'assistant' | 'system'
  content: ContentBlock[]
  created_at: string
}

export interface SessionUsage {
  input_tokens: number
  output_tokens: number
  total_tokens: number
}

class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })

  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new ApiError(response.status, body?.detail ?? response.statusText)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return response.json() as Promise<T>
}

export function login(email: string, password: string): Promise<CurrentUser> {
  return request('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) })
}

export function logout(): Promise<void> {
  return request('/auth/logout', { method: 'POST' })
}

export function me(): Promise<CurrentUser> {
  return request('/auth/me')
}

export function listSessions(): Promise<ChatSession[]> {
  return request('/sessions')
}

export function createSession(): Promise<ChatSession> {
  return request('/sessions', { method: 'POST' })
}

export function listMessages(sessionId: string, after?: number): Promise<ChatMessageOut[]> {
  const query = after !== undefined ? `?after=${after}` : ''
  return request(`/sessions/${sessionId}/messages${query}`)
}

export function cancelGeneration(sessionId: string): Promise<void> {
  return request(`/sessions/${sessionId}/cancel`, { method: 'POST' })
}

export function getSessionUsage(sessionId: string): Promise<SessionUsage> {
  return request(`/sessions/${sessionId}/usage`)
}

/**
 * Reads a `text/event-stream` response incrementally, dispatching each `delta` frame and
 * throwing on `error` frames. Shared by `sendMessage` (POST, starts a generation) and
 * `attachToStream` (GET, attaches to one already running) since both speak the same wire format.
 */
async function consumeEventStream(response: Response, onDelta: (text: string) => void): Promise<void> {
  if (!response.body) return
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      let eventName = 'message'
      let dataLine = ''
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) eventName = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLine = line.slice(5).trim()
      }
      if (!dataLine) continue
      const data = JSON.parse(dataLine)
      if (eventName === 'delta' && typeof data.text === 'string') {
        onDelta(data.text)
      } else if (eventName === 'error') {
        throw new ApiError(0, data.message ?? 'Streaming error')
      }
    }
  }
}

/**
 * Posts a chat message and consumes the `text/event-stream` reply as it arrives.
 * Not built on `request()`: that helper always parses one JSON body, but this response is a
 * live stream of `event:`/`data:` frames read incrementally via the fetch body reader.
 */
export async function sendMessage(
  sessionId: string,
  content: string,
  onDelta: (text: string) => void,
): Promise<void> {
  const response = await fetch(`/sessions/${sessionId}/messages`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })

  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null)
    throw new ApiError(response.status, body?.detail ?? response.statusText)
  }

  await consumeEventStream(response, onDelta)
}

/**
 * Attaches to a session's in-progress generation, if any: `204` means nothing is running, so the
 * caller has nothing more to do beyond the persisted history it already has. Otherwise consumes
 * the stream exactly like `sendMessage` -- the server replays everything accumulated so far as one
 * `delta` before continuing live.
 *
 * Returns `true` if there was a generation to attach to (the stream has now fully ended), `false`
 * on `204`. The server uses the exact message "Generation cancelled" for a `POST /cancel`-driven
 * termination (see `run_generation` in `api/chat/streaming.py`), so callers can tell that apart
 * from a genuine upstream error.
 */
export async function attachToStream(
  sessionId: string,
  onDelta: (text: string) => void,
): Promise<boolean> {
  const response = await fetch(`/sessions/${sessionId}/stream`, { credentials: 'include' })
  if (response.status === 204) return false
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null)
    throw new ApiError(response.status, body?.detail ?? response.statusText)
  }
  await consumeEventStream(response, onDelta)
  return true
}

export { ApiError }
