import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from 'react'
import { useOutletContext, useParams } from 'react-router-dom'
import type { ChatMessageOut } from '../../api/client'
import { listMessages, sendMessage } from '../../api/client'
import type { ChatOutletContext } from './ChatLayout'
import Markdown from './Markdown'

interface DisplayMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  text: string
}

const ROLE_LABEL: Record<DisplayMessage['role'], string> = {
  user: 'You',
  assistant: 'Claude',
  system: 'System',
}

function toDisplayMessage(message: ChatMessageOut): DisplayMessage {
  const text = message.content
    .filter((block) => block.type === 'text' && block.text)
    .map((block) => block.text)
    .join('')
  return { id: message.id, role: message.role, text }
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export default function ChatView() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const { sessions, titles, registerFirstMessage } = useOutletContext<ChatOutletContext>()
  const session = sessions.find((s) => s.id === sessionId)
  const title = sessionId ? titles[sessionId] : undefined

  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (!sessionId) return
    listMessages(sessionId).then((history) => {
      const display = history.map(toDisplayMessage)
      setMessages(display)
      const firstUser = display.find((m) => m.role === 'user' && m.text.trim())
      if (firstUser) registerFirstMessage(sessionId, firstUser.text)
    })
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
    if (!sessionId || !draft.trim() || sending) return

    const userText = draft
    setDraft('')
    requestAnimationFrame(autoResize)
    setError(null)
    setSending(true)
    registerFirstMessage(sessionId, userText)
    setMessages((prev) => [...prev, { id: `local-user-${Date.now()}`, role: 'user', text: userText }])
    const assistantId = `local-assistant-${Date.now()}`
    setMessages((prev) => [...prev, { id: assistantId, role: 'assistant', text: '' }])

    try {
      await sendMessage(sessionId, userText, (delta) => {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, text: m.text + delta } : m)),
        )
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Streaming failed.')
    } finally {
      setSending(false)
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
          {messages.map((message) => (
            <div className="msg" key={message.id}>
              <span className={`avatar avatar-${message.role}`} aria-hidden="true">
                {ROLE_LABEL[message.role].charAt(0)}
              </span>
              <div className="msg-body">
                <div className="msg-head">
                  <span className="msg-name">{ROLE_LABEL[message.role]}</span>
                </div>
                <div className="msg-text">
                  <Markdown text={message.text} />
                  {sending && message.role === 'assistant' && message.id.startsWith('local-assistant-') && (
                    <span className="cursor" aria-hidden="true" />
                  )}
                </div>
              </div>
            </div>
          ))}
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
            disabled={sending}
          />
          <button className="send-btn" type="submit" disabled={sending || !draft.trim()} aria-label="Send message">
            <svg className="icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M17 3 3 9.5l6 2.2M17 3l-4.5 14-4.5-5.3M17 3 8.5 11.7" />
            </svg>
          </button>
        </form>
        <p className="composer-hint">Enter to send · Shift+Enter for a new line</p>
      </div>
    </main>
  )
}
