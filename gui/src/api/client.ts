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

export function listMessages(sessionId: string): Promise<ChatMessageOut[]> {
  return request(`/sessions/${sessionId}/messages`)
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

export { ApiError }
