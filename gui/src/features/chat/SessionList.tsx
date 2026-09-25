import { Link } from 'react-router-dom'
import type { ChatSession } from '../../api/client'

interface SessionListProps {
  sessions: ChatSession[]
  titles: Record<string, string>
  loading: boolean
  activeSessionId: string | undefined
  onSelect: () => void
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export default function SessionList({ sessions, titles, loading, activeSessionId, onSelect }: SessionListProps) {
  if (loading) {
    return <ul className="session-list" aria-busy="true" />
  }

  if (sessions.length === 0) {
    return (
      <div className="session-list-empty">
        <p>No sessions yet — start one above.</p>
      </div>
    )
  }

  return (
    <ul className="session-list">
      {sessions.map((session) => (
        <li key={session.id}>
          <Link
            to={`/sessions/${session.id}`}
            className="session-item"
            data-state={session.status}
            aria-current={session.id === activeSessionId ? 'true' : 'false'}
            onClick={onSelect}
          >
            <span className="session-status" aria-hidden="true" />
            <span className="session-main">
              <span className="session-title">{titles[session.id] ?? formatTime(session.created_at)}</span>
              <span className="session-meta">
                <span>{formatTime(session.created_at)}</span>
                <span className="dot" />
                <span>{session.status}</span>
                <span className="dot" />
                <span>{session.approval_mode}</span>
              </span>
            </span>
          </Link>
        </li>
      ))}
    </ul>
  )
}
