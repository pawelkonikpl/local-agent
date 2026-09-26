import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from 'react'
import { useOutletContext, useParams } from 'react-router-dom'
import type {
  ChatMessageOut,
  ContentBlock,
  SessionUsage,
  StreamHandlers,
  ToolCallEvent,
  ToolResultEvent,
} from '../../api/client'
import {
  ApiError,
  attachToStream,
  cancelGeneration,
  getSessionUsage,
  listMessages,
  sendMessage,
} from '../../api/client'
import type { ChatOutletContext } from './ChatLayout'
import Markdown from './Markdown'
import { useChatModel } from './useChatModel'

interface ToolCallView {
  id: string
  name: string
  input: Record<string, unknown>
  result?: string
  isError?: boolean
}

// A bubble's content in order of occurrence: text interleaved with the tool calls between it.
type DisplayPart = { kind: 'text'; text: string } | { kind: 'tool'; call: ToolCallView }

interface DisplayMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  parts: DisplayPart[]
}

const ROLE_LABEL: Record<DisplayMessage['role'], string> = {
  user: 'You',
  assistant: 'Claude',
  system: 'System',
}

// Exact wording `run_generation` (api/chat/streaming.py) uses for a cancel-driven termination, so
// it can be shown as a plain stop rather than an error banner.
const CANCELLED_MESSAGE = 'Generation cancelled'

// How often an idle tab checks whether a generation has started on this session (e.g. from
// another tab) -- there's no push channel yet, so this is a deliberately simple poll.
const WATCH_POLL_MS = 2000

function toParts(content: ContentBlock[]): DisplayPart[] {
  const parts: DisplayPart[] = []
  for (const block of content) {
    if (block.type === 'text' && block.text) {
      parts.push({ kind: 'text', text: block.text })
    } else if (block.type === 'tool_use' && block.id) {
      parts.push({
        kind: 'tool',
        call: { id: block.id, name: block.name ?? '', input: block.input ?? {} },
      })
    }
  }
  return parts
}

function isToolResultsOnly(message: ChatMessageOut): boolean {
  return (
    message.role === 'user' &&
    message.content.length > 0 &&
    message.content.every((block) => block.type === 'tool_result')
  )
}

/** Resolves the first still-pending call with `result.id` -- returns new parts, never mutates. */
function withToolResult(parts: DisplayPart[], result: ToolResultEvent): DisplayPart[] {
  let resolved = false
  return parts.map((part) => {
    if (
      resolved ||
      part.kind !== 'tool' ||
      part.call.id !== result.id ||
      part.call.result !== undefined
    ) {
      return part
    }
    resolved = true
    return {
      kind: 'tool',
      call: { ...part.call, result: result.content, isError: result.is_error },
    }
  })
}

/**
 * Maps persisted history to bubbles: one bubble per user turn, as it looks while streaming. A user
 * message made only of `tool_result` blocks isn't a bubble -- its results are attached to the
 * preceding assistant bubble's calls, and the assistant message after it continues that bubble.
 */
function toDisplayMessages(history: ChatMessageOut[]): DisplayMessage[] {
  const display: DisplayMessage[] = []
  let continuesAssistant = false
  for (const message of history) {
    const last = display[display.length - 1]
    if (isToolResultsOnly(message)) {
      if (last?.role === 'assistant') {
        for (const block of message.content) {
          last.parts = withToolResult(last.parts, {
            id: block.tool_use_id ?? '',
            content: block.content ?? '',
            is_error: block.is_error ?? false,
          })
        }
        continuesAssistant = true
      }
      continue
    }
    if (message.role === 'assistant' && continuesAssistant && last?.role === 'assistant') {
      last.parts = [...last.parts, ...toParts(message.content)]
    } else {
      display.push({ id: message.id, role: message.role, parts: toParts(message.content) })
    }
    continuesAssistant = false
  }
  return display
}

function messageText(message: DisplayMessage): string {
  return message.parts.map((part) => (part.kind === 'text' ? part.text : '')).join('')
}

function appendText(parts: DisplayPart[], text: string): DisplayPart[] {
  const last = parts[parts.length - 1]
  if (last?.kind !== 'text') return [...parts, { kind: 'text', text }]
  return [...parts.slice(0, -1), { kind: 'text', text: last.text + text }]
}

type ToolCallState = 'running' | 'ok' | 'error' | 'interrupted'

const TOOL_STATE_LABEL: Record<ToolCallState, string> = {
  running: 'running…',
  ok: 'ok',
  error: 'error',
  interrupted: 'no result',
}

function toolCallState(call: ToolCallView, isStreaming: boolean): ToolCallState {
  if (call.result === undefined) return isStreaming ? 'running' : 'interrupted'
  return call.isError ? 'error' : 'ok'
}

function ToolCall({ call, isStreaming }: { call: ToolCallView; isStreaming: boolean }) {
  const state = toolCallState(call, isStreaming)
  return (
    <details className={`tool-call tool-call-${state}`}>
      <summary>
        <span className="tool-call-name">{call.name}</span>
        <span className="tool-call-state">{TOOL_STATE_LABEL[state]}</span>
      </summary>
      <div className="tool-call-body">
        <div className="tool-call-label">Input</div>
        <pre>{JSON.stringify(call.input, null, 2)}</pre>
        {call.result !== undefined && (
          <>
            <div className="tool-call-label">{call.isError ? 'Error' : 'Result'}</div>
            <pre>{call.result}</pre>
          </>
        )}
      </div>
    </details>
  )
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function isCancelledError(err: unknown): boolean {
  return err instanceof ApiError && err.message === CANCELLED_MESSAGE
}

export default function ChatView() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const { sessions, titles, registerFirstMessage } = useOutletContext<ChatOutletContext>()
  const session = sessions.find((s) => s.id === sessionId)
  const title = sessionId ? titles[sessionId] : undefined

  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [draft, setDraft] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [usage, setUsage] = useState<SessionUsage | null>(null)
  const { models, model, selectModel } = useChatModel()
  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  // Which displayed message is the one currently receiving deltas -- set the moment a bubble is
  // created (own send or an attached-to generation), cleared once that generation ends.
  const streamingIdRef = useRef<string | null>(null)
  // Guards against the background watcher's `GET /stream` poll and this tab's own `POST /messages`
  // overlapping: both would become independent subscribers of the same generation and double up
  // every delta. Only one of them may have a request in flight at a time.
  const requestInFlightRef = useRef(false)
  // The session the view is currently showing. `streamingIdRef`/`requestInFlightRef`/`messages`
  // are plain component state, not scoped to a session -- React Router re-renders this same
  // component instance with a new `sessionId` when switching chats rather than remounting it. Any
  // async callback still in flight for a session the user has since navigated away from (a
  // `sendMessage`/`attachToStream` delta, or their `finally` cleanup) must check this before
  // touching shared state, or it corrupts whatever session is now on screen.
  const activeSessionIdRef = useRef<string | undefined>(undefined)

  function beginAssistantBubble() {
    const id = `local-assistant-${Date.now()}`
    streamingIdRef.current = id
    setStreaming(true)
    setMessages((prev) => [...prev, { id, role: 'assistant', parts: [] }])
  }

  function updateStreamingParts(update: (parts: DisplayPart[]) => DisplayPart[]) {
    if (!streamingIdRef.current) beginAssistantBubble()
    const id = streamingIdRef.current
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, parts: update(m.parts) } : m)))
  }

  function appendDelta(delta: string) {
    updateStreamingParts((parts) => appendText(parts, delta))
  }

  function appendToolCall(call: ToolCallEvent) {
    updateStreamingParts((parts) => [...parts, { kind: 'tool', call }])
  }

  function applyToolResult(result: ToolResultEvent) {
    updateStreamingParts((parts) => withToolResult(parts, result))
  }

  // Stream handlers that only touch the view while `forSessionId` is still the one on screen.
  function handlersFor(forSessionId: string): StreamHandlers {
    const isActive = () => activeSessionIdRef.current === forSessionId
    return {
      onDelta: (text) => {
        if (isActive()) appendDelta(text)
      },
      onToolCall: (call) => {
        if (isActive()) appendToolCall(call)
      },
      onToolResult: (result) => {
        if (isActive()) applyToolResult(result)
      },
    }
  }

  // Best-effort: token usage is a secondary display, not core chat functionality, so a failed
  // fetch (e.g. no usage recorded yet for a brand-new session) just leaves the counter as-is
  // rather than surfacing an error banner.
  async function refreshUsage(forSessionId: string) {
    try {
      const result = await getSessionUsage(forSessionId)
      if (activeSessionIdRef.current === forSessionId) setUsage(result)
    } catch {
      // ignore
    }
  }

  useEffect(() => {
    if (!sessionId) return
    let cancelled = false

    // A previous session's in-flight `handleSubmit`/watcher may still be running (its request
    // keeps going in the background even though we're navigating away from it) -- switching
    // `activeSessionIdRef` now means its `finally`/delta callbacks will see themselves as stale
    // and skip touching state, instead of corrupting the session we're about to show.
    activeSessionIdRef.current = sessionId
    requestInFlightRef.current = false
    streamingIdRef.current = null
    setStreaming(false)
    setMessages([])
    setError(null)
    setUsage(null)

    async function loadHistory() {
      const history = await listMessages(sessionId!)
      if (cancelled) return
      const display = toDisplayMessages(history)
      setMessages(display)
      const firstUser = display.find((m) => m.role === 'user' && messageText(m).trim())
      if (firstUser) registerFirstMessage(sessionId!, messageText(firstUser))
    }

    async function watchForGeneration() {
      while (!cancelled) {
        if (requestInFlightRef.current) {
          await sleep(250)
          continue
        }
        requestInFlightRef.current = true
        let attached = false
        try {
          attached = await attachToStream(sessionId!, handlersFor(sessionId!))
        } catch (err) {
          if (!cancelled && !isCancelledError(err)) {
            setError(err instanceof Error ? err.message : 'Streaming failed.')
          }
          // Only a mid-stream protocol error (thrown by `consumeEventStream` with status 0) means
          // we were genuinely attached to a generation that just ended -- treat anything else
          // (connection/routing failure, e.g. a proxy misconfiguration) like a plain 204 so it
          // backs off via `sleep(WATCH_POLL_MS)` below instead of retrying in a tight loop.
          attached = err instanceof ApiError && err.status === 0
        } finally {
          if (!cancelled) {
            requestInFlightRef.current = false
            streamingIdRef.current = null
            setStreaming(false)
          }
        }
        if (cancelled) return
        if (attached) {
          // Replaces the local placeholder bubble with the persisted message (real id/seq).
          await loadHistory()
          await refreshUsage(sessionId!)
        } else {
          await sleep(WATCH_POLL_MS)
        }
      }
    }

    loadHistory().then(() => {
      refreshUsage(sessionId!)
      if (!cancelled) watchForGeneration()
    })

    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, registerFirstMessage])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  function autoResize() {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      event.currentTarget.form?.requestSubmit()
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    if (!sessionId || !draft.trim() || streaming || requestInFlightRef.current) return

    const mySessionId = sessionId
    const userText = draft
    setDraft('')
    requestAnimationFrame(autoResize)
    setError(null)
    registerFirstMessage(mySessionId, userText)
    setMessages((prev) => [
      ...prev,
      { id: `local-user-${Date.now()}`, role: 'user', parts: [{ kind: 'text', text: userText }] },
    ])
    beginAssistantBubble()

    requestInFlightRef.current = true
    try {
      // Guarded: if the user has since switched to another session (React Router re-renders this
      // same component with a new `sessionId` rather than remounting it), this send keeps running
      // in the background but must stop touching `messages`/`streaming` -- those now belong to
      // whatever session is on screen.
      await sendMessage(mySessionId, userText, model, handlersFor(mySessionId))
    } catch (err) {
      if (activeSessionIdRef.current === mySessionId && !isCancelledError(err)) {
        setError(err instanceof Error ? err.message : 'Streaming failed.')
      }
    } finally {
      if (activeSessionIdRef.current === mySessionId) {
        requestInFlightRef.current = false
        streamingIdRef.current = null
        setStreaming(false)
      }
    }
    await refreshUsage(mySessionId)
  }

  async function handleStop() {
    if (!sessionId) return
    try {
      await cancelGeneration(sessionId)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to stop generation.')
    }
  }

  return (
    <main className="chat-main">
      <header className="chat-topbar">
        <div className="topbar-titles">
          <div className="topbar-title">
            {title ?? (session ? formatTime(session.created_at) : 'Session')}
          </div>
          <div className="topbar-meta">
            {sessionId?.slice(0, 8)} · {messages.length} message{messages.length === 1 ? '' : 's'}
            {usage && usage.total_tokens > 0 && <> · {usage.total_tokens.toLocaleString()} tokens</>}
          </div>
        </div>
        {session && (
          <div className="topbar-badges">
            <span className="badge">{session.status}</span>
            <span className="badge">{session.approval_mode}</span>
          </div>
        )}
      </header>

      <div className="thread" aria-live="polite">
        <div className="thread-inner">
          {messages.map((message) => {
            const isStreaming = streaming && message.id === streamingIdRef.current
            return (
              <div className="msg" key={message.id}>
                <span className={`avatar avatar-${message.role}`} aria-hidden="true">
                  {ROLE_LABEL[message.role].charAt(0)}
                </span>
                <div className="msg-body">
                  <div className="msg-head">
                    <span className="msg-name">{ROLE_LABEL[message.role]}</span>
                  </div>
                  <div className="msg-text">
                    {message.parts.map((part, index) =>
                      part.kind === 'text' ? (
                        <Markdown key={index} text={part.text} />
                      ) : (
                        <ToolCall key={index} call={part.call} isStreaming={isStreaming} />
                      ),
                    )}
                    {isStreaming && <span className="cursor" aria-hidden="true" />}
                  </div>
                </div>
              </div>
            )
          })}
          <div ref={bottomRef} />
        </div>
      </div>

      <div className="composer-wrap">
        {error && (
          <div className="banner tone-error" role="alert">
            <svg className="icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10 6.5v4.5" />
              <circle cx="10" cy="14" r="0.5" fill="currentColor" stroke="none" />
              <path d="M8.6 3.3 1.9 15a1.5 1.5 0 0 0 1.3 2.2h13.6a1.5 1.5 0 0 0 1.3-2.2L11.4 3.3a1.5 1.5 0 0 0-2.8 0Z" />
            </svg>
            <div>
              <strong>Message failed</strong>
              <p>{error}</p>
            </div>
          </div>
        )}

        <form className="composer" onSubmit={handleSubmit}>
          <label className="sr-only" htmlFor="chat-composer-input">
            Message
          </label>
          <textarea
            id="chat-composer-input"
            ref={textareaRef}
            rows={1}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value)
              autoResize()
            }}
            onKeyDown={handleKeyDown}
            placeholder="Message Claude…"
            disabled={streaming}
          />
          {streaming ? (
            <button
              className="send-btn stop-btn"
              type="button"
              onClick={handleStop}
              aria-label="Stop generating"
            >
              <svg className="icon" viewBox="0 0 20 20" fill="currentColor">
                <rect x="5" y="5" width="10" height="10" rx="1.5" />
              </svg>
            </button>
          ) : (
            <button className="send-btn" type="submit" disabled={!draft.trim()} aria-label="Send message">
              <svg className="icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M17 3 3 9.5l6 2.2M17 3l-4.5 14-4.5-5.3M17 3 8.5 11.7" />
              </svg>
            </button>
          )}
        </form>
        <div className="composer-footer">
          {models.length > 0 && (
            <>
              <label className="sr-only" htmlFor="chat-model-select">
                Model
              </label>
              <select
                id="chat-model-select"
                className="model-select"
                value={model}
                onChange={(e) => selectModel(e.target.value)}
                disabled={streaming}
              >
                {models.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </>
          )}
          <p className="composer-hint">Enter to send · Shift+Enter for a new line</p>
        </div>
      </div>
    </main>
  )
}
