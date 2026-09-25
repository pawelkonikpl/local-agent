import { useCallback, useEffect, useState } from 'react'
import { Link, Outlet, useMatch, useNavigate } from 'react-router-dom'
import type { ChatSession, CurrentUser } from '../../api/client'
import { createSession, listSessions, logout } from '../../api/client'
import SessionList from './SessionList'
import { deriveTitle, loadSessionTitles, saveSessionTitle } from './sessionTitles'

interface ChatLayoutProps {
  user: CurrentUser
  onLoggedOut: () => void
}

export interface ChatOutletContext {
  sessions: ChatSession[]
  titles: Record<string, string>
  registerFirstMessage: (sessionId: string, text: string) => void
}

export default function ChatLayout({ user, onLoggedOut }: ChatLayoutProps) {
  const navigate = useNavigate()
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [titles, setTitles] = useState<Record<string, string>>(() => loadSessionTitles())
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const activeMatch = useMatch('/sessions/:sessionId')
  const activeSessionId = activeMatch?.params.sessionId

  useEffect(() => {
    listSessions()
      .then(setSessions)
      .finally(() => setLoading(false))
  }, [])

  const registerFirstMessage = useCallback((sessionId: string, text: string) => {
    const title = deriveTitle(text)
    if (!title) return
    setTitles((prev) => {
      if (prev[sessionId]) return prev
      saveSessionTitle(sessionId, title)
      return { ...prev, [sessionId]: title }
    })
  }, [])

  async function handleNewChat() {
    setCreating(true)
    try {
      const session = await createSession()
      setSessions((prev) => [session, ...prev])
      navigate(`/sessions/${session.id}`)
      setSidebarOpen(false)
    } finally {
      setCreating(false)
    }
  }

  async function handleLogout() {
    await logout()
    onLoggedOut()
    navigate('/login', { replace: true })
  }

  return (
    <div className="chat-shell">
      <button
        type="button"
        className="mobile-menu-btn"
        aria-label={sidebarOpen ? 'Hide sessions' : 'Show sessions'}
        aria-expanded={sidebarOpen}
        onClick={() => setSidebarOpen((open) => !open)}
      >
        <svg className="icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
          <path d="M3 5h14M3 10h14M3 15h14" />
        </svg>
      </button>
      <div
        className={`sidebar-backdrop${sidebarOpen ? ' open' : ''}`}
        onClick={() => setSidebarOpen(false)}
      />

      <aside className={`chat-sidebar${sidebarOpen ? ' open' : ''}`} aria-label="Chat sessions">
        <div className="brand">
          <Link to="/" className="brand-mark">
            local-agent
          </Link>
          <span className="brand-sub">Chat</span>
        </div>

        <button type="button" className="new-chat-btn" onClick={handleNewChat} disabled={creating}>
          <svg className="icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
            <path d="M10 4v12M4 10h12" />
          </svg>
          {creating ? 'Starting…' : 'New chat'}
        </button>

        <SessionList
          sessions={sessions}
          titles={titles}
          loading={loading}
          activeSessionId={activeSessionId}
          onSelect={() => setSidebarOpen(false)}
        />

        <div className="sidebar-footer">
          <div className="user-row">
            <span className="avatar user-avatar" aria-hidden="true">
              {user.email.charAt(0).toUpperCase()}
            </span>
            <div className="user-info">
              <div className="user-email">{user.email}</div>
              <div className="user-role">{user.role}</div>
            </div>
            <button className="logout-btn" type="button" aria-label="Log out" onClick={handleLogout}>
              <svg className="icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <path d="M8 17H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h3" />
                <path d="M13 14l4-4-4-4" />
                <path d="M17 10H7" />
              </svg>
            </button>
          </div>
        </div>
      </aside>

      <Outlet
        key={activeSessionId}
        context={{ sessions, titles, registerFirstMessage } satisfies ChatOutletContext}
      />
    </div>
  )
}
